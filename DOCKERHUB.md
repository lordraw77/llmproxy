<!--
  Overview per Docker Hub — repository: lordraw/llmproxy
  Short description (max 100 char), da incollare nel campo dedicato:
  "Ollama/OpenAI/llama.cpp-compatible proxy for NVIDIA, OpenAI, Azure, Anthropic and Gemini."
  Full description (max ~25.000 char): il contenuto qui sotto.
-->

# llmproxy

**One proxy, three APIs, many providers.** `llmproxy` speaks the **Ollama**,
**OpenAI** and **llama.cpp** HTTP protocols at the same time and relays every
request to the provider that owns the requested model — any OpenAI-compatible
endpoint (NVIDIA by default, plus OpenAI, Mistral, Groq, OpenRouter, vLLM, a
local Ollama…), **Azure OpenAI**, **Anthropic** and **Google Gemini**, the last
two translated natively to and from the OpenAI shape. To your tools it *looks*
like a local LLM server; the inference runs wherever you configured it.

Point Open WebUI, an IDE plugin, a chat frontend or an agent framework at
llmproxy — no client change beyond the base URL — and serve responses from any
hosted model. With no configuration file at all, a single NVIDIA provider is
built from the `NVIDIA_*` variables.

- 🐙 Source & docs: **https://github.com/lordraw77/llmproxy**
- 🐳 Image: **`lordraw/llmproxy`**

---

## Features

- **Triple API surface** — Ollama (`/api/chat`, `/api/generate`, `/api/tags`,
  `/api/show`, `/api/version`), OpenAI (`/v1/chat/completions`,
  `/v1/completions`, `/v1/models`, `/v1/models/<id>`, `/v1/embeddings`) and
  llama.cpp (`/completion`, `/props`).
- **Embeddings** — OpenAI `/v1/embeddings` and Ollama `/api/embed` + legacy
  `/api/embeddings`, with a dedicated default embeddings model.
- **Streaming** — SSE for OpenAI/llama.cpp, NDJSON for Ollama; token usage is
  logged and re-exposed.
- **Multi-model** — advertise a whole list of models; clients with a
  model picker (e.g. Open WebUI) just work.
- **Multi-provider** — serve several upstreams at once via `providers.toml`
  (OpenAI-compatible, Azure, Anthropic, Gemini); all their models are exposed
  together as `provider:model` and routed to the owning provider. With no file, a
  single NVIDIA provider is built from the `NVIDIA_*` vars (zero-config).
- **Vision** — pass `image_url` content to vision-capable models.
- **Resilient** — configurable upstream timeout and automatic retry with
  exponential backoff on `429`/`5xx`/network errors (honours `Retry-After`).
- **Response caching** — optional per-worker in-memory cache for non-streaming
  completions and embeddings, with configurable TTL and size
  (`CACHE_ENABLED`/`CACHE_TTL`/`CACHE_MAX_SIZE`/`CACHE_POLICY`) and hit/miss stats
  at `/stats`.
- **Optional inbound auth** — protect the proxy with `PROXY_API_KEY`
  (`Authorization: Bearer` or `X-Api-Key`); `/` and `/health` stay open for
  health-checks.
- **Audit trail** — optional (`AUDIT_ENABLED`): one structured record per
  request — prompt, completion, provider and native model, sampling parameters,
  token usage, retries, latency and time-to-first-token, and the conversation it
  belongs to. Written to a rotating file by a background thread, so the request
  never waits for it; under back-pressure records are dropped, never delayed.
- **Observability** — per-request correlation IDs, structured logging,
  `/health` with an optional live upstream probe (`?upstream=1`), plus a live
  **`/stats`** dashboard (and `/stats.json`) with request/latency/token metrics,
  cache and audit counters, and a process-manager view (PID, workers, memory,
  uptime).
- **Clean architecture** — a small layered Python/Flask package (config · domain
  · upstream · services · web), no database, run under gunicorn.

---

## Quick start

