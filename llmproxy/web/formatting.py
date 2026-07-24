"""Presentation helpers shared by the route adapters.

Timestamp formatting, upstream-key guarding, streaming-usage logging, and the
small OpenAI ``/v1/models`` entry builder. These are interface-layer concerns:
they shape data for a particular client dialect.
"""

from datetime import datetime, timezone
from functools import wraps

from flask import jsonify

from .container import deps


def now_iso():
    """Return the current UTC time as ISO-8601 with millisecond precision and a 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def model_entry(name, created):
    """Build a single OpenAI ``/v1/models`` entry for ``name`` created at ``created``."""
    return {"id": name, "object": "model", "created": created, "owned_by": "nvidia"}


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


def require_upstream_key(view):
    """Reject the request with the historical 500 error when NVIDIA_API_KEY is unset."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not deps().settings.nvidia_api_key:
            return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500
        return view(*args, **kwargs)
    return wrapper
