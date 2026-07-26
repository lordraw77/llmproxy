# Testing

llmproxy has two independent test layers:

| Layer | What it is | Needs a running server? | Needs an API key? |
|---|---|---|---|
| [Unit tests](#unit-tests) (`tests/`, pytest) | Pure logic: cache, registry, sampling, SSE parsing, plus regression tests over the routes with a stubbed upstream | No | No |
| [Endpoint tests](#endpoint-tests) (`scripts/tests.sh`) | Integration/smoke: every endpoint and every configured model against a live instance | Yes | Yes |

The unit suite is the one to run on every change — it is offline and takes well
under a second. The endpoint script is what you run before a release, or when
you need to confirm a real upstream behaves as expected.

---

# Unit tests

Offline, deterministic, no network and no `.env`: `Settings` objects are built
directly by the test fixtures instead of through `load_settings()`, so the suite
never picks up your real credentials or `providers.toml`.

## Running them

```bash
make install-dev        # pip install -r requirements-dev.txt (pytest, pytest-cov)
make test               # the whole suite
make test-cov           # with a coverage report over llmproxy/
make test ARGS="-k registry -v"     # pass any pytest option through
```

`pytest` alone works too — the configuration lives in
[`pytest.ini`](../pytest.ini). Test dependencies are in
[`requirements-dev.txt`](../requirements-dev.txt), deliberately **not** in
`requirements.txt`: `tests/` is excluded from the Docker build context, so
nothing here reaches the runtime image.

## What is covered

| File | Target | What it pins |
|---|---|---|
| `tests/test_cache.py` | `llmproxy.cache.ResponseCache` | TTL expiry (with a frozen clock), LRU eviction and recency refresh, deep-copy isolation in both directions, key derivation, the counters `/stats` reports |
| `tests/test_registry.py` | `ProviderRegistry` | Bare names with one provider vs. `provider:model` with two or more, aliases, collision detection, bare-native-id resolution when three providers serve the same model id, embeddings routing |
| `tests/test_sampling.py` | `build_sampling_params` | OpenAI passthrough, the `num_predict` → `max_tokens` alias, unknown keys dropped, `temperature: 0` preserved |
| `tests/test_sse.py` | `iter_nvidia_sse` | Delta accumulation, `[DONE]`, malformed chunks skipped, `usage` extraction, incremental reassembly of parallel tool calls |
| `tests/test_cache_policy.py` | `CACHE_POLICY` eligibility | The four levels, the determinism predicate (`seed`, `temperature`, `top_p`), `skipped` counting, and the end-to-end effect through both services |
| `tests/test_translate_gemini.py` | `providers/translate/gemini.py` | Block content → parts, `data:`/remote images, the tool round-trip (`functionCall` / `functionResponse` paired by name), role merging, and degradation on malformed input |
| `tests/test_translate_anthropic.py` | `providers/translate/anthropic.py` | The same ground for the Messages API: `tool_use` / `tool_result` paired by `tool_use_id`, `image` sources, role merging, orphan tool results. **Shape-only — never run against the live API** |
| `tests/test_streaming_close.py` | Every streaming generator | The upstream is closed both after a full stream and on a mid-stream client hang-up, including the `FORCE_UPSTREAM_STREAM` aggregation path |
| `tests/test_no_proxy.py` | `NO_PROXY` on the provider sessions | An excluded host gets no proxy, a remote one still does, and the matching rules (suffix, IP, CIDR, `*`) |
| `tests/test_stream_metrics.py` | Request accounting on streaming routes | Latency spans the whole stream (a deliberately slow upstream separates it from the handler setup), `in_flight` holds until the last frame, a client hang-up still settles the request, and the non-streaming path is untouched |
| `tests/test_response_shape.py` | `first_message` / `first_content` | A `2xx` reply with no choices, or `content: null` on a tool-calls-only message, degrades instead of raising |
| `tests/test_routing_errors.py` | The `ValueError` → `400` handler | Unknown models, embeddings asked of a chat-only provider, an empty catalogue — and that the handler does not swallow upstream `JSONDecodeError` |
| `tests/test_model_name_in_stream.py` | `model` across the streaming routes | The re-framing routes report the exposed name; the `/v1/chat/completions` byte relay reports the native one (a documented limit) |
| `tests/test_startup_log.py` | `banner.log_startup` | The start-up summary describes the registry, not the `NVIDIA_*` fallback fields |
| `tests/test_config.py` | `load_settings` | The `NVIDIA_*` zero-config fallback, `providers.toml` precedence, `${ENV}` interpolation, and that `Settings` carries no model catalogue |
| `tests/test_routing.py` | `CachedRouter` | Native-vs-exposed model naming, cache bypass on streams, namespace separation, only successful JSON replies stored |
| `tests/test_model_metadata.py` | `owner_of` + discovery routes | `owned_by` / `details.family` name the serving provider, per model |
| `tests/test_stats_dashboard.py` | The `/stats` template | Data shaping (ordering, uptime, hit rate), which cards render, and autoescaping |
| `tests/test_p0_regressions.py` | The three 1.3.0 security/robustness fixes | See below |

### The regression tests

`tests/test_p0_regressions.py` exists so the fixes released in 1.3.0 cannot come
back. Each was verified to **fail** against the pre-fix code:

- **Metric cardinality + stored XSS** — an arbitrary request path must collapse
  into the single `<unmatched>` bucket rather than becoming its own metric key,
  and every dynamic value on `/stats` must come out HTML-escaped.
- **Missing credential** — no inference route may return the historical
  `500 NVIDIA_API_KEY non configurata`; a provider with no key is a start-up
  warning, and an unknown provider `type` must still fail fast.
- **Non-ASCII inbound key** — a token (or a configured `PROXY_API_KEY`) with
  non-ASCII characters must produce a `401`, not the `TypeError`-driven `500`.

## Writing a new test

`tests/conftest.py` provides the three building blocks:

- `make_settings(**overrides)` — a `Settings` with safe defaults; override any
  field by keyword. It also accepts the shorthand `models=`, `embeddings_model=`
  and `api_key=`, which describe the *default provider* rather than a `Settings`
  field — the model catalogue belongs to the registry, not to `Settings`.
- `make_provider_config(name, models=…, api_key=…)` — a `ProviderConfig` with the
  auth wrapping the config layer normally applies.
- the `app_factory` / `client` fixtures — a fully wired Flask app and test client.

To exercise a route without a network, monkeypatch `OpenAICompatibleProvider.post`
to return an `AggregatedResponse` with a canned OpenAI body — see the
`offline_upstream` fixture in `tests/test_p0_regressions.py`.

---

# Endpoint tests

[`scripts/tests.sh`](../scripts/tests.sh) exercises every endpoint and every
configured model against a running instance. It works as a **plain bash menu**
with no dependencies, and automatically upgrades to a **TUI** when
`whiptail`/`dialog` (menu) or `fzf` (model picker) are available.

> These are **integration/smoke tests**: they hit a live llmproxy and, through
> it, the real upstream provider. A valid credential must be configured and the
> server must be running.

## Prerequisites

- llmproxy running and reachable (default `http://localhost:11434` — see
  [Installation](installation.md)).
- `curl` (required).
- `jq` (optional) — for pretty-printed, field-extracted output. Without it, raw
  JSON is printed instead.
- `whiptail` or `dialog` (optional) — enables the windowed menu.
- `fzf` (optional) — enables fuzzy model selection.

## Usage

```bash
./scripts/tests.sh            # interactive menu (TUI if available, else bash)
./scripts/tests.sh 3          # run test #3 directly
./scripts/tests.sh all        # ping every exposed model
./scripts/tests.sh --list     # list the available tests
./scripts/tests.sh --no-tui   # force the plain bash menu
./scripts/tests.sh --help     # show usage
```

### Configuration

The runner is controlled by these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE` | `http://localhost:11434` | Base URL of the llmproxy instance under test. |
| `MODEL` | `meta/llama-3.1-8b-instruct` | Default model for tests that use one (and the fixed default for the vision test). |
| `EMBED_MODEL` | _(unset)_ | Model for the embeddings tests (12/13). If unset, the request omits `model` and the proxy applies its `NVIDIA_EMBEDDINGS_MODEL` default. |
| `PROXY_KEY` | _(unset)_ | Token sent by the auth test (15). If set, the test also probes `Authorization: Bearer`, `X-Api-Key`, and a wrong-token case. |

Example — test a remote instance with a specific default model:

```bash
BASE=http://gpu-host:11434 MODEL=meta/llama-3.2-1b-instruct ./scripts/tests.sh
```

## How model selection works

Tests that need a model (chat, generate, completions, …) let you **pick one from
the models llmproxy actually exposes**, fetched live from `GET /v1/models`:

- If `fzf` is installed (and TUI is not disabled), you get a fuzzy-search picker.
- Otherwise you get a numbered list in the terminal.
- If the list can't be fetched, the runner falls back to `MODEL` / the default.

This means the picker always reflects your current `NVIDIA_MODELS` configuration
(see [Configuration → Multi-model support](configuration.md#multi-model-support)).

## Available tests

| # | Test | What it checks |
|---|------|----------------|
| 1 | Discovery | `GET /api/tags` and `GET /v1/models` list all models |
| 2 | Health & root | `GET /`, `/health`, `/health?upstream=1`, `/api/version`, `/props` |
| 3 | Chat (OpenAI) | `POST /v1/chat/completions`, non-streaming |
| 4 | Chat streaming (OpenAI) | `POST /v1/chat/completions` with `stream: true` |
| 5 | Chat (Ollama) | `POST /api/chat` |
| 6 | Generate (Ollama) | `POST /api/generate` with a system prompt |
| 7 | Completions (legacy) | `POST /v1/completions` |
| 8 | Completion (llama.cpp) | `POST /completion` |
| 9 | Vision | `POST /v1/chat/completions` with an `image_url` (needs a vision model) |
| 10 | Translation | `nvidia/riva-translate-4b-instruct-v1.1` |
| 11 | Content safety | `nvidia/nemotron-3-content-safety` |
| 12 | Embeddings (OpenAI) | `POST /v1/embeddings`; reports vector dimension and first values |
| 13 | Embeddings (Ollama) | `POST /api/embed` (new) and `POST /api/embeddings` (legacy) |
| 14 | Model detail | `GET /v1/models/<id>` for a known model, plus a 404 for an unknown one |
| 15 | Authentication | Probes `/v1/models` with/without a token (`PROXY_KEY`); confirms `/health` stays exempt |
| 16 | Error propagation | Requests a non-existent model; expects the upstream status to be propagated |
| 17 | Response cache | Sends the same non-streaming request twice and shows `metrics.cache` from `/stats.json` before/after (needs `CACHE_ENABLED=on` server-side to see a hit) |
| 18 / `all` | Ping all models | Sends a tiny prompt to every exposed model and reports ✅ / ❌ |

Run `./scripts/tests.sh --list` for the current list (it is generated from the
script itself).

## Interpreting results

- **Test 17 (response cache)** issues an identical `temperature: 0` request
  twice; with `CACHE_ENABLED=on` the second is served from cache and the
  `metrics.cache.hits` counter increments (no upstream call). With caching off it
  simply shows `enabled: false`.
- **Test 18 (`all`)** prints one line per model — `✅` with the reply, or
  `❌ [status]` with the propagated provider error — and a final
  `OK: n   FAILED: m` summary. It's the fastest way to see which models in your
  `NVIDIA_MODELS` are actually reachable with your API key.
- A `❌ [404]` / `❌ [403]` usually means that model isn't enabled for your
  NVIDIA account, not a llmproxy bug — the status comes straight from the
  provider (see [Error propagation](api-reference.md#error-responses)).
- **Test 16** intentionally triggers an error to confirm propagation works; a
  non-200 status there is the expected, passing outcome.

## Notes on specific tests

- **Vision (9)** defaults to `meta/llama-3.2-11b-vision-instruct`. Override it
  with `MODEL=...` if you expose a different vision model. It sends a public
  sample image URL; the upstream must be able to fetch it.
- **Translation (10)** and **content safety (11)** hard-code their specialized
  model names. If those models aren't in your `NVIDIA_MODELS` (or not enabled
  upstream), expect a propagated error.
- **Embeddings (12/13)** need an embeddings-capable model, not a chat model. By
  default the request omits `model` so the proxy uses `NVIDIA_EMBEDDINGS_MODEL`
  (see [Configuration](configuration.md)); override with `EMBED_MODEL=...` to
  target a specific one.
- **Health upstream check (2)** calls `/health?upstream=1`, which performs a live
  `GET /models` against NVIDIA and returns `status: degraded` (HTTP 503) when the
  provider is unreachable.
- **Authentication (15)** only exercises the token paths when `PROXY_KEY` is set.
  It is informational: if the server was started **without** `PROXY_API_KEY`, the
  proxy is open and every probe returns `200`. `/health` is always exempt.
