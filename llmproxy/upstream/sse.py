"""Parsing of the upstream's OpenAI-format Server-Sent-Events stream."""

import json


def iter_nvidia_sse(resp, usage_out=None):
    """Yield the delta text from an OpenAI-format SSE stream.

    If ``usage_out`` (a dict) is provided, the final ``usage`` object emitted by
    the upstream (when ``stream_options.include_usage`` is active) is accumulated
    into it.

    Args:
        resp: A streaming ``requests.Response`` yielding SSE lines.
        usage_out: Optional dict updated in place with the final ``usage`` object.

    Yields:
        The ``content`` string of each non-empty delta.
    """
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if usage_out is not None and chunk.get("usage"):
            usage_out.update(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content
