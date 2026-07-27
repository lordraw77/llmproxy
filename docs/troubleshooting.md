# Troubleshooting

## `Provider senza credenziale configurata: <name>` (startup warning)

A configured provider has no API credential. llmproxy logs this once per
provider at startup and keeps serving: a local upstream (Ollama, vLLM, LM Studio)
legitimately needs no key. If the provider *does* require one, its calls fail with
the upstream's own `401`, which is propagated to the client.

**Fix:**

1. Set the provider's key — `NVIDIA_API_KEY` in `.env` for the env-var
   configuration, or the `api_key` field of the `[[provider]]` block in
   `providers.toml`.
2. If `api_key` uses a `${VAR}` reference, check that `VAR` is actually exported:
   an unresolved reference expands to an empty string silently.
3. Restart the service (`docker compose restart` or re-run `python main.py`) —
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

## `401 {"error": {"message": "unauthorized", ...}}`

Inbound authentication is enabled (`PROXY_API_KEY` is set) and the request did
not present a valid key.

**Fix:** send the key via `Authorization: Bearer <key>` or `X-Api-Key: <key>`.
`/` and `/health` are always exempt. To disable auth, unset `PROXY_API_KEY` and
restart.

## `413 {"error": {..., "code": "request_too_large"}}`

The request body exceeded `MAX_REQUEST_MB` (default `32`), so it was refused
before any route saw it. The body is buffered in memory before routing, and the
cap exists so that one caller cannot make a worker hold an unbounded amount of it
— multiplied by every thread accepting at once.

**Fix:** if the request is legitimate — a multimodal prompt carrying several
large base64 images is the usual case — raise `MAX_REQUEST_MB` and restart.
Setting it to `0` removes the limit entirely (the behaviour before 1.4.1).
Sending images by URL rather than inline avoids the problem altogether, and is
cheaper on every hop.

## Requests queue under load / the proxy "goes slow" with many clients

Response times climb as concurrency rises, the upstream is not the bottleneck,
and the host looks idle. The proxy is **full**, not slow.

`WEB_CONCURRENCY × THREADS` is a hard ceiling on requests in flight — 64 with the
current defaults (`2 × 32`), and 16 in versions before 1.4.1. Request `N+1` waits
in the socket backlog until a thread frees up, and since a completion holds its
thread for the whole generation (tens of seconds for a long answer), a handful of
slow streams is enough to fill it.

**Check:** `in_flight` at `/stats` sitting at `THREADS` (per worker) is the
symptom. Remember the figure is per-worker, so compare it against `THREADS`, not
against the product.

**Fix:** raise `THREADS`. It is nearly free for this workload — a request spends
its life blocked reading from the upstream, using no CPU — so the usual "threads
≈ cores" rule does not apply. Leave `UPSTREAM_POOL_SIZE` unset so it follows
`THREADS`: pinned lower, urllib3 discards the connections above the pool size and
every discard costs the next request a fresh TLS handshake with the provider
(look for `Connection pool is full, discarding connection`). See
[Concurrency and pool sizing](configuration.md#concurrency-and-pool-sizing).

## `502 {"error": {"message": ..., "type": "upstream_request_error"}}`

llmproxy could not get any response from the provider. This covers connection
failures, DNS errors, and timeouts (the upstream call uses `UPSTREAM_TIMEOUT`,
default 120s, so a very large or slow request can exceed it). Transient failures
(network errors, `429`, `5xx`) are retried first per `RETRY_MAX`/`RETRY_BACKOFF`;
a `502` means every attempt failed.

### Read timeout on non-streaming requests

The most common cause is a **non-streaming** request (`stream: false`) to a slow
model. In the logs it looks like this:

```
--> POST /v1/chat/completions | model=nvidia/nemotron-... stream=False
<- NVIDIA no-response after 120112ms (tentativo 1/3) ... Read timed out
<- NVIDIA no-response after 120098ms (tentativo 2/3) ... Read timed out
<- NVIDIA no-response after 120129ms ... Read timed out
<-- POST /v1/chat/completions | status=502 duration=361847ms
```

With `stream: false` the provider sends **no bytes** until the whole completion
is ready, so the read timeout trips even though the same model streams fine via
`curl` with `stream: true`. Note the ~6-minute total: a read timeout is retried,
so the wait is `(RETRY_MAX + 1) × UPSTREAM_TIMEOUT`.

**Fix (recommended):** set `FORCE_UPSTREAM_STREAM=on`. The proxy then always
streams towards the provider and re-aggregates the result, so a non-streaming
client still gets its single JSON response but the read timeout no longer trips.
See [Forcing upstream streaming](configuration.md#forcing-upstream-streaming).

**Fix (alternative):** raise `UPSTREAM_TIMEOUT` (e.g. `300`) to give slow
generations more headroom.

### Hangs with no `<- NVIDIA response` line (egress proxy)

If the log shows the `-> NVIDIA payload` but **never** a `<- NVIDIA response`,
and eventually times out, the host likely reaches the internet only through a
corporate proxy. A `curl` from the host works (the shell inherits the proxy
variables) but the container connects directly and stalls. Set
`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in `.env` — see
[Outbound proxy](configuration.md#outbound-proxy).

**Other 502 causes:** check network/DNS connectivity to `NVIDIA_API_BASE`, then
retry.

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
`/completion`, a normalized subset is forwarded upstream: `temperature`,
`top_p`, `max_tokens`, `stop`, `presence_penalty`, `frequency_penalty`, `seed`,
`n` (with `num_predict`/`n_predict` mapped to `max_tokens`). Parameters outside
this set (e.g. `top_k`) are dropped so the upstream doesn't reject the request.

If you need full parameter pass-through (`tools`, `response_format`, …), use the
**`/v1/chat/completions`** endpoint, which forwards the whole payload verbatim.
See [API Reference → Sampling parameters](api-reference.md#sampling-parameters).

## Streaming output arrives all at once

Something between the client and llmproxy is buffering the stream:

- With `curl`, add `-N` to disable client-side buffering.
- Behind a reverse proxy, disable response buffering for streaming routes
  (e.g. nginx `proxy_buffering off;`). See [Deployment](deployment.md).

## Token usage is reported as zero

Token counts come from the upstream `usage` object and are re-exposed
(`usage`, `prompt_eval_count`/`eval_count`, `tokens_predicted`/`tokens_evaluated`
depending on the endpoint). If they are `0`, the upstream did not return usage
for that request (some models/streaming modes omit it) — not a llmproxy bug.

## The Docker healthcheck shows `unhealthy`

The healthcheck polls `GET /health` on the container's `PORT`. If it fails:

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

On a healthy local start you should see (last line reports inbound auth state):

```
llmproxy in ascolto su http://0.0.0.0:11434
Modelli esposti: meta/llama-3.1-8b-instruct, ...
Default: meta/llama-3.1-8b-instruct | log level=INFO | timezone log=Europe/Rome
Autenticazione in ingresso: disattivata
```

If instead you see `Attenzione: NVIDIA_API_KEY non impostata in .env`, the key
is missing and inference calls will fail until you set it.
