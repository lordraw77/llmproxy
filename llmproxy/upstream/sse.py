"""Parsing of the upstream's OpenAI-format Server-Sent-Events stream."""

import json


#: Bytes pulled from the socket per read while re-framing an SSE stream. ``requests``
#: defaults ``iter_lines`` to 512, which costs a read and a buffer scan roughly
#: every other token. SSE upstreams answer with ``Transfer-Encoding: chunked``, and
#: urllib3 surfaces each HTTP chunk as it arrives regardless of the requested size,
#: so a larger buffer cuts the syscall and scan count without holding a token back.
_READ_SIZE = 65536


def _decode_lines(resp):
    """Yield the text lines of a streaming response, whatever surface it offers."""
    try:
        return resp.iter_lines(decode_unicode=True, chunk_size=_READ_SIZE)
    except TypeError:
        # ``TranslatedStream`` and the audit's buffered replay take no chunk_size.
        return resp.iter_lines(decode_unicode=True)


def _decode_sse(resp):
    """Yield the parsed JSON object of every ``data:`` frame, up to ``[DONE]``."""
    for line in _decode_lines(resp):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def iter_openai_sse(resp, usage_out=None, meta_out=None):
    """Yield the delta text from an OpenAI-format SSE stream.

    ``resp`` may expose either read surface: ``iter_lines`` (a live upstream, or
    the audit trail's buffered replay), which is decoded here, or ``iter_chunks``
    — already-decoded ``chat.completion.chunk`` dicts, which the native providers
    build in memory. Preferring the latter when it exists removes a
    ``json.dumps`` + ``json.loads`` round trip *per token* that existed only to
    hand a dict across this function's boundary: 6.6us against 0.1us per token,
    ~6.5ms of pure CPU burned per thousand-token Claude or Gemini completion.

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
    source = resp.iter_chunks() if hasattr(resp, "iter_chunks") else _decode_sse(resp)
    for chunk in source:
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
