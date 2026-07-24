# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.1] - 2026-07-24

### Added

- **`FORCE_UPSTREAM_STREAM`** option: when enabled, the proxy always requests
  `stream=true` from the upstream on `/chat/completions`, even when the caller
  asked for a non-streaming reply. Because the provider keeps sending SSE bytes,
  the `UPSTREAM_TIMEOUT` read timeout no longer trips on slow/non-streaming
  generations. The stream is transparently re-aggregated into a single
  `chat.completion` object, so caller behavior is unchanged. Fixes the common
  `502 upstream_request_error` (`Read timed out`) on slow reasoning/large models.

### Changed

- Documented that `UPSTREAM_TIMEOUT` is a *read* timeout and that read timeouts
  are retried (total wait `(RETRY_MAX + 1) × UPSTREAM_TIMEOUT`).

## [1.1.0] - 2026-07-24

First structured release after `1.0.0`: the monolithic proxy has been rewritten
as a layered, modular package (`llmproxy/`), and embeddings, a stats/metrics
endpoint and native `llama.cpp` compatibility were added.

### Added

- **Layered architecture** in the `llmproxy/` package:
  - `config` — centralized environment-variable parsing into an immutable
    `Settings` object.
  - `logging_setup` — logging configuration with timezone handling and
    per-request correlation.
  - `metrics` — in-process counters and latency collection.
  - `domain/` — models and sampling logic decoupled from transport.
  - `upstream/` — HTTP client with connection pooling, retry/backoff and SSE streaming.
  - `services/` — orchestration for `completions` and `embeddings`.
  - `web/` — application factory, middleware, error handling and route blueprints.
- **Startup banner**: ASCII-art `LLMPROXY` banner with version and project URL,
  printed on both the development entrypoint and, via a `gunicorn.conf.py`
  `on_starting` hook, once in the gunicorn master process.
- **Embeddings endpoint** (OpenAI-compatible `/v1/embeddings`), with configurable
  model and `input_type` (`NVIDIA_EMBEDDINGS_MODEL`, `EMBEDDINGS_INPUT_TYPE`).
- **Native llama.cpp `/completion` endpoint**, alongside the Ollama (`/api/*`)
  and OpenAI (`/v1/*`) routes.
- **Stats endpoint** and in-process metrics.
- **Optional inbound authentication** via `PROXY_API_KEY`, with configurable
  exempt paths (`/`, `/health`).
- **Retry with backoff** on transient upstream errors (429/500/502/503/504) and a
  sizeable **connection pool** (`UPSTREAM_POOL_SIZE`/`THREADS`).
- **Makefile** with the Docker image version derived automatically from git tags.
- **LICENSE** added to the repository.
- Expanded documentation: `installation`, `configuration`, `usage`,
  `api-reference`, `deployment`, `logging`, `testing`, `troubleshooting`.

### Changed

- `main.py` reduced to a thin WSGI entrypoint (`app = create_app(settings)`),
  compatible with `gunicorn main:app`.
- Test suite (`scripts/tests.sh`) extended to cover embeddings and the new endpoints.
- `Dockerfile` and `.dockerignore` updated for the new package layout.

### Notes

- The upstream is still a single provider (NVIDIA, OpenAI-compatible API).
  Multi-provider support and fail-chain are planned for `1.2.0`/`1.3.0`
  (see [ROADMAP](ROADMAP.md)).

## [1.0.0] - 2026-07-23

### Added

- Initial llmproxy server implementation: an Ollama-compatible proxy to NVIDIA's
  OpenAI-compatible API, with streaming, base documentation and test scripts.

[1.1.0]: https://github.com/lordraw77/llmproxy/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/lordraw77/llmproxy/releases/tag/v1.0.0
