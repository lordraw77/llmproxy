# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **The response cache no longer replays non-deterministic completions by
  default.** `CACHE_ENABLED` used to cache *any* successful non-streaming
  completion, so a request sampled at `temperature > 0` without a `seed` got the
  same answer for the whole TTL. The new `CACHE_POLICY` picks the eligibility
  level — `off`, `embeddings`, `deterministic` (default: embeddings plus
  completions with a `seed`, or `temperature: 0` and `top_p: 1`), or `all` (the
  previous behaviour). Eligibility is evaluated before the lookup, so tightening
  the policy applies to entries already stored. Ineligible requests are reported
  as `skipped` under `metrics.cache`, alongside the active `policy`.

### Fixed

- **Tool calling and multimodal content now work on the native `gemini`
  provider.** The OpenAI → Gemini translation only handled the first turn of a
  conversation: `str(content)` sent a Python `repr` when the content was a block
  list, `assistant.tool_calls` were dropped (the turn reached Gemini as the
  literal text `"None"`), and a `role="tool"` message was forwarded as prose, so
  the tool result never reached the model as a result. Assistant tool calls now
  become `functionCall` parts, tool results become `functionResponse` parts paired
  by function **name** (Gemini does not use the OpenAI call id), block content
  becomes real parts — text, `inlineData` for `data:` image URIs and audio,
  `fileData` for remote URIs — and consecutive same-role turns are merged, which
  Gemini requires. The request-side translation moved to
  `llmproxy/providers/translate/gemini.py`, free of HTTP and unit-tested.
  Validated end-to-end against the live API. **The `anthropic` provider still has
  the original defect** and cannot do a tool-calling round-trip.
- **The start-up summary describes the registry, not the environment.** It read
  `settings.models` / `settings.default_model`, which derive from the `NVIDIA_*`
  variables: with a `providers.toml` in place the banner listed models that were
  not exposed and omitted the real catalogue. It now reads the registry attached
  to the app and also reports the configured providers, the embeddings model and
  the cache state.
- **Streaming upstream responses are now always closed.** The SSE parser stops at
  the `[DONE]` marker, so the upstream body was never drained to EOF and none of
  the streaming routes released it: the connection was dropped instead of
  returning to the pool, and clients aborting mid-stream produced
  `Connection pool is full, discarding connection`. Every streaming generator
  (`/v1/chat/completions`, `/v1/completions`, `/api/chat`, `/api/generate`,
  `/completion`) now closes the upstream in a `finally`, as does the
  `FORCE_UPSTREAM_STREAM` re-aggregation path.
- **Malformed upstream replies no longer return 500.** Four routes read
  `data["choices"][0]["message"]["content"]` unguarded, so an upstream answering
  200 with `{"choices": []}` (content filter, applicative error, provider
  off-standard) raised `IndexError`. The new `first_message()` / `first_content()`
  helpers in `web/formatting.py` degrade to an empty assistant message, and
  normalize the `content: null` of a tool-calls-only reply to `""` — the Ollama
  and llama.cpp dialects have no field to carry the tool calls.
- **Routing refusals are now `400` JSON instead of `500` HTML.** Only
  `RequestException` had a handler, so an unknown embeddings model, embeddings
  asked of a chat-only native provider (Anthropic, Gemini), or a request against
  an empty catalogue reached the client as Flask's HTML error page. They are now
  `{"error": {"message": …, "type": "invalid_request_error"}}` with status `400`,
  documented in `docs/api-reference.md`. `ProviderRegistry.provider_for()` also
  raises explicitly instead of dereferencing `None`, which used to be an
  `AttributeError` when no model was configured.

### Added

- **Unit test suite** (`tests/`, pytest). Offline and deterministic — no network,
  no `.env`, no `providers.toml`: `Settings` objects are built by the fixtures
  rather than through `load_settings()`. Covers `ResponseCache` (TTL, LRU,
  copy-isolation, counters), `ProviderRegistry` (bare vs. `provider:model` naming,
  aliases, collisions, bare-id resolution across several owners of one model id),
  `build_sampling_params`, and the SSE parser (including incremental reassembly of
  parallel tool calls), plus regression tests for the three fixes below. Run with
  `make test` / `make test-cov`; dev dependencies live in `requirements-dev.txt`
  and never enter the runtime image.

### Documentation

- **`model` in streaming chunks is a documented limit, not a bug.** Streaming
  `/v1/chat/completions` is a byte relay, so its SSE chunks carry the
  provider-native model id while every other streaming endpoint reports the
  exposed name. Rewriting it would cost a parse and a re-serialization per chunk;
  `docs/api-reference.md` now states the asymmetry and how to work around it, and
  tests pin both halves.

## [1.3.0] - 2026-07-24

### Added

- **Multi-provider support** (`llmproxy/providers/`). The single hard-coded NVIDIA
  upstream is replaced by a `Provider` abstraction; several upstreams are now
  served at once, each owning its own set of models.
  - **Provider types**: `openai_compatible` (NVIDIA, OpenAI, Mistral, vLLM, Groq,
    OpenRouter, LM Studio, local Ollama/llama.cpp), `azure` (deployment-scoped
    URLs + `api-version`), `anthropic` (native Messages API translation), and
    `gemini` (native `generateContent` translation). Native providers translate
    their request/response/streaming formats to and from the OpenAI shape, so the
    whole client-facing API is unchanged.
  - **Declarative configuration** via `providers.toml` (path overridable with
    `PROVIDERS_CONFIG`), with `${ENV_VAR}` interpolation for secrets and optional
    per-provider proxy/timeout/`api_version`/`max_tokens`. When no file is present,
    a single provider is synthesized from the existing `NVIDIA_*` env vars, so
    upgrades are zero-config.
  - **Model → provider routing**. All providers' models are exposed together
    (their **union**). With a single provider the exposed names stay **bare**
    (unchanged from before); with two or more providers they are prefixed as
    `provider:model` (colon separator, leaving the `/` in model ids intact) to
    disambiguate. A per-model `alias` overrides the exposed name in either case.
    Clients may still send the bare native model id. A start-up error is raised on
    an exposed-name collision.
  - **Migration tool**: `make migrate-config` (a.k.a.
    `python -m llmproxy.scripts.env_to_toml`) writes a `providers.toml` from the
    current environment, with commented stubs for the other provider types.
  - `/health?upstream=1` now reports a per-provider `upstreams` status map;
    `/stats` shows the provider count.

