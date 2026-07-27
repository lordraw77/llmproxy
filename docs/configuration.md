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
| `PROVIDERS_CONFIG` | `providers.toml` | No | Path to the declarative [multi-provider](#multi-provider) config. When the file exists it defines the providers and supersedes the `NVIDIA_*` vars below; when it is absent, a single NVIDIA provider is synthesized from those vars (zero-config fallback). |
| `NVIDIA_API_BASE` | `https://integrate.api.nvidia.com/v1` | No | Base URL of the upstream OpenAI-compatible API. Change it to target a different compatible endpoint. Used only by the env fallback (no `providers.toml`). |
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
| `CACHE_ENABLED` | `false` | No | When truthy (`1`/`true`/`yes`/`on`), enables the **response cache**: identical **non-streaming** requests are served from memory, skipping the upstream call. Streaming requests are never cached. See [Response caching](#response-caching). |
| `CACHE_TTL` | `300` | No | Time-to-live, in seconds, of each cache entry. After it elapses the entry expires and the next identical request goes upstream again. A non-positive value disables the cache. |
| `CACHE_MAX_SIZE` | `512` | No | Maximum number of entries kept in the cache. Once the cap is reached the **least-recently-used** entry is evicted to make room. A non-positive value disables the cache. |
| `CACHE_POLICY` | `deterministic` | No | Which requests are **eligible** for the cache once it is enabled: `off`, `embeddings`, `deterministic`, or `all`. The default only caches replies that can have a single correct value (embeddings, or completions with a `seed` / `temperature: 0`), so enabling the cache never silently replays a sampled answer. An unrecognized value falls back to `deterministic`. See [Cache eligibility](#cache-eligibility). |
| `AUDIT_ENABLED` | `false` | No | When truthy, writes one structured record per request to `AUDIT_FILE`: prompt, completion, parameters, provider, tokens, timings and session. The record is built and written by a background thread, so it adds no latency to the request. See [Audit trail](audit.md). |
| `AUDIT_FILE` | `logs/audit.jsonl` | No | Destination of the audit records (the directory is created if missing). Supports `{pid}` and `{date}` placeholders — use `{pid}` when running several gunicorn workers, which cannot coordinate rotation on a shared file. |
| `AUDIT_FORMAT` | `jsonl` | No | `jsonl` (one record per line, for `tail`/`jq`) or `pretty` (indented, for reading directly). |
| `AUDIT_BODIES` | `truncated` | No | How much content each record holds: `none` (accounting only: no prompts, no completions), `truncated` (clipped to `AUDIT_MAX_CHARS`), `full` (uncapped). The file contains conversations in clear text unless this is `none`. |
| `AUDIT_MAX_CHARS` | `2000` | No | Character budget per captured text (each message, and the completion) under `truncated`. |
| `AUDIT_QUEUE_SIZE` | `10000` | No | How many records may wait for the writer. When the queue is full a record is **dropped and counted** rather than making the request wait; drops are reported at `/stats`. |
| `AUDIT_MAX_MB` | `64` | No | Size at which the audit file rotates. A record is never split, so the file may exceed the cap by at most one record. |
| `AUDIT_BACKUPS` | `5` | No | Rotated audit files kept (`.1` … `.5`). Total disk used is at most `AUDIT_MAX_MB × (AUDIT_BACKUPS + 1)`. |
| `AUDIT_SESSION_HEADER` | *(empty)* | No | Extra request header consulted first for the conversation id, before the built-in `X-Session-Id` / `X-Conversation-Id` / `X-Chat-Id` / `X-Request-Session`. Without any of them the session is fingerprinted from the conversation's opening message. |
| `WEB_CONCURRENCY` / `THREADS` / `GUNICORN_TIMEOUT` | `2` / `8` / `600` | No | gunicorn tuning (Docker image only). Workers, threads per worker, and worker timeout. |
| `UPSTREAM_POOL_SIZE` | value of `THREADS`, else `8` | No | Size of the pooled HTTP connections kept open towards each upstream. Size it on the threads that actually issue requests: below `THREADS`, concurrent calls queue for a free connection. |

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

`NO_PROXY` is matched against each provider's `base_url` when its session is
built: a provider whose host is excluded is given no proxy at all and is reached
directly. Host suffixes (`.internal.example.com`), bare hosts, IP addresses, CIDR
blocks and `*` all work. The exclusion applies to a provider's own `proxy` entry
too — a host listed in `NO_PROXY` is never proxied, whatever the source of the
proxy setting.

> This exclusion is applied by llmproxy, not by `requests`. `requests` honors
> `NO_PROXY` only for proxies it discovers in the environment on its own; a proxy
> set explicitly on a session — which is what `HTTP_PROXY`/`HTTPS_PROXY` produce
> here — is used for every URL, and any `no_proxy` key in that mapping is
> ignored. Up to and including 1.3.0 that made `NO_PROXY` inert for upstream
> calls: setting an egress proxy sent even a provider on `127.0.0.1` through it.

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

## Response caching

llmproxy can memoize **non-streaming** upstream replies in an in-memory cache, so
repeated identical requests are answered instantly without a round-trip to the
provider — cutting latency and, for metered upstreams, token cost. It is
**disabled by default**; enable it with:

```dotenv
CACHE_ENABLED=on
CACHE_TTL=300                 # entry time-to-live in seconds (default 300)
CACHE_MAX_SIZE=512            # max entries; LRU eviction past the cap (default 512)
CACHE_POLICY=deterministic    # which requests are eligible (default deterministic)
```

How it works:

- **What is cached** — successful (`2xx`), **non-streaming** chat/text completions
  (`/api/chat`, `/api/generate`, `/v1/chat/completions`, `/v1/completions`,
  `/completion`) and embeddings (`/v1/embeddings`, `/api/embeddings`, `/api/embed`),
  **subject to `CACHE_POLICY`** (see [Cache eligibility](#cache-eligibility)).
  **Streaming responses are never cached** — they are consumed incrementally and
  cannot be replayed.
- **Cache key** — a SHA-256 of the canonicalized payload actually sent upstream
  (model + messages/prompt + forwarded sampling parameters, or the embeddings
  input). Any difference in those fields — a changed prompt, `temperature`,
  `max_tokens`, `seed`, model, etc. — is a different key and misses the cache.
- **TTL** — each entry expires `CACHE_TTL` seconds after it is stored; an expired
  entry is discarded on the next lookup and the request goes upstream again.
- **Size / eviction** — the cache holds at most `CACHE_MAX_SIZE` entries. When the
  cap is exceeded the least-recently-used entry is evicted (classic LRU).
- **Observability** — hit/miss counts, hit rate, live entry count, stores,
  evictions, expirations and policy `skipped` counts are reported under `metrics.cache` at `/stats.json`
  and on the `/stats` dashboard. See [Statistics & metrics](api-reference.md#statistics).

> **Per-worker, in-memory.** Like the metrics, the cache lives in each worker
> process and is **not shared** across gunicorn workers or persisted to disk. With
> `N` workers a request may hit whichever worker holds the entry; the reported hit
> rate is therefore a per-worker figure. A restart clears the cache.

### Cache eligibility

A cache hit replays a stored answer. For an embedding that is simply a saved
round-trip: the same input maps to the same vector by construction. For a
completion sampled with `temperature > 0` it is a **behavioral change** — two
identical requests are supposed to produce different text, and within the TTL
they no longer do.

`CACHE_POLICY` makes that trade-off explicit instead of bundling it into
`CACHE_ENABLED`. Levels, from strictest to loosest:

| `CACHE_POLICY` | Embeddings | Completions | Use it when |
|---|---|---|---|
| `off` | never | never | You want the cache wired and its counters visible on `/stats`, but every request to go upstream — e.g. while A/B-testing the cache's effect, or to disable it for one deployment without editing the rest of the configuration. |
| `embeddings` | cached | never | RAG-style workloads: re-embedding the same corpus is pure waste, but every generation must be fresh. |
| `deterministic` **(default)** | cached | only when the reply can have one value: an explicit `seed`, or `temperature: 0` **and** `top_p: 1` | The safe default. Enabling the cache never changes what a sampled request returns. |
| `all` | cached | always | Cost/latency above variety — demos, evaluation harnesses replaying a fixed prompt set, metered upstreams. **You are accepting that identical prompts return identical text for the whole TTL.** |

Notes on `deterministic`:

- A `seed` counts as deterministic **regardless of temperature** — that is exactly
  what a seed is for. Note that most upstreams treat `seed` as best-effort.
- Absent sampling fields take the OpenAI defaults (`temperature: 1`, `top_p: 1`),
  so a request that specifies neither is **not** eligible. This is the common
  case: a plain client gets fresh generations by default.
- `temperature: 0` with a restricted `top_p` (e.g. `0.9`) is not eligible: the
  nucleus still admits more than one continuation.

Requests turned away by the policy are counted as `skipped` in `metrics.cache`.
A hit rate of zero alongside a growing `skipped` means the policy is doing its
job, not that the cache is broken — raise the level if that is not what you want.

> **Tightening the policy takes effect immediately.** Eligibility is checked
> *before* the lookup, so entries stored under `all` are never served to a request
> that `deterministic` rejects — no flush and no restart needed. They simply sit
> there until the TTL or the LRU cap drops them.

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

## Multi-provider

Beyond the single NVIDIA upstream, llmproxy can serve **several providers at
once**, each with its own credentials, base URL, and set of models. Providers are
declared in a TOML file pointed at by `PROVIDERS_CONFIG` (default
`providers.toml`); see [`providers.toml.example`](../providers.toml.example).

```toml
[[provider]]
name = "nvidia"
type = "openai_compatible"
base_url = "https://integrate.api.nvidia.com/v1"
api_key = "${NVIDIA_API_KEY}"
models = ["meta/llama-3.1-8b-instruct"]
embeddings_models = ["nvidia/nv-embedqa-e5-v5"]

[[provider]]
name = "anthropic"
type = "anthropic"
api_key = "${ANTHROPIC_API_KEY}"
models = ["claude-opus-4-8", "claude-sonnet-5"]
```

**Provider types**

| `type` | Upstream | Default `base_url` | Auth |
|--------|----------|--------------------|------|
| `openai_compatible` | NVIDIA, vLLM, Groq, OpenRouter, LM Studio, local Ollama/llama.cpp… | *(required)* | `Authorization: Bearer` |
| `openai` | OpenAI | `https://api.openai.com/v1` | `Authorization: Bearer` |
| `mistral` | Mistral | `https://api.mistral.ai/v1` | `Authorization: Bearer` |
| `azure` | Azure OpenAI | *(required, resource root)* | `api-key` header + `api-version` |
| `anthropic` | Anthropic (Claude) | `https://api.anthropic.com` | `x-api-key` + `anthropic-version` |
| `gemini` | Google Gemini | `…/v1beta` generativelanguage API | `x-goog-api-key` |

`anthropic` and `gemini` are translated natively to and from the OpenAI shape
(request, response, and streaming), so clients keep speaking OpenAI/Ollama.

For both native providers the translation covers a full conversation, not just
the first turn — a tool-calling round-trip survives it. The request-side half
lives in `llmproxy/providers/translate/`, free of HTTP and unit-tested.

| | `gemini` | `anthropic` |
|---|---|---|
| Assistant tool calls | `functionCall` part | `tool_use` block |
| Tool result (`role="tool"`) | `functionResponse` part in a user turn, paired **by function name** — Gemini does not use the OpenAI call id | `tool_result` block in a user turn, paired by `tool_use_id` |
| Text blocks | `parts[].text` | `text` blocks |
| `data:` image URIs | `inlineData` (also audio, via `input_audio`) | `image` / `base64` source |
| Remote image URIs | `fileData` — Gemini does not fetch arbitrary URLs, so this fails explicitly rather than dropping the image | `image` / `url` source — Anthropic fetches it |
| Consecutive same-role turns | merged (only `user` and `model` exist) | merged — which is also what keeps parallel tool results in a single turn |
| Unsupported block types | dropped, never stringified | dropped, never stringified |

> **`anthropic` is not validated against the live API.** The translation is
> pinned by unit tests on the shape of the request body; no Anthropic credential
> was available to exercise a real tool-calling round-trip. Treat it as a
> declared residual risk. The `gemini` side *was* validated end-to-end.

**Per-provider keys**: `name`, `type`, `base_url`, `api_key`, `models`,
`embeddings_models`, `timeout`, `api_version` (Azure), `max_tokens` (Anthropic
default), `proxy`, and `extra_headers`. Use `${ENV_VAR}` in any string to
interpolate from the environment (keeps secrets out of the file).

A reference to a variable that is not set expands to the empty string — it does
not stop start-up — but every such name is listed once in a start-up warning:

```
providers.toml references environment variables that are not set: GROQ_TOKEN.
They expanded to an empty string — a provider whose api_key ended up empty will
fail with 401 on its first request.
```

A variable that is set but empty is a deliberate choice and is not reported.

**Model naming and routing**

- Every provider's models are exposed together — the **union** — through
  `/v1/models` and `/api/tags`.
- With a **single** provider configured, exposed names stay **bare**
  (`meta/llama-3.1-8b-instruct`) — identical to the pre-v1.3.0 behaviour. The
  `provider:model` prefix is applied **only when two or more providers coexist**
  and names need disambiguating (e.g. `nvidia:meta/llama-3.1-8b-instruct`). A
  model's `alias` always overrides the exposed name, in either case. A `models`
  entry is either a bare string or a table: `{ id = "llama-3.3-70b", alias = "fast-70b" }`.
- This makes the **same model served by two providers** unambiguous
  (`cerebras:llama-3.3-70b` vs. `nvidia:llama-3.3-70b`). Two models resolving to
  the same exposed name is a start-up error.
- A request's `model` is matched against the exposed names first, then against
  bare native ids (so a client sending `meta/llama-3.1-8b-instruct` still works);
  the request is routed to the owning provider and the native id is sent upstream.

**Backward compatibility**: with no `providers.toml`, a single `nvidia`
provider is built from the `NVIDIA_*` env vars — existing deployments upgrade with
zero config.

### What stays in `.env` vs. what goes in `providers.toml`

The two files have **different jobs** and are used together:

- **`.env` — process-global settings and secret _values_.** Everything that is
  not specific to one provider stays here, plus the actual API-key values (the
  TOML only *references* them). These apply to the whole proxy regardless of how
  many providers you run.
- **`providers.toml` — the upstreams and their per-provider settings.** Which
  providers exist, their base URLs, which models they serve, and any per-provider
  overrides.

| Setting | Where | Notes |
|---------|-------|-------|
| `HOST`, `PORT` | **.env** | Server bind address. |
| `LOG_LEVEL`, `LOG_TZ` / `TZ` | **.env** | Logging / clock. |
| `PROXY_API_KEY` | **.env** | Inbound auth (global). |
| `CACHE_ENABLED`, `CACHE_TTL`, `CACHE_MAX_SIZE`, `CACHE_POLICY` | **.env** | Response cache (global). |
| `RETRY_MAX`, `RETRY_BACKOFF` | **.env** | Retry policy (applies to every provider). |
| `FORCE_UPSTREAM_STREAM` | **.env** | Force-stream re-aggregation (global). |
| `EMBEDDINGS_INPUT_TYPE` | **.env** | Default embeddings `input_type` (global). |
| `UPSTREAM_TIMEOUT` | **.env** | **Default** read timeout; a provider's `timeout` overrides it. |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | **.env** | **Default** egress proxy; a provider's `proxy` overrides it. `NO_PROXY` excludes hosts from **both**. |
| `UPSTREAM_POOL_SIZE` / `THREADS`, `WEB_CONCURRENCY`, `GUNICORN_TIMEOUT` | **.env** | Pool / gunicorn tuning. |
| `PROVIDERS_CONFIG` | **.env** | Path to the TOML itself. |
| **API-key values** (`NVIDIA_API_KEY`, `ANTHROPIC_API_KEY`, …) | **.env** | Referenced from the TOML as `${ENV_VAR}` — never inline the value. |
| `name`, `type`, `base_url` | **providers.toml** | Provider identity + endpoint. |
| `api_key` | **providers.toml** | A `${ENV_VAR}` reference, resolved from `.env`. |
| `models`, `embeddings_models` (+ `alias`) | **providers.toml** | Per-provider model lists / exposed names. |
| `timeout` | **providers.toml** | Per-provider read timeout (else `UPSTREAM_TIMEOUT`). |
| `proxy` | **providers.toml** | Per-provider egress proxy (else the global one). |
| `api_version` | **providers.toml** | Azure only. |
| `max_tokens` | **providers.toml** | Anthropic default (required by its API). |
| `extra_headers` | **providers.toml** | Extra headers merged into every request. |

**Rule of thumb:** if the setting answers *"how does the proxy behave overall?"*
it belongs in `.env`; if it answers *"which upstream, which models, with which
credentials/limits?"* it belongs in `providers.toml`. **Secrets are the split
case** — the *value* lives in `.env`, the *reference* (`${…}`) lives in the TOML.

> When `providers.toml` is active, the `NVIDIA_API_BASE` / `NVIDIA_MODEL(S)` /
> `NVIDIA_EMBEDDINGS_MODEL` vars are **ignored** — those move into the NVIDIA
> provider block. Only `NVIDIA_API_KEY` stays relevant, as the value behind
> `api_key = "${NVIDIA_API_KEY}"`.

Minimal split, one provider:

```dotenv
# .env — globals + the key value
PORT=11434
PROXY_API_KEY=change-this
CACHE_ENABLED=on
UPSTREAM_TIMEOUT=180
NVIDIA_API_KEY=nvapi-xxxxxxxx
PROVIDERS_CONFIG=providers.toml
```

```toml
# providers.toml — the upstream + models
[[provider]]
name = "nvidia"
type = "openai_compatible"
base_url = "https://integrate.api.nvidia.com/v1"
api_key = "${NVIDIA_API_KEY}"   # value comes from .env
models = ["meta/llama-3.1-8b-instruct"]
embeddings_models = ["nvidia/nv-embedqa-e5-v5"]
timeout = 300                    # this provider only; overrides UPSTREAM_TIMEOUT
```

### Migrating the env config to `providers.toml`

The `env_to_toml` tool reads your current environment (honouring `.env`) and
writes a `providers.toml` with the NVIDIA provider filled in and commented stubs
for every other provider type. Secrets are emitted as `${ENV_VAR}` references, so
the file stays safe to commit.

**Locally:**

```bash
make migrate-config                       # writes ./providers.toml
# equivalently:
python -m llmproxy.scripts.env_to_toml    # ./providers.toml (refuses to overwrite)
python -m llmproxy.scripts.env_to_toml providers.dev.toml --force   # custom path
python -m llmproxy.scripts.env_to_toml -  # print to stdout instead of writing a file
```

**From inside Docker** — the container image ships the tool, so you can generate
the file straight from the same `.env` the service uses. Writing to stdout avoids
any host-filesystem permission issues:

```bash
# Generate ./providers.toml on the host, from the container, using .env:
docker compose run --rm --no-TTY llmproxy \
  python -m llmproxy.scripts.env_to_toml - > providers.toml
# or simply:
make migrate-config-docker
```

Then enable it in `docker-compose.yml` (uncomment the `environment` and `volumes`
blocks, which mount `./providers.toml` at `/config/providers.toml` and set
`PROVIDERS_CONFIG`) and restart:

```yaml
    environment:
      PROVIDERS_CONFIG: /config/providers.toml
    volumes:
      - ./providers.toml:/config/providers.toml:ro
```

```bash
docker compose up -d
docker compose exec llmproxy sh -c 'curl -s localhost:$PORT/health'   # providers: N
```

For a plain `docker run`, bind-mount the file and set the variable:

```bash
docker run -d -p 11434:11434 --env-file .env \
  -v "$PWD/providers.toml:/config/providers.toml:ro" \
  -e PROVIDERS_CONFIG=/config/providers.toml \
  lordraw/llmproxy:latest
```

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
