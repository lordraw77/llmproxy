"""Cross-cutting request handling: correlation ids, authentication, access logging."""

import hmac
import time
import uuid

from flask import g, jsonify, request

from .. import audit
from .container import deps


def request_id():
    """Return the short correlation id for the current request (fresh if unset)."""
    return getattr(g, "req_id", None) or uuid.uuid4().hex[:8]


def _unauthorized():
    """Return a 401 JSON response in the OpenAI error format."""
    return jsonify({"error": {"message": "unauthorized", "type": "authentication_error"}}), 401


def _inbound_token():
    """Return the API key presented by the caller, from either accepted header."""
    header = request.headers.get("Authorization", "")
    token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    return token or request.headers.get("X-Api-Key", "").strip()


def _check_auth(settings):
    """Validate the request token when a proxy API key is configured.

    Returns:
        A 401 response tuple if the token is missing/invalid, otherwise ``None``
        (request allowed). Exempt paths and an unset key always return ``None``.
    """
    if not settings.proxy_api_key or request.path in settings.auth_exempt_paths:
        return None
    token = _inbound_token()
    # Constant-time comparison: avoids leaking the key via timing. Compared as
    # bytes because ``compare_digest`` rejects non-ASCII str with a TypeError,
    # which would turn a failed authentication into a 500.
    if not hmac.compare_digest(token.encode("utf-8"), settings.proxy_api_key.encode("utf-8")):
        return _unauthorized()
    return None


# Paths excluded from metrics so self-observation (dashboard auto-refresh) does
# not skew the counters.
_METRICS_EXEMPT = {"/stats", "/stats.json"}

#: Label recorded for a request that matched no route (a 404).
_UNMATCHED = "<unmatched>"


def _metrics_label():
    """Return a bounded, trusted label identifying the current request's route.

    Deliberately **not** ``request.path``: that value is attacker-controlled and
    unbounded, so recording it would let any client grow ``by_path`` without limit
    and place arbitrary text into the ``/stats`` dashboard. The matched URL rule is
    a fixed string from the routing table, which caps the key space at the number
    of registered routes; unmatched requests (404s) collapse into one bucket.
    """
    rule = request.url_rule
    return rule.rule if rule is not None else _UNMATCHED


class DeferredMetrics:
    """A request's bookkeeping, postponed until its streamed body is drained.

    ``after_request`` fires when the headers are emitted, which for a streaming
    route is *before* a single token has been generated: recording there measures
    the handler setup (a few ms) and reports it as the request latency, while
    ``teardown_request`` drops the in-flight gauge on a request that is still
    running (``F9``). Both are handed to the streaming generator instead, which
    calls :meth:`finish` from its ``finally`` — on the normal path and on a client
    hang-up alike.

    The generator runs outside the request context, so everything it needs is
    captured here while the context is still alive. ``status`` is filled in later
    by ``after_request``, which does run before the body is consumed.
    """

    def __init__(self, logger, metrics, rid, label, start, trail=None, event=None):
        self._logger = logger
        self._metrics = metrics
        self._rid = rid
        self._label = label
        self._start = start
        self._trail = trail
        #: The request's audit event, carried here for the same reason as the
        #: rest: the generator outlives the request context that owns it.
        self.event = event if event is not None else audit.NO_AUDIT
        self.method = request.method
        self.path = request.path
        #: Response status, set by ``after_request`` once the response exists.
        self.status = 200

    def finish(self):
        """Record the request with its real, end-of-stream duration and log it."""
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        self._metrics.record(self._label, self.status, elapsed_ms)
        self._metrics.end()
        self._logger.info(
            "[%s] <-- %s %s | status=%s duration=%.0fms (stream complete)",
            self._rid, self.method, self.path, self.status, elapsed_ms,
        )
        submit_audit(self._trail, self.event, self.status, elapsed_ms)


def defer_request_metrics():
    """Hand the current request's metrics and access log over to a streaming generator.

    Returns:
        A :class:`DeferredMetrics` to be finished from the generator's ``finally``,
        or ``None`` when the request is not tracked (an exempt path) — in which
        case the ordinary hooks apply and there is nothing to defer.
    """
    if not getattr(g, "metrics_tracked", False):
        return None
    container = deps()
    deferred = DeferredMetrics(
        container.logger, container.metrics,
        getattr(g, "req_id", None), _metrics_label(), g.req_start,
        container.audit, getattr(g, "audit", None),
    )
    g.deferred_metrics = deferred
    return deferred


