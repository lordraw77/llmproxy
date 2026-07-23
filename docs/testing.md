# Testing

llmproxy ships with a single test runner, [`scripts/tests.sh`](../scripts/tests.sh),
that exercises every endpoint and every configured model against a running
instance. It works as a **plain bash menu** with no dependencies, and
automatically upgrades to a **TUI** when `whiptail`/`dialog` (menu) or `fzf`
(model picker) are available.

> These are **integration/smoke tests**: they hit a live llmproxy and, through
> it, the real NVIDIA API. A valid `NVIDIA_API_KEY` must be configured and the
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
| 17 / `all` | Ping all models | Sends a tiny prompt to every exposed model and reports ✅ / ❌ |

Run `./scripts/tests.sh --list` for the current list (it is generated from the
script itself).

## Interpreting results

- **Test 17 (`all`)** prints one line per model — `✅` with the reply, or
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
