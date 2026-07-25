"""Anthropic (Claude) provider — native Messages API translation.

Anthropic does not speak the OpenAI dialect, so this provider translates in both
directions and hides the difference from the rest of the app:

- **Request**: OpenAI ``messages`` -> Anthropic ``/v1/messages`` (the ``system``
  message is lifted to the top-level ``system`` field, ``max_tokens`` is required,
  sampling params and OpenAI ``tools`` are mapped across).
- **Response**: Anthropic ``content`` blocks -> OpenAI ``choices`` (text joined,
  ``tool_use`` blocks -> ``tool_calls``), ``stop_reason`` -> ``finish_reason``,
  ``usage`` token names remapped.
- **Streaming**: Anthropic SSE events -> OpenAI ``chat.completion.chunk`` SSE.

Auth is the ``x-api-key`` header plus ``anthropic-version`` (set by the factory).
Anthropic has no embeddings endpoint; routing embeddings here raises.
"""

import json
import time

from .base import AggregatedResponse, Provider, TranslatedStream, resp_json

# Anthropic stop_reason -> OpenAI finish_reason.
_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _split_system(messages):
    """Return ``(system_text, anthropic_messages)`` from OpenAI-format messages."""
    system_parts = []
    out = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content if isinstance(content, str) else str(content))
            continue
        # Tool results (OpenAI role="tool") map to a user turn carrying a
        # tool_result block; keep it simple and forward as plain user text.
        if role == "tool":
            out.append({"role": "user", "content": str(content)})
            continue
        out.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return "\n\n".join(system_parts), out


def _map_tools(openai_tools):
    """Translate OpenAI ``tools`` into Anthropic ``tools``."""
    tools = []
    for tool in openai_tools or []:
        fn = tool.get("function") or {}
        tools.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return tools


def _to_openai_message(content_blocks):
    """Turn Anthropic ``content`` blocks into an OpenAI assistant message."""
    text_parts = []
    tool_calls = []
    for block in content_blocks or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


class AnthropicProvider(Provider):
    """Claude upstream reachable through the native Messages API."""

    models_path = "/v1/models"

    def _url(self, path, stream, model=None):
        base = self._config.base_url.rstrip("/")
        if path == "/embeddings":
            raise ValueError("Anthropic provider does not support embeddings")
        return f"{base}/v1/messages"

    def _build_body(self, payload, path, stream, aggregate):
        system, messages = _split_system(payload.get("messages") or [])
        body = {
            "model": payload.get("model"),
            "messages": messages,
            "max_tokens": payload.get("max_tokens") or self._config.max_tokens,
            "stream": stream,
        }
        if system:
            body["system"] = system
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
            if payload.get(src) is not None:
                body[dst] = payload[src]
        stop = payload.get("stop")
        if stop:
            body["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        if payload.get("tools"):
            body["tools"] = _map_tools(payload["tools"])
        return body

    def _normalize_nonstream(self, resp):
        data = resp_json(resp)
        usage = data.get("usage") or {}
        out = {
            "id": data.get("id", "chatcmpl-llmproxy"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": data.get("model"),
            "choices": [{
                "index": 0,
                "message": _to_openai_message(data.get("content")),
                "logprobs": None,
                "finish_reason": _FINISH.get(data.get("stop_reason"), "stop"),
            }],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
        }
        return AggregatedResponse(out)

    def _normalize_stream(self, resp):
        return TranslatedStream(self._translate_stream(resp), raw=resp)

    def _translate_stream(self, resp):
        """Yield OpenAI ``chat.completion.chunk`` dicts from Anthropic SSE events."""
        created = int(time.time())
        model = None
        base = {"id": "chatcmpl-llmproxy", "object": "chat.completion.chunk", "created": created}
        # Maps Anthropic content-block index -> OpenAI tool_call index for tool_use blocks.
        tool_index = {}
        next_tool = 0
        prompt_tokens = 0

        def chunk(delta, finish_reason=None, usage=None):
            obj = dict(base)
            obj["model"] = model
            choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
            obj["choices"] = [choice]
            if usage is not None:
                obj["usage"] = usage
            return obj

        event = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line[len("data:"):].strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            etype = data.get("type") or event
            if etype == "message_start":
                msg = data.get("message") or {}
                model = msg.get("model")
                prompt_tokens = (msg.get("usage") or {}).get("input_tokens", 0)
            elif etype == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "tool_use":
                    idx = next_tool
                    tool_index[data.get("index")] = idx
                    next_tool += 1
                    yield chunk({"tool_calls": [{
                        "index": idx,
                        "id": block.get("id"),
                        "type": "function",
                        "function": {"name": block.get("name"), "arguments": ""},
                    }]})
            elif etype == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield chunk({"content": delta.get("text", "")})
                elif delta.get("type") == "input_json_delta":
                    idx = tool_index.get(data.get("index"), 0)
                    yield chunk({"tool_calls": [{
                        "index": idx,
                        "function": {"arguments": delta.get("partial_json", "")},
                    }]})
            elif etype == "message_delta":
                stop_reason = (data.get("delta") or {}).get("stop_reason")
                out_tokens = (data.get("usage") or {}).get("output_tokens", 0)
                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": out_tokens,
                    "total_tokens": prompt_tokens + out_tokens,
                }
                yield chunk({}, finish_reason=_FINISH.get(stop_reason, "stop"), usage=usage)
            elif etype == "message_stop":
                break
