"""Deferred audit trail: one deep, structured record per request.

The access log answers *what happened*; this answers *what was asked, what came
back, and what it cost*. Every request produces a single record correlating the
inbound call with its upstream leg: the resolved provider and native model, the
sampling parameters actually sent, the prompt, the completion, the token usage,
the timings, and the session the request belongs to.

**Nothing of that work happens on the request thread.** The web and provider
layers only append plain values to an :class:`AuditEvent` (attribute writes and
``list.append``); the event is then handed to a bounded queue and a single
background writer thread does the expensive part — clipping bodies, parsing the
buffered SSE of a relayed stream, serializing to JSON, writing and rotating the
file. A request therefore pays a few microseconds regardless of how deep the
record is, and a slow or full disk cannot add latency to a completion.

Back-pressure is resolved by **dropping**, never by blocking: if the writer falls
behind and the queue fills up, the record is discarded and counted, because a
proxy that stalls its traffic to keep its audit complete has the trade-off
backwards. Drops are reported at ``/stats`` alongside the written count.

The file is JSON Lines by default (``AUDIT_FORMAT=jsonl``): one self-contained
JSON object per line, appended in completion order, readable with ``tail -f`` and
queryable with ``jq``. ``AUDIT_FORMAT=pretty`` indents each record instead, for
eyeball reading; both forms are valid input to ``jq``.

Per-worker note: under gunicorn every worker runs its own writer thread. They can
share one file (each record is written with a single append) but they cannot
coordinate rotation, so with several workers give each one its own file via the
``{pid}`` placeholder in ``AUDIT_FILE`` (``logs/audit-{pid}.jsonl``).
"""

import hashlib
import json
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from .upstream.sse import iter_openai_sse

#: Request headers consulted, in order, for an explicit session/conversation id.
#: The first non-empty one wins; ``AUDIT_SESSION_HEADER`` prepends a custom name.
SESSION_HEADERS = (
    "X-Session-Id",
    "X-Conversation-Id",
    "X-Chat-Id",
    "X-Request-Session",
)

#: Body keys carrying content rather than parameters: recorded separately (and
#: subject to the body-capture policy) instead of inside ``params``.
_CONTENT_KEYS = frozenset({"messages", "prompt", "input", "stream_options"})

#: Accepted values of ``AUDIT_BODIES``, from least to most disclosing.
BODY_MODES = ("none", "truncated", "full")

#: Value applied when ``AUDIT_BODIES`` is unset or unrecognized.
DEFAULT_BODY_MODE = "truncated"

#: Sentinel queued to tell the writer thread to drain and exit.
_STOP = object()


def normalize_body_mode(value):
    """Return a valid :data:`BODY_MODES` entry for ``value`` (default on garbage)."""
    mode = (value or "").strip().lower()
    return mode if mode in BODY_MODES else DEFAULT_BODY_MODE


@dataclass(frozen=True)
class AuditOptions:
    """How much of each request/response the trail is allowed to record."""

    bodies: str = DEFAULT_BODY_MODE
    max_chars: int = 2000
    session_header: str = ""

    @property
    def captures_bodies(self):
        """Whether prompts/completions are recorded at all."""
        return self.bodies != "none"

    @property
    def budget(self):
        """Character budget per captured text, or ``None`` when uncapped."""
        return None if self.bodies == "full" else max(0, self.max_chars)


# --- the per-request accumulator ------------------------------------------


