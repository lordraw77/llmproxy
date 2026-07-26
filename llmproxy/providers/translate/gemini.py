"""OpenAI ``messages`` -> Gemini ``contents``.

Gemini's ``generateContent`` takes a list of ``Content`` objects, each a role
(``user`` or ``model``, and nothing else) plus a list of ``Part`` objects. A part
is a union: ``text``, ``inlineData`` (base64 bytes), ``fileData`` (a URI),
``functionCall`` or ``functionResponse``.

Three things the OpenAI dialect expresses differently, and that a naive
``str(content)`` destroyed:

1. **Block content.** ``content`` may be a list of typed blocks rather than a
   string. Stringifying it sends a Python ``repr`` as the prompt.
2. **The tool round-trip.** An assistant turn carries ``tool_calls`` alongside
   (or instead of) text, and the result comes back as a separate
   ``role="tool"`` message keyed by ``tool_call_id``. Gemini instead wants a
   ``functionCall`` part in the model turn and a ``functionResponse`` part —
   matched **by function name**, not by id — in the following user turn.
3. **Role alternation.** Gemini merges poorly on consecutive same-role turns;
   after dropping ``system`` messages (lifted to ``systemInstruction``) and
   turning tool results into user turns, consecutive duplicates are the norm.

Everything here is pure: no HTTP, no provider state.
"""

import json

#: OpenAI roles that map to Gemini's ``model`` role. Everything else is ``user``.
_MODEL_ROLES = ("assistant",)


def to_contents(messages):
    """Translate OpenAI-format ``messages`` into ``(system_instruction, contents)``.

    Args:
        messages: The OpenAI ``messages`` array.

    Returns:
        A ``(system, contents)`` pair. ``system`` is a Gemini
        ``systemInstruction`` object, or ``None`` when no system message carried
        text. ``contents`` is the ``Content`` list, with consecutive same-role
        turns merged and empty turns dropped.
    """
    names = _tool_call_names(messages)
    system_parts = []
    contents = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.extend(_content_parts(msg.get("content")))
            continue
        if role == "tool":
            parts = [_function_response(msg, names)]
            gem_role = "user"
        else:
            parts = _content_parts(msg.get("content"))
            parts.extend(_function_calls(msg.get("tool_calls")))
            gem_role = "model" if role in _MODEL_ROLES else "user"
        if not parts:
            # An empty turn (e.g. an assistant message with neither content nor
            # tool calls) is rejected upstream: Content.parts must be non-empty.
            continue
        _append(contents, gem_role, parts)

    system = {"parts": system_parts} if system_parts else None
    return system, contents


def _append(contents, role, parts):
    """Add a turn, merging it into the previous one when the role repeats."""
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _tool_call_names(messages):
    """Map ``tool_call_id -> function name`` over the whole conversation.

    Gemini identifies a ``functionResponse`` by name; OpenAI identifies it by the
    id of the call it answers. The name is only available on the assistant turn
    that requested the call, which is why this is a separate pass.
    """
    names = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            if call.get("id") and name:
                names[call["id"]] = name
    return names


def _content_parts(content):
    """Turn an OpenAI ``content`` value into a list of Gemini parts.

    Accepts a plain string or the block list of the multimodal dialect. Unknown
    block types are dropped rather than stringified: a Python ``repr`` in the
    prompt is worse than a missing block, and it is the bug this replaces.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return [{"text": str(content)}]

    parts = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append({"text": block})
            continue
        if not isinstance(block, dict):
            continue
        part = _block_to_part(block)
        if part is not None:
            parts.append(part)
    return parts


def _block_to_part(block):
    """Translate one OpenAI content block, or return ``None`` if unsupported."""
    btype = block.get("type")
    if btype == "text" or (btype is None and "text" in block):
        text = block.get("text") or ""
        return {"text": text} if text else None
    if btype == "image_url":
        url = (block.get("image_url") or {}).get("url") or ""
        return _media_part(url)
    if btype == "input_audio":
        audio = block.get("input_audio") or {}
        data = audio.get("data")
        if not data:
            return None
        fmt = audio.get("format") or "wav"
        return {"inlineData": {"mimeType": f"audio/{fmt}", "data": data}}
    return None


def _media_part(url):
    """Turn an image URL into an ``inlineData`` (data: URI) or ``fileData`` part.

    Gemini does not fetch arbitrary http(s) URLs; ``fileData`` is meant for URIs
    it owns (the Files API). Forwarding a remote URL therefore fails upstream with
    an explicit error, which beats silently dropping the image the caller sent.
    """
    if not url:
        return None
    if url.startswith("data:"):
        header, _, data = url[len("data:"):].partition(",")
        if not data:
            return None
        mime = header.split(";", 1)[0] or "application/octet-stream"
        return {"inlineData": {"mimeType": mime, "data": data}}
    return {"fileData": {"fileUri": url}}


def _function_calls(tool_calls):
    """Turn OpenAI ``tool_calls`` into Gemini ``functionCall`` parts."""
    parts = []
    for call in tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        parts.append({"functionCall": {"name": name, "args": _parse_args(fn.get("arguments"))}})
    return parts


def _parse_args(arguments):
    """Parse a tool call's ``arguments`` (a JSON *string* in the OpenAI dialect)."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _function_response(msg, names):
    """Turn an OpenAI ``role="tool"`` message into a ``functionResponse`` part.

    The name is resolved from the call this message answers; ``name`` on the
    message itself (deprecated by OpenAI, still sent by some clients) is the
    fallback. Gemini requires ``response`` to be an object, so a scalar or a
    non-JSON string is wrapped under ``result``.
    """
    name = names.get(msg.get("tool_call_id")) or msg.get("name") or "function"
    return {"functionResponse": {"name": name, "response": _response_object(msg.get("content"))}}


def _response_object(content):
    """Coerce a tool result into the JSON object Gemini expects."""
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        # A block list: keep the text, which is what a tool result carries.
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return {"result": text}
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return {"result": content}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": content}
