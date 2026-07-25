# Migration guide

How to move from the single-provider **environment configuration** (`NVIDIA_*`
vars) to the declarative **multi-provider** `providers.toml` introduced in
v1.3.0 — locally and in Docker, for both development and production.

> **You may not need to migrate.** With no `providers.toml`, llmproxy keeps
> building a single provider from the `NVIDIA_*` env vars, exactly as before.
> Migrate only when you want to add providers, or prefer a declarative file.

---

## What changes

| | Before (env) | After (`providers.toml`) |
|---|---|---|
| Config source | `NVIDIA_API_BASE/KEY/MODEL(S)/EMBEDDINGS_MODEL` | `providers.toml` (path via `PROVIDERS_CONFIG`) |
| Providers | one (NVIDIA / OpenAI-compatible) | many (OpenAI-compatible, Azure, Anthropic, Gemini) |
| Exposed model names | as-is (`meta/llama-3.1-8b-instruct`) | **bare** with a single provider (unchanged); **prefixed** `provider:model` only with 2+ providers, or a per-model `alias` |
| Selection precedence | env vars | `providers.toml` when present, **else** env fallback |

**Backward compatibility for clients:** a request whose `model` is a bare native
id (e.g. `meta/llama-3.1-8b-instruct`) always resolves — it is matched against the
native ids and routed to the owning provider. With a **single** provider the
listed names stay bare too, so nothing changes. The prefix appears in the listings
only once you add a **second** provider; if your clients pin a model by its listed
name, either switch them to `provider:model` or set an `alias` in the TOML to keep
the old bare name.

---

## Step 1 — Generate `providers.toml` from your env

The `env_to_toml` tool reads the current environment (honouring `.env`) and emits
a `providers.toml` with the NVIDIA provider filled in plus commented stubs for the
other provider types. Secrets are written as `${ENV_VAR}` references, never
inlined, so the file is safe to commit.

**Locally:**

```bash
make migrate-config                       # writes ./providers.toml
# equivalents:
python -m llmproxy.scripts.env_to_toml    # ./providers.toml (won't overwrite)
python -m llmproxy.scripts.env_to_toml providers.dev.toml --force   # custom path
python -m llmproxy.scripts.env_to_toml -  # print to stdout (no file written)
```

**From inside Docker** (no local Python needed; writes to the host via stdout, so
there are no in-container permission issues):

```bash
docker compose run --rm --no-TTY llmproxy \
  python -m llmproxy.scripts.env_to_toml - > providers.toml
# or:
make migrate-config-docker
```

## Step 2 — Review and extend it

Open `providers.toml` and:

- Keep the NVIDIA block (or edit its `models` / `embeddings_models`).
- Uncomment and fill in any other providers you want — see
  [`providers.toml.example`](../providers.toml.example) for every supported type
  and its base URL.
- Add the corresponding API keys to `.env` (referenced as `${ENV_VAR}`).
- Resolve any **name collisions**: the same model served by two providers must
  differ once exposed — either rely on the automatic `provider:` prefix or give
  one an `alias`. Two models resolving to the same exposed name is a start-up
  error.

Example — the same `llama-3.3-70b` from two providers:

```toml
[[provider]]
name = "nvidia"
type = "openai_compatible"
base_url = "https://integrate.api.nvidia.com/v1"
api_key = "${NVIDIA_API_KEY}"
models = ["llama-3.3-70b"]            # exposed as nvidia:llama-3.3-70b

[[provider]]
name = "cerebras"
type = "openai_compatible"
base_url = "https://api.cerebras.ai/v1"
api_key = "${CEREBRAS_API_KEY}"
models = [{ id = "llama-3.3-70b", alias = "fast-70b" }]   # exposed as fast-70b
```

## Step 3 — Point llmproxy at the file

Set `PROVIDERS_CONFIG` to the file path. It defaults to `providers.toml` in the
working directory, so if the file sits there, it is picked up automatically.

```bash
# local
PROVIDERS_CONFIG=providers.toml python main.py
```

## Step 4 — Verify

```bash
curl -s localhost:11434/health            # -> "providers": N, "models": M
curl -s 'localhost:11434/health?upstream=1' | jq   # per-provider reachability
curl -s localhost:11434/v1/models | jq '.data[].id'   # union, prefixed names
```

A quick end-to-end call (bare id still works, prefixed id is canonical):

```bash
curl -s localhost:11434/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"nvidia:meta/llama-3.1-8b-instruct","messages":[{"role":"user","content":"ping"}]}'
```

---

## Docker: development

The bundled [`docker-compose.yml`](../docker-compose.yml) ships the multi-provider
lines commented out. Uncomment them to mount the file and set the variable:

```yaml
    environment:
      PROVIDERS_CONFIG: /config/providers.toml
    volumes:
      - ./providers.toml:/config/providers.toml:ro
```

```bash
docker compose up -d
```

## Docker: production

Two ready-made production compose files are provided; pick the one that matches
your setup. Both use the prebuilt image (`lordraw/llmproxy:${LLMPROXY_VERSION:-latest}`),
bind the port to loopback (put a TLS reverse proxy in front), enable log rotation
and a memory limit, and expect `PROXY_API_KEY` set in `.env` for inbound auth.

### A) Single provider from env — [`docker-compose.prod.env.yml`](../docker-compose.prod.env.yml)

No `providers.toml`. The `NVIDIA_*` vars in `.env` drive a single upstream.

```bash
docker compose -f docker-compose.prod.env.yml up -d
```

### B) Multi-provider from TOML — [`docker-compose.prod.toml.yml`](../docker-compose.prod.toml.yml)

Mounts `./providers.toml` read-only at `/config/providers.toml` and sets
`PROVIDERS_CONFIG`. Keys still come from `.env` via `${ENV_VAR}` refs.

```bash
# 1. Generate + edit providers.toml (see Step 1–2), then:
docker compose -f docker-compose.prod.toml.yml up -d
```

Pin the image for reproducible deploys:

```bash
LLMPROXY_VERSION=1.3.0 docker compose -f docker-compose.prod.toml.yml up -d
```

### `docker run` equivalent (TOML)

```bash
docker run -d --name llmproxy --restart unless-stopped \
  -p 127.0.0.1:11434:11434 \
  --env-file .env \
  -e PROVIDERS_CONFIG=/config/providers.toml \
  -v "$PWD/providers.toml:/config/providers.toml:ro" \
  lordraw/llmproxy:latest
```

---

## Rolling back

`providers.toml` takes precedence only when the file is present and
`PROVIDERS_CONFIG` resolves to it. To revert to the env behavior, either remove
the file / unset `PROVIDERS_CONFIG` (local), or switch back to
`docker-compose.prod.env.yml` (Docker). No data migration is involved — llmproxy
is stateless.

## Troubleshooting

- **`model name collision: '<name>' is exposed by both …`** — two models resolve
  to the same exposed name. Give one an `alias`, or remove the duplicate.
- **`reading providers.toml requires the 'tomli' package on Python < 3.11`** —
  only affects local Python 3.9/3.10 runs; `pip install tomli` (already pinned in
  `requirements.txt`). The Docker image runs Python 3.12, which has `tomllib`.
- **A provider is unreachable** — check `GET /health?upstream=1`; it reports each
  provider's status individually (`ok` / `error:<code>` / `unreachable`).
- **Client can't find a model** — it may be pinning the old bare name that is no
  longer *listed*. Use the prefixed `provider:model`, or add an `alias`. Bare
  native ids still resolve for inference even when not listed.

See also [Configuration → Multi-provider](configuration.md#multi-provider) and
[Deployment](deployment.md).