def submit_audit(trail, event, status, elapsed_ms):
    """Close ``event`` and hand it to the writer, unless it is not worth a record.

    A successful ``GET`` is discovery or liveness traffic — a model list, a health
    probe, the dashboard polling itself — with no prompt, no completion and no
    tokens to account for; recording every one of them would bury the calls that
    do cost something. A failing one is kept: a 404 on a model id or a rejected
    key is exactly what an audit trail is read for.
    """
    if trail is None or event is audit.NO_AUDIT:
        return
    # ``event.method``, not ``request.method``: a streaming request settles this
    # from its generator, with the request context already gone.
    if event.method == "GET" and status < 400:
        return
    trail.submit(event.finish(status, elapsed_ms))


def _start_audit(container, body):
    """Open this request's audit event and bind it for the layers downstream.

    Bound to the thread as well as to ``g``: the provider layer records the
    upstream leg from a call chain that carries no audit argument, and Flask's
    ``g`` is not reachable from the streaming generators.
    """
    trail = container.audit
    if trail is None or not trail.enabled or request.path in _METRICS_EXEMPT:
        return
    event = trail.event(
        rid=g.req_id,
        started=g.req_start,
        method=request.method,
        path=request.path,
        route=_metrics_label(),
        client_ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
        api_key_id=audit.key_id(_inbound_token()),
        body=body,
    )
    event.identify_session(request.headers)
    g.audit = event
    audit.bind(event)


def register_middleware(app):
    """Attach the before/after-request hooks to ``app``."""

    @app.before_request
    def _log_request_start():
        """Assign a correlation id, enforce authentication, and log the inbound request.

        Returns:
            A 401 response if authentication fails, otherwise ``None`` to continue routing.
        """
        container = deps()
        logger = container.logger
        g.req_id = uuid.uuid4().hex[:8]
        g.req_start = time.perf_counter()

        if request.path not in _METRICS_EXEMPT:
            g.metrics_tracked = True
            container.metrics.begin()

        # Authentication precedes body parsing. The other order made every
        # unauthenticated request pay for a full JSON parse of an
        # attacker-controlled document — plus the session fingerprint hashed over
        # it — before being told "no", which hands an unauthenticated caller the
        # worker's CPU. The rejection is still audited, with an empty body: who
        # was refused, from where, on which key is the whole point of that record,
        # and a refused caller has not earned a parse of what they sent.
        unauthorized = _check_auth(container.settings)
        if unauthorized is not None:
            _start_audit(container, {})
            logger.warning(
                "[%s] --> %s %s | client=%s AUTH FAILED",
                g.req_id, request.method, request.path, request.remote_addr,
            )
            return unauthorized

        body = request.get_json(silent=True) or {}
        _start_audit(container, body)

        logger.info(
            "[%s] --> %s %s | client=%s model=%s stream=%s",
            g.req_id, request.method, request.path,
            request.remote_addr,
            body.get("model") if isinstance(body, dict) else None,
            body.get("stream") if isinstance(body, dict) else None,
        )
        return None

    @app.after_request
    def _log_request_end(response):
        """Log the outcome and total client-side duration, and record request metrics.

        A streaming route has handed both over to its generator (``F9``): here the
        body has not been produced yet, so all this hook can contribute is the
        final status code.
        """
        deferred = getattr(g, "deferred_metrics", None)
        if deferred is not None:
            deferred.status = response.status_code
            return response
        if hasattr(g, "req_start"):
            elapsed_ms = (time.perf_counter() - g.req_start) * 1000
            # One container lookup: ``deps()`` dereferences the ``current_app``
            # proxy and walks the app-context stack on every call, and this hook
            # ran it three times per request.
            container = deps()
            container.logger.info(
                "[%s] <-- %s %s | status=%s duration=%.0fms",
                getattr(g, "req_id", "--------"), request.method, request.path,
                response.status_code, elapsed_ms,
            )
            if getattr(g, "metrics_tracked", False):
                container.metrics.record(_metrics_label(), response.status_code, elapsed_ms)
            submit_audit(
                container.audit, getattr(g, "audit", audit.NO_AUDIT),
                response.status_code, elapsed_ms,
            )
        return response

    @app.teardown_request
    def _end_metrics(exc=None):
        """Balance the in-flight gauge even when a request errors out before after_request.

        Skipped for a deferred (streaming) request: it is still in flight here, and
        its generator's ``finally`` owns the decrement.

        Unbinding the audit event is not: the thread is about to serve another
        request, and a leftover binding would attribute its upstream call to the
        request that just ended. A streaming generator keeps its own binding, made
        independently when it runs.
        """
        audit.unbind()
        if getattr(g, "metrics_tracked", False) and getattr(g, "deferred_metrics", None) is None:
            deps().metrics.end()
