# Overview

## What is llmproxy?

llmproxy is a Python/Flask application (the [`llmproxy`](../llmproxy) package,
launched via [`main.py`](../main.py)) that acts as an **API-compatibility shim**.
It presents the HTTP surface of three different local LLM runtimes and relays the
traffic to NVIDIA's OpenAI-compatible inference API.

The name reflects its purpose: to any client, it *looks* like a locally running
LLM server ("fake" local LLM), while the actual inference happens remotely on
NVIDIA infrastructure.

## Why use it?

Many applications in the local-LLM ecosystem (Open WebUI, various IDE plugins,
chat frontends, agent frameworks) are hard-coded to speak one of:

- the **Ollama** API (`/api/chat`, `/api/generate`, `/api/tags`, …),
- the **OpenAI** API (`/v1/chat/completions`, `/v1/completions`, `/v1/models`),
- the **llama.cpp** `llama-server` native API (`/completion`, `/props`).

llmproxy implements all three at once, so you can keep using those clients while
serving responses from a hosted NVIDIA model. No client reconfiguration beyond
changing the base URL is required.

## How it works

1. A client sends a request in Ollama, OpenAI, or llama.cpp format.
2. llmproxy normalizes the request into the OpenAI chat-completions format.
3. It calls NVIDIA's `POST /v1/chat/completions` with your API key and the
   configured model.
4. The upstream response (JSON or a Server-Sent Events stream) is translated
   back into the exact shape the client expects and returned.

### Request translation

| Incoming format | How llmproxy forwards it |
|-----------------|--------------------------|
| Ollama `/api/chat` | `messages` passed through as-is |
| Ollama `/api/generate` | `system` + `prompt` combined into a `messages` array |
| OpenAI `/v1/chat/completions` | Payload passed through, only `model`/`stream` forced |
| OpenAI `/v1/completions` | `prompt` wrapped into a single user message |
| llama.cpp `/completion` | `prompt` wrapped into a single user message |