class AuditEvent:
    """Mutable, single-request accumulator filled in by every layer it crosses.

    Deliberately dumb: each method stores a value or appends to a list, so the
    layers that call it (middleware, provider, streaming generator) pay no
    formatting cost. All shaping happens later in :meth:`to_record`, which the
    writer thread calls.

    The captured request body is the caller's parsed JSON. Nothing in the proxy
    mutates it after the handler has read it, so keeping the reference (instead of
    a copy, which would be a deep copy of the whole prompt on the hot path) is
    safe.
    """

    def __init__(self, options, *, rid, started, method, path, route,
                 client_ip, user_agent, api_key_id, body):
        self._opts = options
        self.rid = rid
        self.started = started            # time.perf_counter() reference
        self.started_at = time.time()     # wall clock, for the record timestamp
        self.method = method
        self.path = path
        self.route = route
        self.client_ip = client_ip
        self.user_agent = user_agent
        self.api_key_id = api_key_id
        self.client_body = body if isinstance(body, dict) else {}

        self.session_id = None
        self.session_source = None

        # Filled in by the provider layer.
        self.provider = None
        self.native_model = None
        self.exposed_model = None
        self.upstream_url = None
        self.upstream_body = None
        self.upstream_status = None
        self.upstream_ms = None
        self.upstream_stream = None
        self.aggregated = False
        self.attempts = 1
        self.cache_hit = False
        self.error = None

        # Filled in by the response path.
        self.status = None
        self.duration_ms = None
        self.usage = None
        self.response_body = None
        self.finish_reason = None
        self.tool_calls = None
        self.ttfb_ms = None

        # Captured output. ``_text`` holds re-framed pieces (the routes that
        # rebuild each chunk), ``_raw``/``_tail`` the bytes of the untouched SSE
        # relay, parsed later by the writer thread.
        self._text = []
        self._text_chars = 0
        self._raw = []
        self._raw_bytes = 0
        self._tail = deque(maxlen=16)
        self._truncated = False

    # -- session ----------------------------------------------------------

    def identify_session(self, headers):
        """Resolve the session this request belongs to, explicitly or by fingerprint.

        A client that tracks conversations (Open WebUI, a custom front-end) can
        say so with a header, which is authoritative. Everything else gets a
        fingerprint of the conversation's opening: the first user message is
        replayed verbatim on every turn of the same chat, so hashing it together
        with the caller and the model groups a multi-turn conversation into one
        session without any client cooperation. It is a heuristic — two identical
        openings from the same client collapse into one session — and the record
        says which of the two produced the id.
        """
        names = (self._opts.session_header,) + SESSION_HEADERS if self._opts.session_header \
            else SESSION_HEADERS
        for name in names:
            value = headers.get(name)
            if value:
                self.session_id = value.strip()[:128]
                self.session_source = f"header:{name}"
                return
        self.session_id = self._fingerprint()
        self.session_source = "fingerprint"

    def _fingerprint(self):
        """Hash (client, model, opening user message) into a short session id."""
        body = self.client_body
        opening = ""
        for message in body.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "user":
                opening = str(message.get("content", ""))[:512]
                break
        if not opening:
            opening = str(body.get("prompt") or "")[:512]
        seed = f"{self.client_ip}|{body.get('model')}|{opening}"
        return "fp-" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]

    # -- routing / upstream leg -------------------------------------------

    def routed(self, exposed_model):
        """Record the exposed model name the request was routed on.

        Captured before the router rewrites the model to the provider's native
        id, which is the only point where both names exist side by side.
        """
        self.exposed_model = exposed_model

    def upstream_request(self, provider, url, body, stream, aggregated):
        """Record the call that is about to leave the proxy."""
        self.provider = provider
        self.upstream_url = url
        self.upstream_body = body
        self.native_model = body.get("model")
        self.upstream_stream = stream
        self.aggregated = aggregated

    def upstream_response(self, status, elapsed_ms, attempts):
        """Record the upstream's status, latency, and how many attempts it took."""
        self.upstream_status = status
        self.upstream_ms = elapsed_ms
        self.attempts = attempts

    def upstream_failed(self, err, elapsed_ms, attempts):
        """Record an upstream call that never produced a response."""
        self.error = {"type": type(err).__name__, "message": str(err)[:500]}
        self.upstream_ms = elapsed_ms
        self.attempts = attempts

    def upstream_rejected(self, status, body):
        """Record the error body of a non-2xx upstream reply.

        The status alone rarely says *why* a provider refused the call — the
        reason (an unknown model, a context overflow, a spent quota) is in the
        body, and this is the only place it exists.
        """
        self.error = {"type": f"upstream_{status}", "message": body}

    def cached(self, provider):
        """Record that the reply came from the response cache (no upstream call)."""
        self.cache_hit = True
        self.provider = provider

    # -- response ----------------------------------------------------------

    def record_usage(self, usage):
        """Store the upstream ``usage`` object (prompt/completion/total tokens)."""
        if usage:
            self.usage = dict(usage)

    def record_outcome(self, meta):
        """Store how a streamed completion ended (finish reason, tool calls)."""
        if meta:
            self.finish_reason = meta.get("finish_reason")
            self.tool_calls = meta.get("tool_calls")

    def record_body(self, body):
        """Keep a non-streaming, OpenAI-shaped reply for the writer to mine.

        Storing the reference costs nothing on the request thread; the completion
        text, the finish reason, the tool calls and the token usage are dug out of
        it later, in :meth:`to_record`. The body is already parsed and memoized on
        the response (``resp_json``), so this adds no second parse either.
        """
        if isinstance(body, dict):
            self.response_body = body

    def first_output(self):
        """Timestamp the first byte handed back to the client (time to first token)."""
        if self.ttfb_ms is None:
            self.ttfb_ms = (time.perf_counter() - self.started) * 1000

    def add_text(self, piece):
        """Accumulate one re-framed output chunk, within the capture budget."""
        budget = self._opts.budget
        if budget is not None and self._text_chars >= budget:
            self._truncated = True
            return
        self._text.append(piece)
        self._text_chars += len(piece)

    def add_raw(self, chunk):
        """Buffer the raw SSE bytes of a relayed stream for deferred parsing.

        The relay forwards the upstream bytes untouched, so the completion and the
        token usage exist only inside them. Parsing here would put an SSE decode
        on the hot path of the busiest route; instead the head (up to the budget)
        and a short ring buffer of the final frames are kept, and the writer thread
        decodes them. The tail matters because ``usage`` and ``finish_reason``
        arrive in the *last* chunks — exactly what a head-only buffer would drop.
        """
        self._tail.append(chunk)
        budget = self._opts.budget
        if budget is not None and self._raw_bytes >= budget * 4:
            self._truncated = True
            return
        self._raw.append(chunk)
        self._raw_bytes += len(chunk)

    def finish(self, status, duration_ms):
        """Close the event with the client-facing status and total duration."""
        self.status = status
        self.duration_ms = duration_ms
        return self

    # -- shaping (writer thread only) --------------------------------------

    def to_record(self):
        """Build the ordered, JSON-serializable record. Runs off the request thread."""
        opts = self._opts
        content, usage, meta = self._resolve_output()
        params_source = self.upstream_body or self.client_body
        record = {
            "ts": _iso(self.started_at),
            "request_id": self.rid,
            "session": {"id": self.session_id, "source": self.session_source},
            "client": {
                "ip": self.client_ip,
                "user_agent": _clip(self.user_agent, 200),
                "api_key_id": self.api_key_id,
            },
            "endpoint": {"method": self.method, "path": self.path, "route": self.route},
            "model": {
                "requested": self.client_body.get("model"),
                "exposed": self.exposed_model or self.client_body.get("model"),
                "native": self.native_model,
                "provider": self.provider,
            },
            "params": _params(params_source, opts),
            "request": self._request_section(),
            "response": {
                "status": self.status,
                "finish_reason": self.finish_reason or meta.get("finish_reason"),
                "content_chars": len(content) if content is not None else None,
                "content": _clip(content, opts.budget) if opts.captures_bodies else None,
                "tool_calls": self.tool_calls or meta.get("tool_calls"),
                "truncated": self._truncated,
            },
            "tokens": _tokens(self.usage or usage),
            "timing": {
                "duration_ms": _ms(self.duration_ms),
                "upstream_ms": _ms(self.upstream_ms),
                "ttfb_ms": _ms(self.ttfb_ms),
            },
            "upstream": {
                "provider": self.provider,
                "url": self.upstream_url,
                "status": self.upstream_status,
                "attempts": self.attempts,
                "stream": self.upstream_stream,
                "aggregated": self.aggregated,
                "cache_hit": self.cache_hit,
            },
            "error": self.error,
        }
        return record

    def _request_section(self):
        """Shape the inbound prompt: always its size, its text within policy."""
        source = self.upstream_body or self.client_body
        messages = source.get("messages")
        prompt = source.get("prompt") or source.get("input")
        opts = self._opts
        section = {
            "message_count": len(messages) if isinstance(messages, list) else None,
            "input_chars": input_chars(messages, prompt),
        }
        if not opts.captures_bodies:
            return section
        if isinstance(messages, list):
            section["messages"] = [_clip_message(m, opts.budget) for m in messages]
        elif prompt is not None:
            section["prompt"] = _clip(prompt if isinstance(prompt, str) else json.dumps(prompt),
                                      opts.budget)
        return section

    def _resolve_output(self):
        """Return ``(content, usage, meta)`` for the reply, parsing buffered SSE if needed.

        Three shapes reach this point: a non-streaming reply (the stored body), a
        re-framed stream (pieces collected chunk by chunk), and a relayed stream
        (raw bytes, decoded here by the same parser that reads a live upstream).
        """
        if self.response_body is not None:
            return _from_body(self.response_body)
        if self._text:
            return "".join(self._text), None, {}
        if not self._raw:
            return None, None, {}
        blob = b"".join(self._raw)
        if self._truncated:
            blob += b"".join(self._tail)
        usage, meta = {}, {}
        content = "".join(iter_openai_sse(_BufferedSSE(blob), usage, meta))
        return content, usage, meta