The zero-config path needs a valid **NVIDIA API key** (`nvapi-…`) from
[build.nvidia.com](https://build.nvidia.com/). To serve several providers
instead, mount a `providers.toml` and point `PROVIDERS_CONFIG` at it
(`-v ./providers.toml:/config/providers.toml:ro`); see the
[configuration guide](https://github.com/lordraw77/llmproxy/blob/main/docs/configuration.md).

### docker run

```bash
docker run -d --name llmproxy -p 11434:11434 \
  -e NVIDIA_API_KEY=nvapi-xxxxxxxx \
  -e NVIDIA_MODELS=meta/llama-3.1-8b-instruct,meta/llama-3.2-11b-vision-instruct \
  lordraw/llmproxy:latest
```

### docker compose

```yaml
services:
  llmproxy:
    image: lordraw/llmproxy:latest
    container_name: llmproxy
    restart: unless-stopped
    env_file: .env
    ports:
      - "11434:11434"
    volumes:
      # Only needed with AUDIT_ENABLED: keeps the audit trail on the host.
      - ./logs:/app/logs
```

### Smoke test

```bash
# Discovery
curl http://localhost:11434/v1/models

# OpenAI chat
curl http://localhost:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"meta/llama-3.1-8b-instruct",
       "messages":[{"role":"user","content":"Say hello."}]}'

# Ollama chat
curl http://localhost:11434/api/chat \
  -d '{"model":"meta/llama-3.1-8b-instruct","stream":false,
       "messages":[{"role":"user","content":"Say hello."}]}'

# Health (+ live upstream check)
curl "http://localhost:11434/health?upstream=1"
```

Using it as an **Ollama** backend (e.g. Open WebUI): set the Ollama base URL to
`http://<host>:11434`. As an **OpenAI** backend: base URL
`http://<host>:11434/v1`.

---

## Configuration

All configuration is via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `PROVIDERS_CONFIG` | `providers.toml` | Path to the multi-provider TOML file. When present it defines the providers; when absent, the `NVIDIA_*` vars synthesize a single provider. |
| `NVIDIA_API_KEY` | _(required in env fallback)_ | Your NVIDIA API key (`nvapi-…`). |
| `NVIDIA_API_BASE` | `https://integrate.api.nvidia.com/v1` | Upstream base URL (env fallback). |
| `NVIDIA_MODEL` | `meta/llama-3.1-8b-instruct` | Single default model (fallback). |
| `NVIDIA_MODELS` | _(unset)_ | Comma-separated model list; the first is the default. Overrides `NVIDIA_MODEL`. |
| `NVIDIA_EMBEDDINGS_MODEL` | `nvidia/nv-embedqa-e5-v5` | Default model for embeddings endpoints. |
| `EMBEDDINGS_INPUT_TYPE` | `query` | `input_type` applied if the client omits it (`query`/`passage`; empty to skip). |
| `PROXY_API_KEY` | _(empty)_ | If set, requires this key on inbound requests. Empty = open proxy. |
| `MAX_REQUEST_MB` | `32` | Largest inbound request body accepted, in MiB (buffered in memory before routing). `0` removes the limit; over it the proxy answers `413` in the OpenAI error format. |
| `UPSTREAM_TIMEOUT` | `120` | Upstream read timeout (seconds). Raise it for slow non-streaming models. |
| `FORCE_UPSTREAM_STREAM` | `false` | Always stream towards the upstream on `/chat/completions` (transparent to the caller). Avoids read timeouts on slow/non-streaming requests. `1`/`true`/`yes`/`on`. |
| `CACHE_ENABLED` | `false` | Enable the response cache for non-streaming replies (`1`/`true`/`yes`/`on`). Identical requests skip the upstream call. Stats at `/stats`. |
| `CACHE_TTL` | `300` | Cache entry time-to-live (seconds). |
| `CACHE_MAX_SIZE` | `512` | Max cache entries (LRU eviction past the cap). |
| `CACHE_POLICY` | `deterministic` | Which requests are eligible: `off`, `embeddings`, `deterministic` (embeddings + completions with a `seed` or `temperature: 0`), `all` (everything — identical prompts return identical text for the whole TTL). |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | _(empty)_ | Outbound egress proxy to reach the upstream (corporate proxy). Without it, a proxied host hangs until `UPSTREAM_TIMEOUT`. Keep `localhost,127.0.0.1` in `NO_PROXY`. |
| `RETRY_MAX` | `2` | Retries beyond the first attempt on transient errors (`0` disables). |
| `RETRY_BACKOFF` | `0.5` | Base of the exponential backoff (seconds). |
| `AUDIT_ENABLED` | `false` | Write one structured record per request (prompt, reply, provider, parameters, tokens, timings, session) to `AUDIT_FILE`. Built and written by a background thread: it costs the request nothing. |
| `AUDIT_FILE` | `logs/audit.jsonl` | Destination file; `{pid}` and `{date}` are expanded. **Mount the directory** or the records die with the container, and use `{pid}` with several workers — they can share a file but cannot coordinate its rotation. |
| `AUDIT_FORMAT` | `jsonl` | `jsonl` (one record per line, for `tail`/`jq`) or `pretty` (indented). |
| `AUDIT_BODIES` | `truncated` | Content recorded: `none` (accounting only), `truncated` (clipped to `AUDIT_MAX_CHARS`), `full`. Unless `none`, the file holds conversations **in clear text** — protect it and give it a retention policy. |
| `AUDIT_MAX_CHARS` | `2000` | Character budget per captured text under `truncated`. |
| `AUDIT_QUEUE_SIZE` | `10000` | Records that may wait for the writer. Past this they are dropped and counted at `/stats`, rather than making a request wait. |
| `AUDIT_MAX_MB` / `AUDIT_BACKUPS` | `64` / `5` | Rotation: at most `AUDIT_MAX_MB × (AUDIT_BACKUPS + 1)` on disk. |
| `AUDIT_SESSION_HEADER` | _(empty)_ | Header carrying your front-end's conversation id (e.g. `X-OpenWebUI-Chat-Id`), consulted before the built-in `X-Session-Id`/`X-Conversation-Id`/`X-Chat-Id`. Without one, the session is fingerprinted from the conversation's opening message. |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs the payload sent upstream. |
| `LOG_TZ` | `TZ` env, else `UTC` | IANA timezone for log timestamps (e.g. `Europe/Rome`). |
| `PORT` / `HOST` | `11434` / `0.0.0.0` | Bind address. |
| `WEB_CONCURRENCY` | `2` | gunicorn workers. |
| `THREADS` | `32` | Threads per worker (SSE-friendly). `WEB_CONCURRENCY × THREADS` is a hard ceiling on requests in flight; size it on the concurrency you need, not on the core count (a request spends its life blocked on the upstream, not on the CPU). |
| `GUNICORN_TIMEOUT` | `600` | gunicorn worker timeout (seconds). |
| `UPSTREAM_POOL_SIZE` | value of `THREADS` | Pooled HTTP connections kept open towards each upstream. Below `THREADS`, concurrent calls queue for a free connection and urllib3 discards the surplus, costing the next request a fresh TLS handshake. |

Minimal `.env`:

```dotenv
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NVIDIA_MODELS=meta/llama-3.1-8b-instruct,meta/llama-3.2-11b-vision-instruct
LOG_TZ=Europe/Rome
# PROXY_API_KEY=change-me   # uncomment to require inbound auth
# AUDIT_ENABLED=on          # uncomment to record every request (needs ./logs mounted)
```

---

## Image tags

| Tag | Meaning |
|-----|---------|
| `latest` | Latest released version. |
| `X.Y.Z` | Specific released version (recommended for production). |

The image runs as a non-root user, exposes port `11434`, includes a Docker
`HEALTHCHECK`, and is served by gunicorn with threaded workers (compatible with
SSE streaming). It is built for `linux/amd64` (extendable to other platforms via
the `Makefile`'s `PLATFORMS`).

---

## Health & monitoring

- `GET /health` — liveness plus basic config (`api_key_configured`, number of
  models, default model).
- `GET /health?upstream=1` — also probes every configured provider; returns
  `status: degraded` (HTTP `503`) if one is unreachable.
- `GET /stats` — self-contained, auto-refreshing **HTML dashboard** with live
  statistics, metrics (requests, latency, tokens, upstream calls, cache and audit
  counters) and the **process-manager** view (PID, worker pool, memory, uptime).
  Open it in a browser at `http://<host>:11434/stats`.
- `GET /stats.json` — the same data as JSON for scraping/scripting.

> Metrics are in-memory **per gunicorn worker**: one response reflects the worker
> (`process.pid`) that served it. `/stats` and `/stats.json` honour `PROXY_API_KEY`
> like the API endpoints; only `/` and `/health` stay open for health-checks.

### Smoke test (metrics)

```bash
curl http://localhost:11434/stats.json      # JSON snapshot
# open http://localhost:11434/stats in a browser for the dashboard
```

---

## Links

- **GitHub (source, full docs, issues):** https://github.com/lordraw77/llmproxy
- **Configuration reference:** https://github.com/lordraw77/llmproxy/blob/main/docs/configuration.md
- **Audit trail:** https://github.com/lordraw77/llmproxy/blob/main/docs/audit.md
- **NVIDIA API keys & models:** https://build.nvidia.com/

## License

Released under the **MIT License** — free to use, copy, modify, and distribute,
with attribution and no warranty. See the
[LICENSE](https://github.com/lordraw77/llmproxy/blob/main/LICENSE) in the GitHub
repository for the full text.
