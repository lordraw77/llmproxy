# Troubleshooting

## `{"error": "NVIDIA_API_KEY non configurata nel file .env"}` (HTTP 500)

The `NVIDIA_API_KEY` environment variable is empty. llmproxy returns this on
every inference endpoint when the key is missing.

**Fix:**

1. Set `NVIDIA_API_KEY` in `.env`.
2. Restart the service (`docker compose restart` or re-run `python main.py`) —
   configuration is only read at startup.

## The client receives a provider error (401, 403, 404, 429, …)

llmproxy **propagates provider errors**: the upstream HTTP status code is
preserved and the provider's JSON error body is forwarded to the client
unchanged. So the status code and message you see come directly from NVIDIA and
tell you what went wrong.

Common causes:

| Upstream status | Likely cause | Fix |
|-----------------|--------------|-----|
| 401 unauthorized | Invalid or revoked API key | Check `NVIDIA_API_KEY` |
| 403 forbidden | Key lacks access to the model | Use a permitted `NVIDIA_MODEL` |
| 404 model not found | `NVIDIA_MODEL` name is wrong | Correct the model name |
| 429 rate limit | Quota or rate limit exceeded | Slow down / check your NVIDIA plan |

If the upstream body is not JSON, llmproxy wraps the raw text in an `error`
object (`type: "upstream_error"`) while still preserving the status code.

## `502 {"error": {"message": ..., "type": "upstream_request_error"}}`

llmproxy could not get any response from the provider. This covers connection
failures, DNS errors, and timeouts (the upstream call has a 120s timeout, so a
very large or slow request can exceed it).

**Fix:** check network/DNS connectivity to `NVIDIA_API_BASE`, then retry.

## The client can't reach the server / connection refused

- Confirm the container/process is running:
  `docker compose ps` or check the `python main.py` process.
- Confirm the port matches: llmproxy listens on `PORT` (default `11434`), and
  Docker publishes `${PORT}:${PORT}`. A client pointed at the wrong port fails.
- If a **real Ollama** is already using `11434`, there will be a port conflict.
  Stop it, or set a different `PORT` for llmproxy ([Configuration](configuration.md)).

## My model choice is ignored / a different model responds

llmproxy honors the requested `model` only when it matches one of the models it
exposes (`NVIDIA_MODELS`, or the single `NVIDIA_MODEL`). An unrecognized model
name silently falls back to the default (the first entry) instead of erroring.

Check that:

- the model name you send exactly matches one of the entries in `NVIDIA_MODELS`
  (as shown by `GET /api/tags` or `GET /v1/models`);
- you restarted llmproxy after editing `NVIDIA_MODELS` (config is read at startup).

## My models don't appear in Open WebUI's picker

Open WebUI populates its model list from `/api/tags` (Ollama connection) or
`/v1/models` (OpenAI connection). Confirm those endpoints return all your models
(`curl http://localhost:11434/api/tags`). If only one shows up, verify
`NVIDIA_MODELS` is set (comma-separated) and that you restarted after changing
it.

## My sampling parameters have no effect

On the Ollama endpoints (`/api/chat`, `/api/generate`), `/v1/completions`, and
`/completion`, only `temperature` and `top_p` are forwarded upstream. All other
parameters are dropped.

If you need full parameter pass-through (`max_tokens`, `stop`, `tools`, …), use
the **`/v1/chat/completions`** endpoint, which forwards the whole payload
verbatim.

## Streaming output arrives all at once

Something between the client and llmproxy is buffering the stream:

- With `curl`, add `-N` to disable client-side buffering.
- Behind a reverse proxy, disable response buffering for streaming routes
  (e.g. nginx `proxy_buffering off;`). See [Deployment](deployment.md).

## Token usage is always zero

llmproxy does not compute token counts. `/v1/completions` and `/completion`
report `0` for all usage/token fields. This is by design and not an error.

## The Docker healthcheck shows `unhealthy`

The healthcheck polls `GET /` on the container's `PORT`. If it fails:

- Check logs: `docker compose logs -f llmproxy`.
- Ensure the app actually started (a Python import error would prevent it).
- Ensure `PORT` inside the container matches what the healthcheck expects (it
  reads the same env var, so they should agree unless overridden oddly).

## Changes to `.env` don't take effect

Configuration is read once, at startup. Restart after any change:

```bash
docker compose restart llmproxy
```

## Seeing the startup banner

On a healthy local start you should see:

```
llmproxy in ascolto su http://0.0.0.0:11434 -> NVIDIA model: meta/llama-3.1-8b-instruct
```

If instead you see `Attenzione: NVIDIA_API_KEY non impostata in .env`, the key
is missing and inference calls will fail until you set it.
