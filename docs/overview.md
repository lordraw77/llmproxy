# Overview

## What is llmproxy?

llmproxy is a single-file Python/Flask application ([`main.py`](../main.py)) that
acts as an **API-compatibility shim**. It presents the HTTP surface of three
different local LLM runtimes and relays the traffic to NVIDIA's
OpenAI-compatible inference API.

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

Internally, `iter_nvidia_sse()` parses the upstream SSE stream and yields the
text of each content delta, which is then re-wrapped in the target format. It
also requests `stream_options.include_usage` upstream, so the final token usage
is captured, logged, and (on the Ollama endpoints) re-exposed in the closing
chunk.

## Architecture

llmproxy is deliberately minimal — a single module with no database, no state,
and no persistence.

```
main.py
├── Configuration (env vars loaded at import time)
├── Helpers
│   ├── now_iso()                  → RFC3339 timestamp
│   ├── nvidia_headers()           → Authorization + Content-Type
│   ├── build_sampling_params()    → normalize Ollama/OpenAI sampling options
│   ├── call_nvidia()              → build messages payload, POST upstream
│   ├── call_nvidia_passthrough()  → forward an OpenAI payload verbatim
│   ├── call_nvidia_embeddings()   → POST the upstream /embeddings
│   ├── _post_upstream()           → POST with timeout + retry/backoff
│   ├── _check_auth()              → optional inbound PROXY_API_KEY check
│   └── iter_nvidia_sse()          → parse upstream SSE deltas (+ usage)
├── Error handling
│   └── handle_nvidia_error()      → map upstream errors to JSON + status
└── Routes
    ├── Ollama:    /, /api/version, /api/tags, /api/show, /api/chat,
    │              /api/generate, /api/embed, /api/embeddings
    ├── OpenAI:    /v1/models, /v1/models/<id>, /v1/chat/completions,
    │              /v1/completions, /v1/embeddings
    ├── llama.cpp: /completion, /props
    └── Misc:      /health
```

### Key design points

- **Stateless** — every request is independent; no sessions or storage.
- **Multi-model** — the set of exposed models is configured by `NVIDIA_MODELS`
  (comma-separated), or the single `NVIDIA_MODEL` as a fallback. All exposed
  models are advertised by the discovery endpoints, and each request is served
  by the client-requested model when it matches, otherwise the default. This
  makes llmproxy work with clients such as Open WebUI that offer a model picker.
- **Resilient upstream calls** — every upstream request has a configurable
  timeout (`UPSTREAM_TIMEOUT`) and automatic retry with exponential backoff on
  transient failures (network errors, `429`, `5xx`), honouring `Retry-After`.
- **Optional inbound auth** — when `PROXY_API_KEY` is set, a `before_request`
  hook enforces it on every path except `/` and `/health`.
- **Threaded server** — locally, `app.run(..., threaded=True)` handles
  concurrent requests; the Docker image serves the app under gunicorn with
  threaded workers. See [Deployment](deployment.md).

## Technology stack

| Component | Purpose |
|-----------|---------|
| [Flask](https://flask.palletsprojects.com/) | HTTP server and routing |
| [requests](https://requests.readthedocs.io/) | Upstream HTTP calls to NVIDIA |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Loads `.env` configuration |
| [gunicorn](https://gunicorn.org/) | Production WSGI server (Docker image) |
| Python 3.12 | Runtime (per the Docker image) |
