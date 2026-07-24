"""Cross-cutting request handling: correlation ids, authentication, access logging."""

import hmac
import time
import uuid

from flask import g, jsonify, request

from .container import deps


def request_id():
    """Return the short correlation id for the current request (fresh if unset)."""
    return getattr(g, "req_id", None) or uuid.uuid4().hex[:8]


def _unauthorized():
    """Return a 401 JSON response in the OpenAI error format."""
    return jsonify({"error": {"message": "unauthorized", "type": "authentication_error"}}), 401


def _check_auth(settings):
    """Validate the request token when a proxy API key is configured.

    Returns:
        A 401 response tuple if the token is missing/invalid, otherwise ``None``
        (request allowed). Exempt paths and an unset key always return ``None``.
    """
    if not settings.proxy_api_key or request.path in settings.auth_exempt_paths:
        return None
    header = request.headers.get("Authorization", "")
    token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    token = token or request.headers.get("X-Api-Key", "").strip()
    # Constant-time comparison: avoids leaking the key via timing.
    if not hmac.compare_digest(token, settings.proxy_api_key):
        return _unauthorized()
    return None


# Paths excluded from metrics so self-observation (dashboard auto-refresh) does
# not skew the counters.
_METRICS_EXEMPT = {"/stats", "/stats.json"}


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

        unauthorized = _check_auth(container.settings)
        if unauthorized is not None:
            logger.warning(
                "[%s] --> %s %s | client=%s AUTH FAILED",
                g.req_id, request.method, request.path, request.remote_addr,
            )
            return unauthorized

        body = request.get_json(silent=True) or {}
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
        """Log the outcome and total client-side duration, and record request metrics."""
        if hasattr(g, "req_start"):
            elapsed_ms = (time.perf_counter() - g.req_start) * 1000
            deps().logger.info(
                "[%s] <-- %s %s | status=%s duration=%.0fms",
                getattr(g, "req_id", "--------"), request.method, request.path,
                response.status_code, elapsed_ms,
            )
            if getattr(g, "metrics_tracked", False):
                deps().metrics.record(request.path, response.status_code, elapsed_ms)
        return response

    @app.teardown_request
    def _end_metrics(exc=None):
        """Balance the in-flight gauge even when a request errors out before after_request."""
        if getattr(g, "metrics_tracked", False):
            deps().metrics.end()