### Fixed

- **`500` instead of `401` on a non-ASCII inbound API key.** `hmac.compare_digest`
  raises `TypeError` when either `str` operand contains non-ASCII characters, so a
  client sending such a token got an unhandled error (and a traceback in the log)
  where authentication should simply have failed. Both sides are now compared as
  UTF-8 bytes, which keeps the comparison constant-time and returns `401`.
- **`NVIDIA_API_KEY` guard on every inference route.** The `require_upstream_key`
  decorator returned `500` whenever `NVIDIA_API_KEY` was empty — meaningless under
  multi-provider configuration, where a request may target a provider that needs no
  key at all (local Ollama, vLLM, LM Studio) or one whose credential lives in
  `providers.toml`. The decorator is gone; missing credentials are now reported
  **once per provider at start-up** as a warning, and a genuinely unauthenticated
  call surfaces as the upstream's own `401`, propagated verbatim.
- **Stored XSS and unbounded metric cardinality on `/stats`.** Per-path counters
  recorded the raw `request.path`, an attacker-controlled and unbounded value that
  was then interpolated into the dashboard HTML without escaping. Requests are now
  labelled with the **matched URL rule** (a fixed string from the routing table,
  with unmatched 404s collapsed into a single bucket), and every dynamic value in
  the dashboard is HTML-escaped at the point of use.

## [1.2.0] - 2026-07-24

### Added

- **Response caching** for non-streaming replies (`llmproxy/cache.py`,
  `ResponseCache`). When `CACHE_ENABLED` is truthy, identical non-streaming
  chat/text completions and embeddings are served from an in-memory, per-worker
  cache — the upstream call (and its latency and token cost) is skipped. The
  cache key is a SHA-256 of the canonicalized outbound payload (model +
  messages/prompt + forwarded sampling params, or the embeddings input), so any
  change in those fields is a distinct entry. Streaming responses are never
  cached, and only successful (`2xx`) replies are stored.
  - **Configurable** via `CACHE_ENABLED` (default `false`), `CACHE_TTL` (entry
    time-to-live in seconds, default `300`) and `CACHE_MAX_SIZE` (max entries with
    LRU eviction, default `512`). A non-positive TTL or size degrades gracefully
    to "disabled".
  - **Detailed in the statistics**: `/stats.json` gains a `metrics.cache` group
    (`enabled`, `ttl_seconds`, `max_size`, `entries`, `hits`, `misses`, `stores`,
    `evictions`, `expirations`, `hit_rate`) and the `/stats` HTML dashboard shows
    a new **Response cache** card. Cache hits are also logged (`cache HIT`).

## [1.1.4] - 2026-07-24

### Fixed

- **Tool calls dropped on aggregated (`FORCE_UPSTREAM_STREAM`) responses**: when
  a non-streaming request carried a `tool_choice`/function call and the proxy
  re-aggregated the upstream SSE stream, only `delta.content` was collected, so
  the reconstructed `chat.completion` had an empty `content`, no `tool_calls`,
  and a hardcoded `finish_reason: "stop"` — the caller never received the tool
  call despite non-zero `completion_tokens`. The SSE parser now also reassembles
  the incremental `delta.tool_calls` fragments (accumulating `arguments` per
  index) and preserves the upstream `finish_reason` (e.g. `"tool_calls"`), which
  are carried back into the aggregated message. Streaming passthrough and
  plain-text replies are unchanged.

## [1.1.3] - 2026-07-24

### Added

- **Prefix-less OpenAI routes**: `/chat/completions`, `/completions`,
  `/embeddings`, `/models` and `/models/<id>` are now served alongside their
  `/v1/*` equivalents, for clients configured with a base URL that omits `/v1`
  (previously a `404`).
- **`DEBUG` logging of the upstream response body**, mirroring the existing
  request-payload log: `<- NVIDIA response body` for non-streaming replies and
  `<- NVIDIA response body (aggregated)` for `FORCE_UPSTREAM_STREAM` responses
  re-assembled from the SSE stream (both truncated to 2000 chars).

## [1.1.2] - 2026-07-24

### Added

- **Outbound proxy support** via `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`,
  applied to the pooled upstream session and the health-check probe. Needed on
  hosts that reach the internet only through a corporate egress proxy, where the
  container otherwise connects directly and every upstream request hangs until
  `UPSTREAM_TIMEOUT`.

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

- The `-> NVIDIA request` log line now reports the stream flag actually sent
  upstream (marked `(forced, aggregated)` when `FORCE_UPSTREAM_STREAM` rewrote
  a non-streaming request), instead of the caller's original flag.
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

[1.2.0]: https://github.com/lordraw77/llmproxy/compare/v1.1.4...v1.2.0
[1.1.0]: https://github.com/lordraw77/llmproxy/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/lordraw77/llmproxy/releases/tag/v1.0.0