class _BufferedSSE:
    """Adapts an in-memory SSE blob to the read surface ``iter_openai_sse`` expects.

    Lets the buffered relay bytes be decoded by the same parser that handles a
    live upstream, rather than by a second, drifting copy of that logic.
    """

    def __init__(self, blob):
        self._blob = blob

    def iter_lines(self, decode_unicode=True):
        return self._blob.decode("utf-8", "replace").splitlines()


class NullEvent:
    """No-op stand-in returned by :func:`current` when auditing is off.

    Keeps the call sites unconditional: a layer records what it knows and the
    disabled trail costs an attribute lookup and an empty call, not an ``if`` at
    every site.
    """

    session_id = None

    def __getattr__(self, _name):
        return _noop

    def __setattr__(self, _name, _value):
        """Swallow attribute writes: the null event is a shared singleton."""

    def finish(self, status, duration_ms):
        return self


def _noop(*_args, **_kwargs):
    """Absorb any call made on the null event."""


#: Shared instance of the disabled event.
NO_AUDIT = NullEvent()

# The current request's event, per thread: the provider layer records the
# upstream leg from inside a call chain that carries no audit argument, and the
# streaming generators run after the Flask request context is gone. A thread
# local is what both have in common (a worker thread serves one request at a
# time, and each streaming generator binds its own event while it runs).
_local = threading.local()


