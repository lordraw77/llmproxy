"""OpenAI ``messages`` -> Anthropic ``/v1/messages``.

The Messages API keeps the system prompt out of the conversation (a top-level
``system`` field) and models everything else as ``user`` / ``assistant`` turns
whose ``content`` is a list of typed blocks: ``text``, ``image``, ``tool_use``,
``tool_result``.

Three things the OpenAI dialect expresses differently, and that the previous
one-way translation lost:

1. **Tool calls.** An assistant turn carries ``tool_calls`` beside (or instead
   of) its text; Anthropic wants a ``tool_use`` block per call, with the call's
   ``arguments`` parsed from their JSON *string* into an object.
2. **Tool results.** OpenAI sends a separate ``role="tool"`` message keyed by
   ``tool_call_id``; Anthropic wants a ``tool_result`` block **inside a user
   turn**, carrying the ``tool_use_id`` of the call it answers. A result whose
   call is not in the history would be rejected upstream, so it degrades to
   plain text rather than being sent as an unpaired block.
3. **Consecutive same-role turns.** The API accepts them and folds them into one
   turn, but folding them here is what puts parallel tool results in a *single*
   user turn — splitting them across turns is what teaches the model to stop
   issuing parallel calls.

Everything here is pure: no HTTP, no provider state.

**Not validated against the real upstream.** No Anthropic credential is
available, so this is pinned by unit tests on the shape of the body only — a
declared residual risk, not finished work.
"""

import json

#: Content-block type emitted for a tool result whose ``tool_use`` is missing.
_ORPHAN_PREFIX = "Tool result: "


def split_system(messages):
    """Translate OpenAI-format ``messages`` into ``(system_text, messages)``.

    Args:
        messages: The OpenAI ``messages`` array.

    Returns:
        A ``(system, messages)`` pair. ``system`` is the concatenated text of
        every system message (``""`` when there is none), for the top-level
        ``system`` field. ``messages`` is the Anthropic turn list, with
        consecutive same-role turns merged and empty turns dropped.
    """
    known_calls = _tool_call_ids(messages)
    system_parts = []
    out = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.extend(_texts(msg.get("content")))
            continue
        if role == "tool":
            blocks = [_tool_result(msg, known_calls)]
            turn_role = "user"
        else:
            blocks = _content_blocks(msg.get("content"))
            blocks.extend(_tool_use(msg.get("tool_calls")))
            turn_role = "assistant" if role == "assistant" else "user"
        if not blocks:
            # An assistant turn with neither content nor tool calls: content
            # must be non-empty upstream.
            continue
        _append(out, turn_role, blocks)

    return "\n\n".join(system_parts), out


def _append(messages, role, blocks):
    """Add a turn, merging it into the previous one when the role repeats."""
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def _tool_call_ids(messages):
    """Collect every ``tool_call`` id declared by an assistant turn.

    A ``tool_result`` may only reference a ``tool_use`` that appears earlier in
    the same conversation; this is what tells an answerable tool message from an
    orphan one.
    """
    ids = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if call.get("id"):
                ids.add(call["id"])
    return ids


def _texts(content):
    """Extract the plain text of an OpenAI ``content`` value, as a list."""
    return [b["text"] for b in _content_blocks(content) if b.get("type") == "text"]


def _content_blocks(content):
    """Turn an OpenAI ``content`` value into a list of Anthropic content blocks.

    Accepts a plain string or the block list of the multimodal dialect. Unknown
    block types are dropped rather than stringified: a Python ``repr`` in the
    prompt is worse than a missing block.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        block = _block(item)
        if block is not None:
            blocks.append(block)
    return blocks


def _block(item):
    """Translate one OpenAI content block, or return ``None`` if unsupported."""
    btype = item.get("type")
    if btype == "text" or (btype is None and "text" in item):
        text = item.get("text") or ""
        return {"type": "text", "text": text} if text else None
    if btype == "image_url":
        return _image((item.get("image_url") or {}).get("url") or "")
    return None


def _image(url):
    """Turn an image URL into an Anthropic ``image`` block.

    A ``data:`` URI becomes a ``base64`` source; anything else is passed through
    as a ``url`` source, which the API fetches itself.
    """
    if not url:
        return None
    if url.startswith("data:"):
        header, _, data = url[len("data:"):].partition(",")
        if not data:
            return None
        media_type = header.split(";", 1)[0] or "application/octet-stream"
        return {"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": data,
        }}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _tool_use(tool_calls):
    """Turn OpenAI ``tool_calls`` into Anthropic ``tool_use`` blocks."""
    blocks = []
    for call in tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        blocks.append({
            "type": "tool_use",
            "id": call.get("id") or f"toolu_{len(blocks)}",
            "name": name,
            "input": _parse_args(fn.get("arguments")),
        })
    return blocks


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


def _tool_result(msg, known_calls):
    """Turn an OpenAI ``role="tool"`` message into a ``tool_result`` block.

    A block whose ``tool_use_id`` matches no ``tool_use`` in the conversation is
    rejected upstream, so an orphan result degrades to a text block — the old
    behaviour, kept only for the case where it is the sole valid option.
    """
    call_id = msg.get("tool_call_id")
    text = "".join(_texts(msg.get("content")))
    if not call_id or call_id not in known_calls:
        return {"type": "text", "text": f"{_ORPHAN_PREFIX}{text}"}
    return {"type": "tool_result", "tool_use_id": call_id, "content": text}
