"""Presentation helpers shared by the route adapters.

Timestamp formatting, streaming-usage logging, and the small OpenAI
``/v1/models`` entry builder. These are interface-layer concerns: they shape data
for a particular client dialect.
"""

from datetime import datetime, timezone


def now_iso():
    """Return the current UTC time as ISO-8601 with millisecond precision and a 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def model_entry(name, created):
    """Build a single OpenAI ``/v1/models`` entry for ``name`` created at ``created``."""
    return {"id": name, "object": "model", "created": created, "owned_by": "nvidia"}


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
