<!--
  Overview per Docker Hub — repository: lordraw/llmproxy
  Short description (max 100 char), da incollare nel campo dedicato:
  "OpenAI/Ollama/llama.cpp-compatible proxy that relays local-LLM clients to NVIDIA's inference API."
  Full description (max ~25.000 char): il contenuto qui sotto.
-->

# llmproxy

**One proxy, three APIs.** `llmproxy` speaks the **Ollama**, **OpenAI** and
**llama.cpp** HTTP protocols at the same time and relays every request to
NVIDIA's OpenAI-compatible inference API. To your tools it *looks* like a local
LLM server; the inference actually runs on NVIDIA infrastructure.

Point Open WebUI, an IDE plugin, a chat frontend or an agent framework at
llmproxy — no client change beyond the base URL — and serve responses from a
hosted NVIDIA model.

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
- **Multi-model** — advertise a whole list of NVIDIA models; clients with a
  model picker (e.g. Open WebUI) just work.
- **Vision** — pass `image_url` content to vision-capable models.
- **Resilient** — configurable upstream timeout and automatic retry with
  exponential backoff on `429`/`5xx`/network errors (honours `Retry-After`).
- **Optional inbound auth** — protect the proxy with `PROXY_API_KEY`
  (`Authorization: Bearer` or `X-Api-Key`); `/` and `/health` stay open for
  health-checks.
- **Observability** — per-request correlation IDs, structured logging,
  `/health` with an optional live upstream probe (`?upstream=1`), plus a live
  **`/stats`** dashboard (and `/stats.json`) with request/latency/token metrics
  and a process-manager view (PID, workers, memory, uptime).
- **Clean architecture** — a small layered Python/Flask package (config · domain
  · upstream · services · web), no database, run under gunicorn.

---

## Quick start

You need a valid **NVIDIA API key** (`nvapi-…`) from
[build.nvidia.com](https://build.nvidia.com/).

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
| `NVIDIA_API_KEY` | _(required)_ | Your NVIDIA API key (`nvapi-…`). |
| `NVIDIA_API_BASE` | `https://integrate.api.nvidia.com/v1` | Upstream base URL. |
| `NVIDIA_MODEL` | `meta/llama-3.1-8b-instruct` | Single default model (fallback). |
| `NVIDIA_MODELS` | _(unset)_ | Comma-separated model list; the first is the default. Overrides `NVIDIA_MODEL`. |
| `NVIDIA_EMBEDDINGS_MODEL` | `nvidia/nv-embedqa-e5-v5` | Default model for embeddings endpoints. |
| `EMBEDDINGS_INPUT_TYPE` | `query` | `input_type` applied if the client omits it (`query`/`passage`; empty to skip). |
| `PROXY_API_KEY` | _(empty)_ | If set, requires this key on inbound requests. Empty = open proxy. |
| `UPSTREAM_TIMEOUT` | `120` | Upstream read timeout (seconds). Raise it for slow non-streaming models. |
| `FORCE_UPSTREAM_STREAM` | `false` | Always stream towards the upstream on `/chat/completions` (transparent to the caller). Avoids read timeouts on slow/non-streaming requests. `1`/`true`/`yes`/`on`. |
| `RETRY_MAX` | `2` | Retries beyond the first attempt on transient errors (`0` disables). |
| `RETRY_BACKOFF` | `0.5` | Base of the exponential backoff (seconds). |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs the payload sent upstream. |
| `LOG_TZ` | `UTC` | IANA timezone for log timestamps (e.g. `Europe/Rome`). |
| `PORT` / `HOST` | `11434` / `0.0.0.0` | Bind address. |
| `WEB_CONCURRENCY` | `2` | gunicorn workers. |
| `THREADS` | `8` | Threads per worker (SSE-friendly). |
| `GUNICORN_TIMEOUT` | `600` | gunicorn worker timeout (seconds). |

Minimal `.env`:

```dotenv
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NVIDIA_MODELS=meta/llama-3.1-8b-instruct,meta/llama-3.2-11b-vision-instruct
LOG_TZ=Europe/Rome
# PROXY_API_KEY=change-me   # uncomment to require inbound auth
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
- `GET /health?upstream=1` — also probes NVIDIA; returns `status: degraded`
  (HTTP `503`) if the provider is unreachable.
- `GET /stats` — self-contained, auto-refreshing **HTML dashboard** with live
  statistics, metrics (requests, latency, tokens, upstream calls) and the
  **process-manager** view (PID, worker pool, memory, uptime). Open it in a
  browser at `http://<host>:11434/stats`.
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
- **NVIDIA API keys & models:** https://build.nvidia.com/

## License

Released under the **MIT License** — free to use, copy, modify, and distribute,
with attribution and no warranty. See the
[LICENSE](https://github.com/lordraw77/llmproxy/blob/main/LICENSE) in the GitHub
repository for the full text.
