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

> **Inbound auth (optional):** if `PROXY_API_KEY` is set, every request (except
> `/` and `/health`) must present it via `Authorization: Bearer <key>` or
> `X-Api-Key: <key>`, otherwise the proxy replies
> `401 {"error": {"message": "unauthorized", "type": "authentication_error"}}`.
> When `PROXY_API_KEY` is empty the proxy is open. See
> [Configuration → Security](configuration.md#security-considerations).

## Endpoint summary

| Method | Path | Family | Streaming | Purpose |
|--------|------|--------|-----------|---------|
| GET | `/` | Ollama | — | Liveness banner |
| GET | `/api/version` | Ollama | — | Reports a version string |
| GET | `/api/tags` | Ollama | — | Lists available models |
| POST | `/api/show` | Ollama | — | Model metadata |
| POST | `/api/chat` | Ollama | ✅ (default on) | Chat completion |
| POST | `/api/generate` | Ollama | ✅ (default on) | Prompt completion |
| POST | `/api/embed` | Ollama | — | Embeddings (new format) |
| POST | `/api/embeddings` | Ollama | — | Embeddings (legacy format) |
| GET | `/v1/models` | OpenAI | — | Lists models |
| GET | `/v1/models/<id>` | OpenAI | — | Single model detail (`404` if unknown) |
| POST | `/v1/chat/completions` | OpenAI | ✅ (default off) | Chat completion (pass-through) |
| POST | `/v1/completions` | OpenAI | ✅ (default off) | Text completion |
| POST | `/v1/embeddings` | OpenAI | — | Embeddings (pass-through) |
| POST | `/completion` | llama.cpp | ✅ (default off) | Native llama-server completion |
| GET | `/props` | llama.cpp | — | Server properties |
| GET | `/health` | Misc | — | Health check (`?upstream=1` probes NVIDIA) |
| GET | `/stats` | Misc | — | HTML dashboard: statistics, metrics, process manager |
| GET | `/stats.json` | Misc | — | Same data as JSON (machine-readable) |

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
| `options` | object | `{}` | Sampling params are normalized and forwarded (see [note](#sampling-parameters)) |

**Non-streaming response** (`stream: false`):

```json
{
  "model": "meta/llama-3.1-8b-instruct",
  "created_at": "2026-07-23T10:00:00.000000Z",
  "message": { "role": "assistant", "content": "Hello!" },
  "done": true,
  "done_reason": "stop",
  "prompt_eval_count": 18,
  "eval_count": 25
}
```

When the upstream reports token usage, `prompt_eval_count` / `eval_count` are
included (Ollama-style token counts).

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
| `options` | object | `{}` | Sampling params are normalized and forwarded (see [note](#sampling-parameters)) |

**Non-streaming response:**

```json
{
  "model": "meta/llama-3.1-8b-instruct",
  "created_at": "2026-07-23T10:00:00.000000Z",
  "response": "…",
  "done": true,
  "done_reason": "stop",
  "prompt_eval_count": 18,
  "eval_count": 25
}
```

`prompt_eval_count` / `eval_count` are included when the upstream reports usage.

**Streaming response** (`application/x-ndjson`): objects with a `response`
string field and `"done": false`, ending with an empty `response` and
`"done": true` (the final object also carries the token counts when available).

### `POST /api/embed`

Embeddings in the newer Ollama format.

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | `NVIDIA_EMBEDDINGS_MODEL` | Embeddings model; chat models are not valid here |
| `input` | string \| string[] | `""` | Text(s) to embed |

**Response:**

```json
{
  "model": "nvidia/nv-embedqa-e5-v5",
  "embeddings": [[0.0123, -0.0456, ...]]
}
```

### `POST /api/embeddings`

Embeddings in the legacy Ollama format (single vector).

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | `NVIDIA_EMBEDDINGS_MODEL` | Embeddings model |
| `prompt` | string | `""` | Text to embed |

**Response:**

```json
{ "embedding": [0.0123, -0.0456, ...] }
```

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

### `GET /v1/models/<id>`

Detail of a single model (some OpenAI SDKs query it). Returns `404` when the id
is not one of the exposed models.

**Response:**

```json
{
  "id": "meta/llama-3.1-8b-instruct",
  "object": "model",
  "created": 1753257600,
  "owned_by": "nvidia"
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
| sampling | number/… | — | `temperature`, `top_p`, `max_tokens`, `stop`, … forwarded if present (see [note](#sampling-parameters)) |

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
  "usage": { "prompt_tokens": 18, "completion_tokens": 25, "total_tokens": 43 }
}
```

> **Note:** `usage` reflects the upstream token counts when available; it falls
> back to zeros only if the upstream omits them.

**Streaming response** (`text/event-stream`): `data:`-prefixed JSON chunks with
`choices[].text`, terminated by `data: [DONE]`.

### `POST /v1/embeddings`

OpenAI-format embeddings, forwarded to the upstream `/embeddings`.

**Request body:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | string | `NVIDIA_EMBEDDINGS_MODEL` | Embeddings model |
| `input` | string \| string[] | — | Text(s) to embed |
| `input_type` | string | `EMBEDDINGS_INPUT_TYPE` | Added when the client omits it (`query`/`passage`) |

**Response:** the upstream OpenAI embeddings response (`data[].embedding`,
`usage`, …), with `model` set to the resolved model.

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
| sampling | number/… | — | `temperature`, `top_p`, `max_tokens`/`n_predict`, `stop`, … forwarded if present (see [note](#sampling-parameters)) |

**Non-streaming response:**

```json
{
  "content": "…",
  "model": "meta/llama-3.1-8b-instruct",
  "prompt": "…",
  "stop": true,
  "stopped_eos": true,
  "tokens_predicted": 25,
  "tokens_evaluated": 18
}
```

`tokens_predicted` / `tokens_evaluated` reflect the upstream token counts when
available (otherwise `0`).

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

Liveness plus basic configuration.

**Response:**

```json
{
  "status": "ok",
  "api_key_configured": true,
  "models": 3,
  "default_model": "meta/llama-3.1-8b-instruct"
}
```

With `?upstream=1` it also probes NVIDIA (`GET /models`) and adds an `upstream`
field (`ok` / `error:<code>` / `unreachable`); if the provider is unreachable the
`status` becomes `degraded` and the endpoint returns HTTP `503`.

Note: the Docker `HEALTHCHECK` polls `GET /health`.

---

### `GET /stats`

A self-contained, auto-refreshing (every 5s) **HTML dashboard** showing live
statistics, metrics, and the process-manager view. Open it in a browser at
`http://<host>:11434/stats`. No external assets, light/dark aware.

### `GET /stats.json`

The same data as JSON, for scraping or scripting.

**Response (shape):**

```json
{
  "metrics": {
    "started_at": "2026-07-24T06:00:00.000000Z",
    "uptime_seconds": 3600.0,
    "requests": {
      "total": 128,
      "in_flight": 1,
      "errors": 2,
      "by_status": { "200": 124, "401": 2, "502": 2 },
      "by_path": { "/v1/chat/completions": 90, "/api/chat": 34 }
    },
    "latency_ms": { "avg": 812.4, "max": 5210.0, "count": 128 },
    "tokens": { "prompt": 40213, "completion": 18902, "total": 59115 },
    "upstream": { "calls": 126, "errors": 2, "avg_latency_ms": 780.1, "max_latency_ms": 5180.0 }
  },
  "process": {
    "pid": 17,
    "server": "gunicorn",
    "workers_configured": 2,
    "threads_per_worker": 8,
    "worker_timeout_seconds": 600,
    "memory_rss_mb": 61.3,
    "python": "3.12.4",
    "platform": "Linux-..."
  },
  "models": { "exposed": ["..."], "default": "...", "embeddings": "..." }
}
```

Both endpoints respect the optional inbound `PROXY_API_KEY` (they are **not**
auth-exempt, unlike `/` and `/health`). The `/stats` and `/stats.json` requests
are themselves excluded from the counters, so the dashboard's auto-refresh does
not skew the numbers.

> **Per-worker metrics.** Counters live in memory, per gunicorn worker. A single
> response therefore reflects only the worker (see `process.pid`) that served it;
> with `WEB_CONCURRENCY > 1` the numbers rotate across workers. For aggregated,
> cross-worker metrics, scrape `/stats.json` per worker or put a real metrics
> backend (e.g. Prometheus) in front.

---

## Sampling parameters

For the Ollama, `/v1/completions`, and `/completion` endpoints, sampling
parameters are **normalized** before being forwarded upstream. These keys are
passed through when present: `temperature`, `top_p`, `max_tokens`, `stop`,
`presence_penalty`, `frequency_penalty`, `seed`, `n`. Ollama's `num_predict` and
llama.cpp's `n_predict` are mapped to `max_tokens`. Keys the upstream OpenAI
schema does not accept (e.g. `top_k`) are dropped to avoid a `400`.

`/v1/chat/completions` is different: it forwards the **entire** request payload
verbatim (only `model` and `stream` are overridden), so any OpenAI parameter is
passed through unchanged.

---

## Error responses

| Situation | Status | Body |
|-----------|--------|------|
| Missing/invalid inbound key (when `PROXY_API_KEY` is set) | `401` | `{"error": {"message": "unauthorized", "type": "authentication_error"}}` |
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