Sampling options (`temperature`, `top_p`, `max_tokens`, `stop`,
`presence_penalty`, `frequency_penalty`, `seed`, `n`) are normalized and
forwarded when present; Ollama's `num_predict` / llama.cpp's `n_predict` map to
`max_tokens`. The `/v1/chat/completions` endpoint forwards the whole payload
verbatim. See [API Reference → Sampling parameters](api-reference.md#sampling-parameters).

In addition to chat/completions, llmproxy exposes **embeddings** in both OpenAI
(`/v1/embeddings`) and Ollama (`/api/embed`, `/api/embeddings`) shapes, relayed
to the upstream `/embeddings` endpoint with a dedicated default model
(`NVIDIA_EMBEDDINGS_MODEL`).

### Streaming

Both streaming and non-streaming responses are supported:

- **Ollama** endpoints emit newline-delimited JSON (`application/x-ndjson`),
  the format Ollama clients expect.
- **OpenAI** and **llama.cpp** endpoints emit Server-Sent Events
  (`text/event-stream`), each terminated with the appropriate sentinel
  (`data: [DONE]`).

Internally, `iter_openai_sse()` (in [`upstream/sse.py`](../llmproxy/upstream/sse.py))
parses the upstream SSE stream and yields the
text of each content delta, which is then re-wrapped in the target format. It
also requests `stream_options.include_usage` upstream, so the final token usage
is captured, logged, and (on the Ollama endpoints) re-exposed in the closing
chunk.

Streaming can also be forced towards the upstream regardless of the caller's
choice: with `FORCE_UPSTREAM_STREAM` enabled, non-streaming `/chat/completions`
requests are streamed upstream and re-aggregated into a single response. This
keeps slow, non-streaming generations from hitting the `UPSTREAM_TIMEOUT` read
timeout, while the client still receives a plain JSON reply. See
[Configuration](configuration.md#forcing-upstream-streaming).

### Response caching

Optionally, non-streaming replies can be **cached in memory** to skip repeated
upstream calls. When `CACHE_ENABLED` is set, the `CompletionService` and
`EmbeddingService` compute a SHA-256 key from the outbound payload; a hit replays
the stored body as a `CachedResponse` (no network call), a miss stores the
successful reply. The cache is a per-worker TTL + LRU store (`CACHE_TTL`,
`CACHE_MAX_SIZE`) and never caches streaming responses. `CACHE_POLICY` decides
which requests are eligible at all: by default only embeddings and completions
that can have a single answer (a `seed`, or `temperature: 0`), so turning the
cache on never silently replays a sampled generation. Its hit/miss counters are
surfaced under `metrics.cache` at `/stats`. See
[Configuration → Response caching](configuration.md#response-caching).

## Architecture

llmproxy has no database and no persistence. The code is organized as a small
**layered package** following clean-architecture boundaries: dependencies point
inward, and each layer has a single responsibility. `main.py` is a thin entrypoint
that builds the app and exposes `app` for gunicorn (`main:app`). The only mutable
state is in-memory and per-worker: the metrics collector powering `/stats` and the
optional response cache.

```
main.py                          # entrypoint — builds the app, exports `app`, dev server
llmproxy/
├── config.py                    # Settings dataclass — the only place env vars are read
├── logging_setup.py             # TZFormatter + configure_logging()
├── metrics.py                   # MetricsCollector (per-worker) + process_info()
├── cache.py                     # ResponseCache (per-worker TTL+LRU) + CachedResponse
├── audit.py                     # Deferred per-request audit trail (queue + writer thread)
├── banner.py                    # the ASCII banner and the start-up summary
│
├── domain/                      # pure business rules (no I/O, no framework)
│   └── sampling.py              #   build_sampling_params — normalize sampling options
│
├── providers/                   # infrastructure — the only code that hits the network
│   ├── base.py                  #   Provider — pool, timeout, retry/backoff, telemetry
│   ├── factory.py               #   build_providers — ProviderConfig -> Provider instances
│   ├── registry.py              #   ProviderRegistry — exposed name -> (provider, native id)
│   ├── openai_compatible.py     #   the default dialect (NVIDIA, Groq, Ollama, …)
│   ├── anthropic.py, gemini.py, azure.py    #   native upstreams
│   └── translate/               #   request/response translation, pure and HTTP-free
│       ├── gemini.py            #     OpenAI messages -> Gemini contents/parts
│       └── anthropic.py         #     OpenAI messages -> Messages API blocks
│
├── upstream/                    # shared wire-format parsing
│   └── sse.py                   #   iter_openai_sse — parse upstream SSE deltas (+ usage)
│
├── services/                    # application layer — orchestrates domain + providers
│   ├── routing.py               #   CachedRouter — the cache lookup/post/store cycle
│   ├── completions.py           #   CompletionService — chat() / passthrough()
│   └── embeddings.py            #   EmbeddingService — embed() / resolve / input_type
│
└── web/                         # interface adapters — Flask + per-dialect framing
    ├── __init__.py              #   create_app() application factory (dependency wiring)
    ├── container.py             #   Container — explicit dependency bundle, via deps()
    ├── middleware.py            #   correlation id, inbound auth, access logging, metrics
    ├── errors.py                #   map upstream and routing errors to JSON + status
    ├── formatting.py            #   now_iso, model_entry, the two completion shapes
    ├── templates/stats.html     #   the /stats dashboard (Jinja, autoescaped)
    └── routes/                  #   one blueprint per client dialect
        ├── ollama.py            #     /, /api/version, /api/tags, /api/show, /api/chat,
        │                        #        /api/generate, /api/embed, /api/embeddings
        ├── openai.py            #     /v1/models, /v1/models/<id>, /v1/chat/completions,
        │                        #        /v1/completions, /v1/embeddings
        ├── llamacpp.py          #     /completion, /props
        ├── health.py            #     /health
        └── stats.py             #     /stats (HTML dashboard), /stats.json
```

### Layers and dependency flow

```
web (Flask, dialects)  →  services  →  domain  ←  upstream (implements the calls)
        ↓                     ↓           ↑
      config ──────────── logging ────────┘
```

| Layer | Responsibility | Depends on |
|-------|----------------|------------|
| **domain** | Pure rules: sampling-option translation. No Flask/HTTP/env. | nothing |
| **providers** | The network boundary to every upstream: connection pool, retries, telemetry, and the per-provider format translation. Model resolution lives in its registry. | config, logging |
| **services** | Builds upstream payloads and orchestrates calls, cache included. Speaks only the OpenAI format. | domain, providers |
| **web** | Translates each client dialect to/from the services; cross-cutting auth, logging, metrics, errors. | services, domain |

Adding a new upstream provider means adding a class under `providers/`; adding a
new client dialect means adding a blueprint under `web/routes/` — neither touches
the domain nor the other dialects. Everything is wired once in `create_app()`,
which makes the app straightforward to instantiate with a stubbed upstream in
tests.

### Key design points

- **Stateless request handling** — every request is independent; no sessions or
  persistence. The only state is an in-memory, per-worker metrics collector
  (`metrics.py`) that survives no restart and coordinates no worker.
- **Multi-model** — the set of exposed models is configured by `NVIDIA_MODELS`
  (comma-separated), or the single `NVIDIA_MODEL` as a fallback. All exposed
  models are advertised by the discovery endpoints, and each request is served
  by the client-requested model when it matches, otherwise the default. This
  makes llmproxy work with clients such as Open WebUI that offer a model picker.
- **Resilient upstream calls** — every upstream request has a configurable read
  timeout (`UPSTREAM_TIMEOUT`) and automatic retry with exponential backoff on
  transient failures (network errors, `429`, `5xx`), honouring `Retry-After`.
  Streaming can be forced upstream (`FORCE_UPSTREAM_STREAM`) so slow
  non-streaming generations don't trip the read timeout.
- **Optional response cache** — when `CACHE_ENABLED` is set, identical
  non-streaming requests are served from a per-worker in-memory TTL + LRU cache
  (`CACHE_TTL`, `CACHE_MAX_SIZE`), skipping the upstream call. `CACHE_POLICY`
  restricts eligibility — deterministic replies only, by default. Streaming
  replies are never cached; cache activity is reported at `/stats`.
- **Optional inbound auth** — when `PROXY_API_KEY` is set, a `before_request`
  hook enforces it on every path except `/` and `/health`.
- **Threaded server** — locally, `app.run(..., threaded=True)` handles
  concurrent requests; the Docker image serves the app under gunicorn with
  threaded workers. See [Deployment](deployment.md).
- **Observability** — per-request correlation IDs and structured logging, plus a
  live **`/stats`** dashboard (and `/stats.json`) reporting request/latency/token
  counters, upstream call telemetry, and the process-manager view (PID, worker
  pool, memory, uptime). See [API Reference → `/stats`](api-reference.md#get-stats).

## Technology stack

| Component | Purpose |
|-----------|---------|
| [Flask](https://flask.palletsprojects.com/) | HTTP server and routing |
| [requests](https://requests.readthedocs.io/) | Upstream HTTP calls (one pooled session per provider) |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Loads `.env` configuration |
| [gunicorn](https://gunicorn.org/) | Production WSGI server (Docker image) |
| Python 3.12 | Runtime (per the Docker image) |
