"""Parsing of the upstream's OpenAI-format Server-Sent-Events stream."""

import json


def iter_openai_sse(resp, usage_out=None, meta_out=None):
    """Yield the delta text from an OpenAI-format SSE stream.

    If ``usage_out`` (a dict) is provided, the final ``usage`` object emitted by
    the upstream (when ``stream_options.include_usage`` is active) is accumulated
    into it.

    If ``meta_out`` (a dict) is provided, it is populated with two keys:

    - ``tool_calls``: the list of tool calls reconstructed from the incremental
      ``delta.tool_calls`` fragments (each fragment appends to its slot by
      ``index``), or ``None`` if the response carried no tool call.
    - ``finish_reason``: the last non-null ``finish_reason`` seen on choice 0.

    This lets callers that aggregate a stream back into a single
    ``chat.completion`` recover forced/auto tool calls, whose payload lives in
    ``delta.tool_calls`` rather than ``delta.content``.

    Args:
        resp: A streaming ``requests.Response`` yielding SSE lines.
        usage_out: Optional dict updated in place with the final ``usage`` object.
        meta_out: Optional dict updated in place with ``tool_calls`` and
            ``finish_reason``.

    Yields:
        The ``content`` string of each non-empty delta.
    """
    tool_calls = {}  # index -> accumulated tool call dict
    finish_reason = None
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
        choice = choices[0]
        if meta_out is not None:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            _accumulate_tool_calls(tool_calls, (choice.get("delta") or {}).get("tool_calls"))
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            yield content

    if meta_out is not None:
        meta_out["tool_calls"] = (
            [tool_calls[i] for i in sorted(tool_calls)] if tool_calls else None
        )
        meta_out["finish_reason"] = finish_reason


def _accumulate_tool_calls(acc, fragments):
    """Merge streamed ``delta.tool_calls`` fragments into ``acc`` (index -> call).

    Only the first fragment for an index carries ``id``/``type``/function name;
    later fragments append their ``function.arguments`` chunk.
    """
    if not fragments:
        return
    for frag in fragments:
        idx = frag.get("index", 0)
        slot = acc.setdefault(idx, {"type": "function", "function": {"name": "", "arguments": ""}})
        if frag.get("id"):
            slot["id"] = frag["id"]
        if frag.get("type"):
            slot["type"] = frag["type"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
