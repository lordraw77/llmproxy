# Audit Trail

The [access log](logging.md) tells you *what happened*. The audit trail tells you
**what was asked, what came back, and what it cost** — one structured record per
request, correlating the inbound call with its upstream leg: provider and native
model, the sampling parameters actually sent, the prompt, the completion, the
token usage, the timings, and the session the request belongs to.

It is **off by default** and **deferred**: enabling it does not slow the proxy
down.

## Why it does not cost latency

Nothing about the record is built on the request thread. The web and provider
layers only append plain values to an in-memory event (attribute writes and
`list.append`); the event is then handed to a bounded queue, and a single
background writer thread does the expensive part — clipping bodies, parsing the
buffered SSE of a relayed stream, serializing to JSON, writing and rotating the
file.

Two consequences worth knowing:

- **A slow or full disk cannot slow a completion down.** If the writer falls
  behind and the queue fills, the record is **dropped and counted**, never
  awaited. A proxy that stalls its traffic to keep its audit complete has the
  trade-off backwards. Drops are visible at `/stats`; a non-zero count means the
  queue (`AUDIT_QUEUE_SIZE`) is too small for the load, or the disk is too slow.
- **The record for a streaming request is written when the last frame leaves**,
  not when the headers do — that is where the completion, the token usage and
  the total duration finally exist.

What the request thread does pay is the event itself: measured on a local loop
with a stubbed upstream, about **0.1 ms per request**, and the same figure with
`AUDIT_BODIES=full` as with `truncated` — which is the point, since the size of
the record is the writer's problem, not the request's. Against an upstream that
answers in hundreds of milliseconds it is not observable.

## Enabling it

```dotenv
AUDIT_ENABLED=true
AUDIT_FILE=logs/audit.jsonl
AUDIT_BODIES=truncated
AUDIT_MAX_CHARS=2000
```

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIT_ENABLED` | `false` | Master switch (`1`/`true`/`yes`/`on`). |
| `AUDIT_FILE` | `logs/audit.jsonl` | Destination file. Its directory is created if missing. Supports the `{pid}` and `{date}` placeholders. |
| `AUDIT_FORMAT` | `jsonl` | `jsonl` (one record per line) or `pretty` (indented records). Both are valid `jq` input. |
| `AUDIT_BODIES` | `truncated` | How much content is recorded: `none` (accounting only — no prompts, no completions), `truncated` (clipped to `AUDIT_MAX_CHARS`), `full` (everything, uncapped). |
| `AUDIT_MAX_CHARS` | `2000` | Per-text character budget under `truncated`. Applies to each message and to the completion. |
| `AUDIT_QUEUE_SIZE` | `10000` | Records that may wait for the writer. Past this, new records are dropped. |
| `AUDIT_MAX_MB` | `64` | Size at which the file rotates. A record is never split across files, so the file can exceed the cap by at most one record. |
| `AUDIT_BACKUPS` | `5` | Rotated files kept (`audit.jsonl.1` … `.5`); the oldest is discarded. |
| `AUDIT_SESSION_HEADER` | *(empty)* | An extra request header to consult for the session id, before the built-in ones. |

Configuration is read at start-up, so changes take effect after a restart.

### Multiple workers

Under gunicorn each worker runs its own writer thread. They can share one file —
every record is written with a single append — but they cannot coordinate
rotation, so with more than one worker give each its own file:

```dotenv
AUDIT_FILE=logs/audit-{pid}.jsonl
```

## What gets recorded

Every request **except** successful `GET`s and the `/stats` endpoints. A model
list, a health probe or the dashboard polling itself has no prompt, no completion
and no tokens to account for; a *failing* `GET` is kept, because a 404 on a model
id or a rejected key is exactly what an audit trail is read for.

One record, in `pretty` form:

```json
{
  "ts": "2026-07-27T10:12:41.883Z",
  "request_id": "5f0bd54f",
  "session": { "id": "conv-1", "source": "header:X-Session-Id" },
  "client": { "ip": "10.0.0.7", "user_agent": "open-webui/1.0", "api_key_id": "sha256:9f2c1ab4e0d7" },
  "endpoint": { "method": "POST", "path": "/v1/chat/completions", "route": "/v1/chat/completions" },
  "model": {
    "requested": "nvidia:llama-3.3-70b",
    "exposed": "nvidia:llama-3.3-70b",
    "native": "meta/llama-3.3-70b-instruct",
    "provider": "nvidia"
  },
  "params": { "temperature": 0.7, "max_tokens": 128, "tools": ["get_weather"], "stream": true },
  "request": {
    "message_count": 2,
    "input_chars": 21,
    "messages": [
      { "role": "system", "content": "sei un assistente" },
      { "role": "user", "content": "ciao" }
    ]
  },
  "response": {
    "status": 200,
    "finish_reason": "stop",
    "content_chars": 11,
    "content": "Lorem ipsum",
    "tool_calls": null,
    "truncated": false
  },
  "tokens": { "prompt": 11, "completion": 7, "total": 18 },
  "timing": { "duration_ms": 812.4, "upstream_ms": 780.1, "ttfb_ms": 143.2 },
  "upstream": {
    "provider": "nvidia",
    "url": "https://integrate.api.nvidia.com/v1/chat/completions",
    "status": 200,
    "attempts": 1,
    "stream": true,
    "aggregated": false,
    "cache_hit": false
  },
  "error": null
}
```

Field by field:

| Field | Meaning |
|-------|---------|
| `ts` / `request_id` | When the request started, and the same correlation id that prefixes its log lines — the two views join on it. |
| `session` | The conversation this request belongs to, and how it was determined (see below). |
| `client.api_key_id` | A truncated SHA-256 of the inbound key: callers can be correlated without the file ever holding a credential. |
| `model` | All four names a request has: what the client asked for, what the proxy exposes, the provider's native id, and who served it. |
| `params` | Everything that is not content in the payload **actually sent upstream** — sampling knobs, `stop`, `response_format`, `tool_choice`, provider-specific extras. `tools` collapses to the function names unless `AUDIT_BODIES=full`. |
| `request` | `input_chars` and `message_count` are always recorded; the messages themselves follow `AUDIT_BODIES`. Multimodal blocks are serialized before clipping, so a base64 image cannot blow up the file. |
| `response` | Status, finish reason, completion length, the completion itself (per `AUDIT_BODIES`), and any tool calls. `truncated` says whether the capture hit its budget. |
| `tokens` | The upstream `usage`, for streaming and non-streaming replies alike. `null` when the upstream reported none. |
| `timing` | End-to-end duration, upstream latency, and time to first token for streams. |
| `upstream` | The call that left the proxy: URL, status, how many `attempts` it took (retries included), whether it streamed, whether the proxy re-aggregated it (`FORCE_UPSTREAM_STREAM`), and whether the reply came from the [response cache](configuration.md#response-caching) instead. |
| `error` | The reason a request failed: the provider's error body for a non-2xx reply, or the exception for an upstream that never answered. `null` on success. |

### Sessions

`session.id` groups the turns of one conversation. It is resolved in this order:

1. **A header**, if the client sends one: `AUDIT_SESSION_HEADER` (when set), then
   `X-Session-Id`, `X-Conversation-Id`, `X-Chat-Id`, `X-Request-Session`. This is
   authoritative and reported as `"source": "header:<name>"`.
2. **A fingerprint** otherwise: a hash of the caller, the model, and the
   conversation's **opening user message**, which every turn of the same chat
   replays verbatim. Reported as `"source": "fingerprint"`.

The fingerprint is a heuristic — two identical openings from the same client
collapse into one session — which is why the record always says which of the two
produced the id. If your front-end can send a conversation id, point
`AUDIT_SESSION_HEADER` at it and the question disappears.

## Reading it

```bash
# follow it live
tail -f logs/audit.jsonl | jq -c '{ts, model: .model.exposed, tok: .tokens.total}'