def current():
    """Return the audit event bound to this thread, or :data:`NO_AUDIT`."""
    return getattr(_local, "event", None) or NO_AUDIT


def bind(event):
    """Bind ``event`` as this thread's current audit event."""
    _local.event = event


def unbind():
    """Clear this thread's current audit event."""
    _local.event = None


class bound:
    """Context manager binding an event for the duration of a block.

    Used by the streaming generators, which run outside the request context (and
    possibly on another thread than the one that built the event).
    """

    def __init__(self, event):
        self._event = event
        self._previous = None

    def __enter__(self):
        self._previous = getattr(_local, "event", None)
        _local.event = self._event
        return self._event

    def __exit__(self, *_exc):
        _local.event = self._previous
        return False


# --- the trail (queue + writer thread) ------------------------------------


class AuditTrail:
    """Bounded queue in front of a single background writer thread.

    Submitting is a ``put_nowait``; everything else — shaping, serializing,
    writing, rotating — happens in the writer. A full queue drops the record and
    increments a counter rather than blocking the request that produced it.
    """

    def __init__(self, path, *, enabled=True, options=None, fmt="jsonl",
                 queue_size=10000, max_bytes=64 * 1024 * 1024, backups=5, logger=None):
        self.enabled = bool(enabled and path)
        self.path = _expand(path) if path else ""
        self.options = options or AuditOptions()
        self.format = "pretty" if fmt == "pretty" else "jsonl"
        self.max_bytes = max_bytes
        self.backups = backups
        self._logger = logger
        self._queue = queue.Queue(maxsize=queue_size)
        self._counters = threading.Lock()
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self._fh = None
        self._size = 0
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._run, name="llmproxy-audit", daemon=True,
            )
            self._thread.start()

    # -- request-thread API -----------------------------------------------

    def event(self, **kwargs):
        """Build an :class:`AuditEvent` bound to this trail's capture options."""
        if not self.enabled:
            return NO_AUDIT
        return AuditEvent(self.options, **kwargs)

    def submit(self, event):
        """Hand a finished event to the writer, dropping it if the queue is full."""
        if not self.enabled or event is NO_AUDIT:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._counters:
                self.dropped += 1

    def snapshot(self):
        """Return the trail's counters for ``/stats``."""
        with self._counters:
            return {
                "enabled": self.enabled,
                "file": self.path,
                "format": self.format,
                "bodies": self.options.bodies,
                "written": self.written,
                "dropped": self.dropped,
                "errors": self.errors,
                "queued": self._queue.qsize(),
            }

    def close(self, timeout=5.0):
        """Drain the queue and close the file (best effort, at shutdown)."""
        if not self.enabled or self._thread is None:
            return
        self.enabled = False
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            return
        self._thread.join(timeout)

    # -- writer thread -----------------------------------------------------

    def _run(self):
        """Consume the queue until stopped, flushing whenever it runs dry."""
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._close_file()
                return
            try:
                self._write(item)
            except Exception as err:  # noqa: BLE001 - auditing must never kill the worker
                self._failed(err)
            if self._queue.empty() and self._fh is not None:
                self._fh.flush()

    def _write(self, event):
        """Shape, serialize, and append one record, rotating the file if needed."""
        record = event.to_record()
        line = json.dumps(record, ensure_ascii=False, default=str,
                          indent=2 if self.format == "pretty" else None)
        line += "\n"
        data = line.encode("utf-8")
        if self._fh is None:
            self._open_file()
        if self.max_bytes and self._size + len(data) > self.max_bytes:
            self._rotate()
        # One write call per record: several gunicorn workers appending to the
        # same file interleave whole records rather than halves of two.
        self._fh.write(line)
        self._size += len(data)
        with self._counters:
            self.written += 1

    def _open_file(self):
        """Open the audit file for appending, creating its directory if needed."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._size = os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def _close_file(self):
        """Flush and close the audit file, if open."""
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _rotate(self):
        """Roll ``audit.jsonl`` to ``.1``, ``.1`` to ``.2``, dropping the oldest."""
        self._close_file()
        for index in range(self.backups - 1, 0, -1):
            source, target = f"{self.path}.{index}", f"{self.path}.{index + 1}"
            if os.path.exists(source):
                os.replace(source, target)
        if self.backups > 0 and os.path.exists(self.path):
            os.replace(self.path, f"{self.path}.1")
        self._open_file()

    def _failed(self, err):
        """Count a write failure and report the first few, without a log storm."""
        with self._counters:
            self.errors += 1
            count = self.errors
        self._close_file()
        if self._logger is not None and count <= 3:
            self._logger.warning("audit: record not written (%s: %s)", type(err).__name__, err)


def open_trail(settings, logger=None):
    """Build the :class:`AuditTrail` described by ``settings``."""
    return AuditTrail(
        settings.audit_file,
        enabled=settings.audit_enabled,
        options=AuditOptions(
            bodies=settings.audit_bodies,
            max_chars=settings.audit_max_chars,
            session_header=settings.audit_session_header,
        ),
        fmt=settings.audit_format,
        queue_size=settings.audit_queue_size,
        max_bytes=settings.audit_max_bytes,
        backups=settings.audit_backups,
        logger=logger,
    )


# --- shaping helpers -------------------------------------------------------


def _expand(path):
    """Expand the ``{pid}`` / ``{date}`` placeholders allowed in ``AUDIT_FILE``."""
    return (path
            .replace("{pid}", str(os.getpid()))
            .replace("{date}", datetime.now(timezone.utc).strftime("%Y%m%d")))


def _iso(epoch):
    """Render an epoch timestamp as UTC ISO-8601 with millisecond precision."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms(value):
    """Round a millisecond duration for the record, preserving ``None``."""
    return None if value is None else round(value, 1)


