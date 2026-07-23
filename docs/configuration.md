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
| `NVIDIA_API_BASE` | `https://integrate.api.nvidia.com/v1` | No | Base URL of the upstream OpenAI-compatible API. Change it to target a different compatible endpoint. |
| `NVIDIA_API_KEY` | *(empty)* | **Yes** | Bearer token sent to the upstream API. Without it, every inference endpoint returns HTTP 500. |
| `NVIDIA_MODEL` | `meta/llama-3.1-8b-instruct` | No | Single-model / default model. Used as the fallback default when `NVIDIA_MODELS` is not set. |
| `NVIDIA_MODELS` | value of `NVIDIA_MODEL` | No | **Comma-separated list** of models to expose. All of them appear in the discovery endpoints (so they show up in Open WebUI's model picker). The **first entry is the default**. See [Multi-model support](#multi-model-support). |

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
```

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

## Notes and behavior

### Configuration is read once, at startup

Environment variables are read into module-level constants when `main.py` is
imported. **Changing `.env` requires a restart** (`docker compose restart` or
re-running `python main.py`) to take effect.

### Model selection

The discovery endpoints — `/api/tags` and `/v1/models` — report every model in
`NVIDIA_MODELS` (or the single `NVIDIA_MODEL` if that list is unset). For each
inference request, the client's requested `model` is used when it matches one of
the exposed models, otherwise the default (first entry) is used. See
[Multi-model support](#multi-model-support) above.

### Only a subset of sampling options is forwarded

For the Ollama, `/v1/completions`, and `/completion` endpoints, only
`temperature` and `top_p` are passed upstream. Other sampling parameters are
dropped. The `/v1/chat/completions` endpoint is the exception — it forwards the
entire request payload (see [API Reference](api-reference.md)).

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
- llmproxy itself performs **no authentication** on inbound requests. Anyone who
  can reach the port can consume your NVIDIA quota. Do not expose it directly to
  the public internet — put it behind a reverse proxy / firewall / VPN. See
  [Deployment](deployment.md).