# token consumption per model
jq -s 'group_by(.model.exposed)[] | {model: .[0].model.exposed, total: map(.tokens.total // 0) | add}' logs/audit.jsonl

# one conversation, in order
jq -c 'select(.session.id == "conv-1") | {ts, prompt: .request.messages[-1].content, reply: .response.content}' logs/audit.jsonl

# everything that failed, with the provider's reason
jq -c 'select(.error != null) | {ts, model: .model.exposed, status: .response.status, why: .error.message}' logs/audit.jsonl

# slowest requests
jq -s 'sort_by(-.timing.duration_ms)[:10] | .[] | {ts, ms: .timing.duration_ms, model: .model.exposed}' logs/audit.jsonl
```

`AUDIT_FORMAT=pretty` is meant for reading the file directly (`less`, an editor);
`jq` parses both forms.

## Monitoring the trail

`/stats` and `/stats.json` report the trail alongside the other counters:

```json
"audit": {
  "enabled": true,
  "file": "logs/audit.jsonl",
  "format": "jsonl",
  "bodies": "truncated",
  "written": 18422,
  "dropped": 0,
  "errors": 0,
  "queued": 3
}
```

`dropped` above zero means records were lost to back-pressure — raise
`AUDIT_QUEUE_SIZE`, put the file on faster storage, or lower `AUDIT_BODIES`.
`errors` counts write failures (a full disk, a permission problem); the first few
are also reported as warnings in the ordinary log.

## Privacy

With `AUDIT_BODIES=truncated` (the default) or `full`, the file contains **the
prompts and the completions**, in clear text. Treat it as you would treat the
conversations themselves: restrict access to it, keep it out of shared log
shipping unless that is intended, and give it a retention policy —
`AUDIT_MAX_MB` × (`AUDIT_BACKUPS` + 1) is the maximum disk it will ever occupy.

`AUDIT_BODIES=none` keeps the whole accounting side — models, parameters,
timings, tokens, sessions, errors, prompt and completion **sizes** — with no
prompt or completion text at all. It is the right setting when the audit exists
for cost attribution or compliance rather than for debugging.

Inbound API keys are never written: only a truncated digest of them.