def _clip(text, budget):
    """Clip ``text`` to ``budget`` characters, marking what was cut."""
    if text is None or budget is None or len(text) <= budget:
        return text
    return text[:budget] + f"… [+{len(text) - budget} chars]"


def _clip_message(message, budget):
    """Return one chat message with its content clipped to ``budget``.

    Multimodal content (a list of blocks) is serialized before clipping: it is
    where base64 images live, and an uncapped copy of one in every record is how
    an audit file eats a disk.
    """
    if not isinstance(message, dict):
        return _clip(str(message), budget)
    content = message.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, default=str) if content is not None else ""
    clipped = {"role": message.get("role"), "content": _clip(content, budget)}
    if message.get("tool_calls"):
        clipped["tool_calls"] = message["tool_calls"]
    if message.get("name"):
        clipped["name"] = message["name"]
    if message.get("tool_call_id"):
        clipped["tool_call_id"] = message["tool_call_id"]
    return clipped


def _params(body, options):
    """Extract the sampling/tooling parameters actually sent upstream.

    Everything that is not content is a parameter, so a provider-specific or
    newly added knob is recorded without this needing a list of known names.
    ``tools`` collapses to the function names unless bodies are captured in full:
    a tool catalogue is easily larger than the prompt and is the same on every
    request of a session.
    """
    params = {k: v for k, v in body.items() if k not in _CONTENT_KEYS and k != "model"}
    tools = params.get("tools")
    if tools and options.bodies != "full":
        params["tools"] = [
            (t.get("function") or {}).get("name") for t in tools if isinstance(t, dict)
        ]
    return params


