"""Presentation helpers shared by the route adapters.

Timestamp formatting, streaming-usage logging, the small OpenAI ``/v1/models``
entry builder, and the two shapes every completion route reduces to. These are
interface-layer concerns: they shape data for a particular client dialect.
"""

from datetime import datetime, timezone

from flask import Response, jsonify

from ..upstream.client import resp_json
from ..upstream.sse import iter_nvidia_sse


def now_iso():
    """Return the current UTC time as ISO-8601 with millisecond precision and a 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def model_entry(name, created, owned_by="llmproxy"):
    """Build a single OpenAI ``/v1/models`` entry for ``name`` created at ``created``.

    ``owned_by`` comes from :meth:`~llmproxy.providers.registry.ProviderRegistry.owner_of`:
    it used to be the constant ``"nvidia"``, which announced a Claude or Gemini
    model as NVIDIA's the moment a second provider was configured.
    """
    return {"id": name, "object": "model", "created": created, "owned_by": owned_by}


def first_message(data):
    """Return the first choice's message of an OpenAI response, defensively.

    An upstream can legitimately answer 200 with ``{"choices": []}`` (content
    filter, applicative error, provider off-standard); indexing it blindly turns
    that into a 500 with a traceback. Falls back to an empty assistant message.
    """
    choices = data.get("choices") or []
    message = choices[0].get("message") if choices else None
    return message or {"role": "assistant", "content": ""}


def first_content(data):
    """Return the first choice's textual content, normalized to a string.

    ``content`` is legitimately ``None`` when the reply carries only
    ``tool_calls``. The Ollama and llama.cpp dialects have no field for those, so
    the empty string is a better answer than a serialized ``null``.
    """
    return first_message(data).get("content") or ""


def completion_json(upstream, frame):
    """Return the non-streaming reply, framed into a client dialect.

    Every completion route does the same three things before framing: parse the
    body once, take the first choice's content defensively (``F6``), and default
    a missing ``usage`` to an empty mapping.

    Args:
        upstream: The non-streaming upstream response (or cached stand-in).
        frame: ``frame(content, usage) -> dict``, the dialect-specific body.
    """
    data = resp_json(upstream)
    return jsonify(frame(first_content(data), data.get("usage") or {}))


def completion_stream(upstream, mimetype, frame_chunk, frame_done, logger, metrics, rid):
    """Return the streaming reply, re-framed chunk by chunk into a client dialect.

    Owns the parts that are identical across dialects and were previously copied
    into each route: iterating the upstream SSE, logging the token telemetry once
    the stream ends, and — the reason this matters — closing the upstream in a
    ``finally`` (``F4``). The generator runs outside the request context, so
    ``logger``/``metrics``/``rid`` are passed in rather than read from
    :func:`~llmproxy.web.container.deps`.

    Args:
        upstream: The streaming upstream response.
        mimetype: ``text/event-stream`` or ``application/x-ndjson``.
        frame_chunk: ``frame_chunk(piece) -> str``, one wire frame per token.
        frame_done: ``frame_done(usage) -> str | None``, the terminal frame.
    """
    def generate():
        usage = {}
        try:
            for piece in iter_nvidia_sse(upstream, usage):
                yield frame_chunk(piece)
            log_stream_usage(logger, metrics, rid, usage)
            tail = frame_done(usage)
            if tail:
                yield tail
        finally:
            # Runs outside the request context (the client may also have hung up
            # mid-stream): release the upstream connection back to the pool.
            upstream.close()

    return Response(generate(), mimetype=mimetype)


def log_stream_usage(logger, metrics, rid, usage):
    """Log (and record) the token telemetry accumulated during a streaming response, if any.

    ``metrics`` is passed in explicitly (rather than read via :func:`deps`) because
    this runs inside the streaming generator, after the request/app context is gone.
    """
    if usage:
        metrics.record_tokens(usage)
        logger.info(
            "[%s] telemetry (stream) | prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
