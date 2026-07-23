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

Only `temperature` and `top_p` options are forwarded to the upstream when
present (except on `/v1/chat/completions`, which forwards the whole payload).

### Streaming

Both streaming and non-streaming responses are supported:

- **Ollama** endpoints emit newline-delimited JSON (`application/x-ndjson`),
  the format Ollama clients expect.
- **OpenAI** and **llama.cpp** endpoints emit Server-Sent Events
  (`text/event-stream`), each terminated with the appropriate sentinel
  (`data: [DONE]`).

Internally, `iter_nvidia_sse()` parses the upstream SSE stream and yields the
text of each content delta, which is then re-wrapped in the target format.

## Architecture

llmproxy is deliberately minimal — a single module with no database, no state,
and no persistence.

```
main.py
├── Configuration (env vars loaded at import time)
├── Helpers
│   ├── now_iso()                  → RFC3339 timestamp
│   ├── nvidia_headers()           → Authorization + Content-Type
│   ├── call_nvidia()              → build messages payload, POST upstream
│   ├── call_nvidia_passthrough()  → forward an OpenAI payload verbatim
│   └── iter_nvidia_sse()          → parse upstream SSE deltas
├── Error handling
│   └── handle_nvidia_error()      → map upstream errors to JSON + status
└── Routes
    ├── Ollama:    /, /api/version, /api/tags, /api/show, /api/chat, /api/generate
    ├── OpenAI:    /v1/models, /v1/chat/completions, /v1/completions
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
- **Threaded Flask server** — `app.run(..., threaded=True)` handles concurrent
  requests. This is the built-in development server; see
  [Deployment](deployment.md) for production notes.

## Technology stack

| Component | Purpose |
|-----------|---------|
| [Flask](https://flask.palletsprojects.com/) | HTTP server and routing |
| [requests](https://requests.readthedocs.io/) | Upstream HTTP calls to NVIDIA |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Loads `.env` configuration |
| Python 3.12 | Runtime (per the Docker image) |