def _from_body(body):
    """Mine ``(content, usage, meta)`` out of a non-streaming OpenAI reply.

    Defensive about the shape for the same reason the web layer is: an upstream
    can answer 200 with no choices at all (content filter, applicative error),
    and an audit record must survive that rather than lose the whole request.
    """
    choices = body.get("choices") or []
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") or {}
    content = message.get("content")
    if content is None:
        content = choice.get("text")
    meta = {
        "finish_reason": choice.get("finish_reason"),
        "tool_calls": message.get("tool_calls"),
    }
    return content, body.get("usage"), meta


def _tokens(usage):
    """Normalize an upstream ``usage`` object into the record's token section."""
    usage = usage or {}
    return {
        "prompt": usage.get("prompt_tokens"),
        "completion": usage.get("completion_tokens"),
        "total": usage.get("total_tokens"),
    }


def content_chars(content):
    """Characters of one message's ``content``, without materializing it.

    ``len(str(content))`` was the old spelling, and on multimodal content — a
    list of blocks — it built the ``repr`` of the whole structure just to measure
    it. That is a full copy of every base64 image in the message, produced on the
    hot path (this runs at INFO, i.e. always) and thrown away one line later:
    ~185us and a transient megabyte for a prompt carrying a few images.

    Walking the blocks instead costs no allocation, and counts the text that is
    actually there rather than the punctuation of Python's repr around it.
    """
    if isinstance(content, str):
        return len(content)
    if content is None:
        return 0
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                # A text block carries "text"; an image block's payload is a URL
                # or a base64 blob, whose length is not prompt text.
                text = block.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return len(str(content))


def input_chars(messages, prompt=None):
    """Total characters of the inbound prompt, whichever shape it arrived in."""
    if isinstance(messages, list):
        return sum(content_chars(m.get("content")) for m in messages if isinstance(m, dict))
    if isinstance(prompt, str):
        return len(prompt)
    if isinstance(prompt, list):
        return sum(content_chars(x) for x in prompt)
    return 0


def key_id(token):
    """Return a short, non-reversible identifier of an inbound API key.

    Correlating the requests of one caller must not require the audit file to
    contain the caller's credential, so only a truncated digest is stored.
    """
    if not token:
        return None
    return "sha256:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:12]
