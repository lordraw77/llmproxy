# Configuration

All configuration is provided through environment variables, typically via a
`.env` file loaded at startup by `python-dotenv`. A template is provided in
[`.env.example`](../.env.example).

## Environment variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `HOST` | `0.0.0.0` | No | Network interface the server binds to. |
| `PORT` | `11434` | No | TCP port to listen on. `11434` is Ollama's default port, which is why most Ollama clients work out of the box. |
| `LOG_LEVEL` | `INFO` | No | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `DEBUG` also logs the full payload sent upstream. See [Logging & telemetry](logging.md). |
| `LOG_TZ` | `TZ` env, else `UTC` | No | IANA timezone name (e.g. `Europe/Rome`) for the clock in log lines. Invalid names fall back to `UTC`. |
| `NVIDIA_API_BASE` | `https://integrate.api.nvidia.com/v1` | No | Base URL of the upstream OpenAI-compatible API. Change it to target a different compatible endpoint. |
| `NVIDIA_API_KEY` | *(empty)* | **Yes** | Bearer token sent to the upstream API. Without it, every inference endpoint returns HTTP 500. |
| `NVIDIA_MODEL` | `meta/llama-3.1-8b-instruct` | No | Single-model / default model. Used as the fallback default when `NVIDIA_MODELS` is not set. |
| `NVIDIA_MODELS` | value of `NVIDIA_MODEL` | No | **Comma-separated list** of models to expose. All of them appear in the discovery endpoints (so they show up in Open WebUI's model picker). The **first entry is the default**. See [Multi-model support](#multi-model-support). |
| `PROXY_API_KEY` | *(empty)* | No | If set, **inbound authentication** is enabled: every request must present this key via `Authorization: Bearer <key>` or `X-Api-Key: <key>`. `/` and `/health` stay open for health-checks. Empty = proxy is open (historic behavior). See [Security considerations](#security-considerations). |
| `UPSTREAM_TIMEOUT` | `120` | No | Read timeout in seconds for calls to the upstream API. For **non-streaming** requests no bytes arrive until the whole completion is generated, so slow models (reasoning, large, or queued) can exceed the default — raise it, or enable `FORCE_UPSTREAM_STREAM`. |
| `FORCE_UPSTREAM_STREAM` | `false` | No | When truthy (`1`/`true`/`yes`/`on`), the proxy always requests `stream=true` from the upstream on `/chat/completions`, even if the caller asked for a non-streaming reply. The upstream keeps sending SSE bytes so the read timeout never trips; if the caller wanted a single JSON response the proxy transparently re-aggregates the stream into one `chat.completion`. **Caller behavior is unchanged.** See [Forcing upstream streaming](#forcing-upstream-streaming). |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | *(empty)* | No | Outbound egress proxy used to reach the upstream, for hosts that can only access the internet through a corporate proxy. Standard `requests` proxy variables (upper- or lower-case). Without them, a proxied host connects directly and every upstream request hangs until `UPSTREAM_TIMEOUT`. See [Outbound proxy](#outbound-proxy). |
| `RETRY_MAX` | `2` | No | Number of retries (beyond the first attempt) on transient upstream failures — network errors and HTTP `429`/`5xx`. `0` disables retries. Note: a read timeout counts as a network error and is retried, so with slow non-streaming generations the total wait is `(RETRY_MAX + 1) × UPSTREAM_TIMEOUT`. |
| `RETRY_BACKOFF` | `0.5` | No | Base of the exponential backoff (seconds) between retries. A `Retry-After` header from the upstream takes precedence when present. |
| `NVIDIA_EMBEDDINGS_MODEL` | `nvidia/nv-embedqa-e5-v5` | No | Model used by the embeddings endpoints when the client does not specify one (chat models are not valid for `/embeddings`). |
| `EMBEDDINGS_INPUT_TYPE` | `query` | No | `input_type` applied to embeddings requests when the client omits it (`query` or `passage`; many NVIDIA embedders require it). Leave empty to never force it. |
| `WEB_CONCURRENCY` / `THREADS` / `GUNICORN_TIMEOUT` | `2` / `8` / `600` | No | gunicorn tuning (Docker image only). Workers, threads per worker, and worker timeout. |

## Example `.env`

```dotenv
PORT=11434
HOST=0.0.0.0

NVIDIA_API_BASE=https://integrate.api.nvidia.com/v1
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Single (default) model
NVIDIA_MODEL=meta/llama-3.1-8b-instruct

# Multi-model: comma-separated; the first entry is the default
NVIDIA_MODELS=meta/llama-3.1-8b-instruct,meta/llama-3.1-70b-instruct,mistralai/mistral-7b-instruct-v0.3

# Read timeout towards the upstream (seconds). Raise it for slow non-streaming models.
UPSTREAM_TIMEOUT=300

# Always stream towards the upstream (transparent to the caller). Avoids read timeouts.
FORCE_UPSTREAM_STREAM=on
```

## Outbound proxy

On hosts that reach the internet only through a corporate egress proxy, the
upstream calls will **hang until `UPSTREAM_TIMEOUT`** unless the proxy is
configured — the symptom is a request that logs the NVIDIA payload but never a
`<- NVIDIA response` line. A `curl` from the host may still work because the
shell inherits the proxy variables; the container does not.

Set the standard proxy variables (they are applied to the pooled upstream
session and the health-check probe, and `requests` also honors them natively):

```dotenv
HTTP_PROXY=http://egress-proxy.example.com:80
HTTPS_PROXY=http://egress-proxy.example.com:80
NO_PROXY=localhost,127.0.0.1,.internal.example.com,10.0.0.0/8
```

Because `docker-compose.yml` loads the `.env` via `env_file`, these end up in
the container environment automatically — no change to the compose file is
needed. Always keep `localhost,127.0.0.1` in `NO_PROXY` so the container's own
health check is not routed through the proxy.

## Forcing upstream streaming

Non-streaming upstream requests (`stream: false`) are the main cause of
`502 upstream_request_error` read timeouts: the provider sends **no bytes** until
the entire completion is ready, so a slow or queued model easily blows past
`UPSTREAM_TIMEOUT`. A streaming request to the same model returns fine because
SSE bytes keep flowing.

Set `FORCE_UPSTREAM_STREAM=on` to make llmproxy always stream towards the
upstream on `/chat/completions`, **regardless of what the caller asked for**:

- The caller's request is untouched — a client that sent `stream: false` still
  receives a single, normal `chat.completion` JSON object, and a streaming
  client still gets its SSE/NDJSON relay.
- Internally the proxy consumes the upstream SSE stream, concatenates the delta
  contents, recovers the final `usage`, and rebuilds the non-streaming response.
- Because bytes arrive continuously, `UPSTREAM_TIMEOUT` acts as an *inactivity*
  timeout rather than a *total-generation* timeout — the practical fix for slow
  reasoning/large models.

Only `/chat/completions` is affected; embeddings and other non-streamable paths
are left as-is. Token telemetry is preserved (logged as `telemetry (aggregated)`).

## Multi-model support

llmproxy can expose several upstream models at once. Set `NVIDIA_MODELS` to a
comma-separated list:

```dotenv
NVIDIA_MODELS=meta/llama-3.1-8b-instruct,meta/llama-3.1-70b-instruct
```

Behavior:

- **Discovery** — all listed models are returned by `/api/tags` and
  `/v1/models`, so a client such as **Open WebUI** shows every model in its
  picker.
- **Selection** — for each inference request, the client's `model` field is
  honored **if it matches one of the listed models**; otherwise llmproxy falls
  back to the default (the first entry). This means an unknown model name never
  errors — it is silently served by the default.
- **Default** — the first entry of `NVIDIA_MODELS` is the default, used when the
  client sends no model or an unrecognized one.
- **Whitespace** — spaces around commas are trimmed; empty entries are ignored.

If `NVIDIA_MODELS` is unset, llmproxy falls back to the single `NVIDIA_MODEL`
value, so existing single-model setups keep working unchanged.

## Notes and behavior

### Configuration is read once, at startup

Environment variables are read once into an immutable `Settings` object
(`llmproxy/config.py`) when the application is built at startup. **Changing
`.env` requires a restart** (`docker compose restart` or re-running
`python main.py`) to take effect.

### Model selection

The discovery endpoints — `/api/tags` and `/v1/models` — report every model in
`NVIDIA_MODELS` (or the single `NVIDIA_MODEL` if that list is unset). For each
inference request, the client's requested `model` is used when it matches one of
the exposed models, otherwise the default (first entry) is used. See
[Multi-model support](#multi-model-support) above.

### Sampling options forwarded

For the Ollama, `/v1/completions`, and `/completion` endpoints, the following
sampling parameters are normalized and forwarded upstream:
`temperature`, `top_p`, `max_tokens`, `stop`, `presence_penalty`,
`frequency_penalty`, `seed`, `n`. Ollama's `num_predict` (and llama.cpp's
`n_predict`) are mapped to `max_tokens`. Parameters the upstream OpenAI schema
does not accept (e.g. `top_k`) are dropped to avoid a `400`. The
`/v1/chat/completions` endpoint forwards the **entire** request payload as-is
(see [API Reference](api-reference.md)).

### Choosing a port

If you already run a real Ollama instance on `11434`, either stop it or set a
different `PORT` for llmproxy to avoid a conflict. Remember that Docker Compose
maps `${PORT}:${PORT}`, so the same variable controls both the container and the
published host port.

## Security considerations

- Keep `.env` out of version control. The project's `.gitignore` and
  `.dockerignore` should already exclude it — verify before committing.
- The `NVIDIA_API_KEY` grants access to your NVIDIA account's inference quota.
  Treat it as a secret.
- llmproxy can enforce **inbound authentication**: set `PROXY_API_KEY` and every
  request must present that key via `Authorization: Bearer <key>` or
  `X-Api-Key: <key>` (`/` and `/health` stay open for health-checks). When it is
  empty the proxy is open — anyone who can reach the port can consume your NVIDIA
  quota, so also put it behind a reverse proxy / firewall / VPN and never expose
  it directly to the public internet. See [Deployment](deployment.md).
