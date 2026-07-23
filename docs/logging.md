# Logging & Telemetry

llmproxy emits structured, correlated logs for every request, so you can see
**what was asked**, **how NVIDIA responded**, and **how long it took** — with a
clock in the timezone you choose.

Logs are written to standard output (visible via `docker compose logs -f
llmproxy` or directly in the terminal for a local run).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_TZ` | value of `TZ`, else `UTC` | IANA timezone name for the clock in each log line (e.g. `Europe/Rome`, `America/New_York`). An unknown name falls back to `UTC`. |

Example `.env`:

```dotenv
LOG_LEVEL=INFO
LOG_TZ=Europe/Rome
```

Changes take effect after a restart (configuration is read at startup).

## The clock (configurable timezone)

Every log line is timestamped in `LOG_TZ`. The timezone abbreviation is included
so the offset is unambiguous:

```
2026-07-23 17:23:57 CEST [INFO] ...
```

With `LOG_TZ=UTC` the same event reads `2026-07-23 15:23:57 UTC`. This only
affects **log** timestamps; the `created_at` / `created` fields in API responses
remain UTC (as the Ollama and OpenAI clients expect).

## Log levels

- **INFO** (default) — request/response lifecycle and telemetry, one correlated
  set of lines per request.
- **DEBUG** — everything in INFO **plus** the full JSON payload sent to NVIDIA
  (truncated to 2000 chars). Useful for debugging request shaping; may contain
  prompt content, so avoid it in shared/production logs.
- **WARNING** — upstream non-2xx responses and error propagation only.
- **ERROR** — upstream unreachable (timeout, DNS, connection refused).

## What gets logged

Each incoming request is assigned a short **correlation ID** (e.g. `85c9fc1b`)
that appears in every related line, so concurrent requests can be told apart.

A typical successful, non-streaming request produces:

```
2026-07-23 17:24:01 CEST [INFO] [85c9fc1b] --> POST /v1/chat/completions | client=172.18.0.1 model=meta/llama-3.1-8b-instruct stream=False
2026-07-23 17:24:01 CEST [INFO] [85c9fc1b] -> NVIDIA request | model=meta/llama-3.1-8b-instruct stream=False messages=1 input_chars=42
2026-07-23 17:24:02 CEST [INFO] [85c9fc1b] <- NVIDIA response | status=200 latency=780ms stream=False
2026-07-23 17:24:02 CEST [INFO] [85c9fc1b] telemetry | prompt_tokens=18 completion_tokens=25 total_tokens=43 latency=780ms
2026-07-23 17:24:02 CEST [INFO] [85c9fc1b] <-- POST /v1/chat/completions | status=200 duration=782ms
```

Line by line:

| Line | Meaning |
|------|---------|
| `-->` | **Incoming request**: method, path, client IP, requested model and `stream` flag. |
| `-> NVIDIA request` | **Upstream call**: resolved model, stream flag, number of messages, total input characters. |
| `<- NVIDIA response` | **Upstream status & latency**: the HTTP status NVIDIA returned and the time to first response. |
| `telemetry` | **Token usage** from the upstream `usage` object (non-streaming only), with latency. |
| `<--` | **End-to-end**: final client-facing status and total request duration. |

### Streaming requests

For streaming responses the upstream `latency` is the **time to first byte**
(headers), since the body is streamed. Token usage is not logged, because
providers don't return a `usage` object on streamed responses.

### Error cases

An upstream error (propagated to the client — see
[API Reference → Error responses](api-reference.md#error-responses)) logs at
`WARNING`, including the upstream status and a truncated error body:

```
2026-07-23 17:25:10 CEST [INFO]    [a1b2c3d4] -> NVIDIA request | model=nvidia/does-not-exist stream=False messages=1 input_chars=2
2026-07-23 17:25:10 CEST [WARNING] [a1b2c3d4] <- NVIDIA response | status=404 latency=120ms stream=False
2026-07-23 17:25:10 CEST [WARNING] [a1b2c3d4] <- NVIDIA error body: {"detail":"model not found"}
2026-07-23 17:25:10 CEST [WARNING] [a1b2c3d4] propagating upstream error | status=404
2026-07-23 17:25:10 CEST [WARNING] [a1b2c3d4] <-- POST /v1/chat/completions | status=404 duration=121ms
```

An unreachable upstream (timeout / DNS / connection refused) logs at `ERROR`:

```
2026-07-23 17:26:00 CEST [ERROR] [b2c3d4e5] <- NVIDIA no-response after 120000ms | HTTPSConnectionPool(...): Read timed out
```

## Startup banner

On start, llmproxy logs its effective configuration, including the log timezone:

```
2026-07-23 17:20:00 CEST [INFO] llmproxy in ascolto su http://0.0.0.0:11434
2026-07-23 17:20:00 CEST [INFO] Modelli esposti: meta/llama-3.1-8b-instruct, ...
2026-07-23 17:20:00 CEST [INFO] Default: meta/llama-3.1-8b-instruct | log level=INFO | timezone log=Europe/Rome
```
