"""Google Gemini provider — native generateContent translation.

Translates the OpenAI dialect to/from Gemini's ``generateContent`` API so the rest
of the app stays provider-agnostic:

- **Request**: OpenAI ``messages`` -> Gemini ``contents`` (``system`` -> top-level
  ``systemInstruction``, ``assistant`` role -> ``model``), sampling params ->
  ``generationConfig``, OpenAI ``tools`` -> ``functionDeclarations``.
- **Response**: Gemini ``candidates`` -> OpenAI ``choices`` (text joined,
  ``functionCall`` -> ``tool_calls``), ``finishReason`` -> ``finish_reason``,
  ``usageMetadata`` -> OpenAI ``usage``.
- **Streaming**: ``:streamGenerateContent?alt=sse`` SSE -> OpenAI chunks.

Auth is the ``x-goog-api-key`` header (set by the factory). Gemini has no
embeddings endpoint here; routing embeddings to it raises.
"""

import json
import time

from .base import AggregatedResponse, Provider, TranslatedStream, resp_json

# Gemini finishReason -> OpenAI finish_reason.
_FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def _to_contents(messages):
    """Return ``(system_instruction, contents)`` from OpenAI-format messages."""
    system_parts = []
    contents = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        text = content if isinstance(content, str) else str(content)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        gem_role = "model" if role == "assistant" else "user"
        contents.append({"role": gem_role, "parts": [{"text": text}]})
    system = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    return system, contents


def _generation_config(payload):
    """Map OpenAI sampling params into a Gemini ``generationConfig``."""
    cfg = {}
    if payload.get("temperature") is not None:
        cfg["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        cfg["topP"] = payload["top_p"]
    if payload.get("max_tokens") is not None:
        cfg["maxOutputTokens"] = payload["max_tokens"]
    stop = payload.get("stop")
    if stop:
        cfg["stopSequences"] = [stop] if isinstance(stop, str) else stop
    return cfg


def _map_tools(openai_tools):
    """Translate OpenAI ``tools`` into a Gemini ``tools`` list."""
    declarations = []
    for tool in openai_tools or []:
        fn = tool.get("function") or {}
        declarations.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return [{"functionDeclarations": declarations}] if declarations else None


def _parts_to_message(parts):
    """Turn Gemini ``content.parts`` into an OpenAI assistant message."""
    text_parts = []
    tool_calls = []
    for part in parts or []:
        if "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            call = part["functionCall"]
            tool_calls.append({
                "id": f"call_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": call.get("name"),
                    "arguments": json.dumps(call.get("args") or {}),
                },
            })
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _usage(meta):
    """Map Gemini ``usageMetadata`` into an OpenAI ``usage`` dict."""
    meta = meta or {}
    prompt = meta.get("promptTokenCount", 0)
    completion = meta.get("candidatesTokenCount", 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": meta.get("totalTokenCount", prompt + completion),
    }


class GeminiProvider(Provider):
    """Gemini upstream reachable through the native generateContent API."""

    def _url(self, path, stream, model=None):
        base = self._config.base_url.rstrip("/")
        if path == "/embeddings":
            raise ValueError("Gemini provider does not support embeddings")
        method = "streamGenerateContent" if stream else "generateContent"
        url = f"{base}/models/{model}:{method}"
        return f"{url}?alt=sse" if stream else url

    def _build_body(self, payload, path, stream, aggregate):
        system, contents = _to_contents(payload.get("messages") or [])
        body = {"contents": contents}
        if system:
            body["systemInstruction"] = system
        cfg = _generation_config(payload)
        if cfg:
            body["generationConfig"] = cfg
        tools = _map_tools(payload.get("tools"))
        if tools:
            body["tools"] = tools
        return body

    def _normalize_nonstream(self, resp):
        data = resp_json(resp)
        candidates = data.get("candidates") or [{}]
        cand = candidates[0]
        out = {
            "id": "chatcmpl-llmproxy",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": data.get("modelVersion"),
            "choices": [{
                "index": 0,
                "message": _parts_to_message((cand.get("content") or {}).get("parts")),
                "logprobs": None,
                "finish_reason": _FINISH.get(cand.get("finishReason"), "stop"),
            }],
            "usage": _usage(data.get("usageMetadata")),
        }
        return AggregatedResponse(out)

    def _normalize_stream(self, resp):
        return TranslatedStream(self._translate_stream(resp), raw=resp)

    def _translate_stream(self, resp):
        """Yield OpenAI ``chat.completion.chunk`` dicts from Gemini SSE events."""
        created = int(time.time())
        base = {"id": "chatcmpl-llmproxy", "object": "chat.completion.chunk", "created": created}
        tool_idx = 0

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            raw = line[len("data:"):].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            candidates = data.get("candidates") or []
            if not candidates:
                continue
            cand = candidates[0]
            parts = (cand.get("content") or {}).get("parts") or []
            model = data.get("modelVersion")

            for part in parts:
                if "text" in part:
                    yield {**base, "model": model,
                           "choices": [{"index": 0, "delta": {"content": part["text"]}, "finish_reason": None}]}
                elif "functionCall" in part:
                    call = part["functionCall"]
                    yield {**base, "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": tool_idx,
                        "id": f"call_{tool_idx}",
                        "type": "function",
                        "function": {"name": call.get("name"), "arguments": json.dumps(call.get("args") or {})},
                    }]}, "finish_reason": None}]}
                    tool_idx += 1

            if cand.get("finishReason"):
                usage = _usage(data.get("usageMetadata"))
                yield {**base, "model": model, "usage": usage,
                       "choices": [{"index": 0, "delta": {},
                                    "finish_reason": _FINISH.get(cand.get("finishReason"), "stop")}]}
