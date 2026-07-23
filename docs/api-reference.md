# API Reference

llmproxy exposes three families of endpoints — Ollama, OpenAI, and llama.cpp —
plus a couple of utility routes. Unless noted otherwise, the base URL is
`http://localhost:11434`.

> **Model note:** llmproxy supports multiple models. Discovery endpoints
> (`/api/tags`, `/v1/models`) list every model in `NVIDIA_MODELS`. Each inference
> endpoint honors the client-supplied `model` when it matches one of the exposed
> models; otherwise it falls back to the default (the first entry). See
> [Configuration → Multi-model support](configuration.md#multi-model-support).

> **Auth note:** endpoints that call the upstream API return
> `500 {"error": "NVIDIA_API_KEY non configurata nel file .env"}` when
> `NVIDIA_API_KEY` is unset. When the upstream provider returns an error, that
> error is **propagated to the client**: the provider's HTTP status code is
> preserved and its JSON error body is forwarded verbatim (see
> [Error responses](#error-responses) below).

## Endpoint summary

| Method | Path | Family | Streaming | Purpose |
|--------|------|--------|-----------|---------|
| GET | `/` | Ollama | — | Liveness banner |
| GET | `/api/version` | Ollama | — | Reports a version string |
| GET | `/api/tags` | Ollama | — | Lists available models |
| POST | `/api/show` | Ollama | — | Model metadata |
| POST | `/api/chat` | Ollama | ✅ (default on) | Chat completion |
| POST | `/api/generate` | Ollama | ✅ (default on) | Prompt completion |
| GET | `/v1/models` | OpenAI | — | Lists models |
| POST | `/v1/chat/completions` | OpenAI | ✅ (default off) | Chat completion (pass-through) |
| POST | `/v1/completions` | OpenAI | ✅ (default off) | Text completion |
| POST | `/completion` | llama.cpp | ✅ (default off) | Native llama-server completion |
| GET | `/props` | llama.cpp | — | Server properties |
| GET | `/health` | Misc | — | Health check |

---

## Ollama API

### `GET /`

Liveness banner used by many Ollama clients to detect the server.

**Response** (`text/html`):

```
Ollama is running
```

### `GET /api/version`

**Response:**

```json
{ "version": "0.0.0-llmproxy" }
```

### `GET /api/tags`

Lists every configured model (one entry per `NVIDIA_MODELS` item). The example
below shows a single model; with a multi-model configuration the `models` array
has one object per model.

**Response:**

```json
{
  "models": [
    {
      "name": "meta/llama-3.1-8b-instruct",
      "model": "meta/llama-3.1-8b-instruct",
      "modified_at": "2026-07-23T10:00:00.000000Z",
      "size": 0,
      "digest": "llmproxy",
      "details": {
        "format": "api",
        "family": "nvidia",
        "families": null,
        "parameter_size": "",
        "quantization_level": ""
      }
    }
  ]
}
```

### `POST /api/show`

Returns placeholder model metadata. The request body is ignored.

**Response:**

```json
{
  "license": "",
  "modelfile": "",
  "parameters": "",
  "template": "",
  "details": {
    "format": "api",
    "family": "nvidia",
    "families": null,
    "parameter_size": "",
    "quantization_level": ""
  }
}
```

### `POST /api/chat`

Chat completion in Ollama format.

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | default model | Used if it matches an exposed model, else the default |
| `messages` | array | `[]` | Chat messages (`{role, content}`), forwarded as-is |
| `stream` | boolean | `true` | Streaming is **on by default** |
| `options` | object | `{}` | Only `temperature` and `top_p` are forwarded |

**Non-streaming response** (`stream: false`):

```json
{
  "model": "meta/llama-3.1-8b-instruct",
  "created_at": "2026-07-23T10:00:00.000000Z",
  "message": { "role": "assistant", "content": "Hello!" },
  "done": true,
  "done_reason": "stop"
}
```

**Streaming response** (`application/x-ndjson`): one JSON object per line, each
with a partial `message.content` and `"done": false`, followed by a final
object with empty content and `"done": true`.

### `POST /api/generate`

Single-prompt completion in Ollama format.

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | default model | Used if it matches an exposed model, else the default |
| `prompt` | string | `""` | User prompt |
| `system` | string | — | Optional system message, prepended if present |
| `stream` | boolean | `true` | Streaming is **on by default** |
| `options` | object | `{}` | Only `temperature` and `top_p` are forwarded |

**Non-streaming response:**

```json
{
  "model": "meta/llama-3.1-8b-instruct",
  "created_at": "2026-07-23T10:00:00.000000Z",
  "response": "…",
  "done": true,
  "done_reason": "stop"
}
```

**Streaming response** (`application/x-ndjson`): objects with a `response`
string field and `"done": false`, ending with an empty `response` and
`"done": true`.

---

## OpenAI API

### `GET /v1/models`

Lists every configured model (one `data` entry per `NVIDIA_MODELS` item).

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "meta/llama-3.1-8b-instruct",
      "object": "model",
      "created": 1753257600,
      "owned_by": "nvidia"
    }
  ]
}
```

### `POST /v1/chat/completions`

OpenAI chat completions. **The entire request body is forwarded upstream**
verbatim, with only `model` and `stream` overridden. This means parameters such
as `temperature`, `top_p`, `max_tokens`, `stop`, `tools`, etc. are passed
through to NVIDIA unchanged.

**Request body:** standard OpenAI chat-completions payload. `stream` defaults to
`false`.

**Non-streaming response:** the upstream OpenAI response JSON, with `model`
rewritten to `NVIDIA_MODEL`.

**Streaming response** (`text/event-stream`): the upstream SSE stream is relayed
byte-for-byte to the client.

### `POST /v1/completions`

Legacy OpenAI text-completions endpoint.

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | default model | Used if it matches an exposed model, else the default |
| `prompt` | string \| string[] | `""` | If a list, elements are concatenated |
| `stream` | boolean | `false` | |
| `temperature` | number | — | Forwarded if present |
| `top_p` | number | — | Forwarded if present |

The prompt is wrapped into a single user message before being sent upstream.

**Non-streaming response:**

```json
{
  "id": "cmpl-llmproxy",
  "object": "text_completion",
  "created": 1753257600,
  "model": "meta/llama-3.1-8b-instruct",
  "choices": [
    { "text": "…", "index": 0, "logprobs": null, "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
}
```

> **Note:** token usage counts are always reported as `0`; llmproxy does not
> compute them.

**Streaming response** (`text/event-stream`): `data:`-prefixed JSON chunks with
`choices[].text`, terminated by `data: [DONE]`.

---

## llama.cpp API

### `POST /completion`

Native `llama-server` completion endpoint.

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | default model | Used if it matches an exposed model, else the default |
| `prompt` | string | `""` | User prompt |
| `stream` | boolean | `false` | |
| `temperature` | number | — | Forwarded if present |
| `top_p` | number | — | Forwarded if present |

**Non-streaming response:**

```json
{
  "content": "…",
  "model": "meta/llama-3.1-8b-instruct",
  "prompt": "…",
  "stop": true,
  "stopped_eos": true,
  "tokens_predicted": 0,
  "tokens_evaluated": 0
}
```

**Streaming response** (`text/event-stream`): `data:`-prefixed JSON chunks with
`content` and `"stop": false`, ending with an empty-content object where
`"stop": true` and `"stopped_eos": true`.

### `GET /props`

Server properties, as reported by `llama-server`.

**Response:**

```json
{
  "default_generation_settings": { "model": "meta/llama-3.1-8b-instruct", "n_ctx": 4096 },
  "total_slots": 1,
  "model_path": "meta/llama-3.1-8b-instruct",
  "chat_template": ""
}
```

---

## Utility

### `GET /health`

**Response:**

```json
{ "status": "ok" }
```

Note: the Docker `HEALTHCHECK` uses `GET /` rather than `/health`.

---

## Error responses

| Situation | Status | Body |
|-----------|--------|------|
| `NVIDIA_API_KEY` not set | `500` | `{"error": "NVIDIA_API_KEY non configurata nel file .env"}` |
| Upstream provider returned an error | **upstream status** | The provider's JSON error body, **forwarded verbatim** |
| Upstream returned a non-JSON error | upstream status | `{"error": {"message": "<raw text>", "type": "upstream_error", "code": <status>}}` |
| No response from upstream (timeout, DNS, connection refused) | `502` | `{"error": {"message": "<reason>", "type": "upstream_request_error"}}` |

### Error propagation

When the NVIDIA API rejects a request, llmproxy does **not** mask the failure. It
preserves the upstream **HTTP status code** and forwards the provider's JSON
error body unchanged, so clients receive the real reason. For example, an
invalid API key surfaces the provider's own `401` response:

```json
{
  "error": {
    "message": "Invalid API key provided.",
    "type": "authentication_error",
    "code": 401
  }
}
```

(The exact shape of the `error` object is whatever the provider sends.) If the
upstream body cannot be parsed as JSON, or if no response was received at all,
llmproxy wraps the reason in a minimal `error` object as shown in the table
above.
