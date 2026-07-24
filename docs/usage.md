# Usage Examples

These examples assume llmproxy is running on `http://localhost:11434` with a
valid `NVIDIA_API_KEY`.

## curl

### Ollama chat (non-streaming)

```bash
curl http://localhost:11434/api/chat -d '{
  "model": "anything",
  "messages": [{"role": "user", "content": "Say hello in one word."}],
  "stream": false
}'
```

### Ollama chat (streaming)

```bash
curl -N http://localhost:11434/api/chat -d '{
  "messages": [{"role": "user", "content": "Count from 1 to 5."}]
}'
```

`-N` disables curl's buffering so you see the newline-delimited JSON stream as
it arrives. (Streaming is the default for `/api/chat`.)

### Ollama generate

```bash
curl http://localhost:11434/api/generate -d '{
  "prompt": "Write a haiku about GPUs.",
  "system": "You are a poet.",
  "stream": false
}'
```

### OpenAI chat completions

```bash
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anything",
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "temperature": 0.2
  }'
```

Because `/v1/chat/completions` forwards the payload verbatim, you can include
any OpenAI parameter (`max_tokens`, `stop`, `top_p`, `tools`, …).

### OpenAI streaming

```bash
curl -N http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Stream a sentence."}],"stream":true}'
```

### llama.cpp native completion

```bash
curl http://localhost:11434/completion -d '{
  "prompt": "The capital of France is",
  "stream": false
}'
```

### Embeddings (OpenAI)

```bash
curl http://localhost:11434/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model": "nvidia/nv-embedqa-e5-v5", "input": "Hello embeddings world."}'
```

### Embeddings (Ollama)

```bash
# New format
curl http://localhost:11434/api/embed \
  -d '{"input": "Hello from /api/embed."}'

# Legacy format
curl http://localhost:11434/api/embeddings \
  -d '{"prompt": "Hello from /api/embeddings."}'
```

If you omit `model`, llmproxy uses `NVIDIA_EMBEDDINGS_MODEL` (chat models are not
valid for embeddings). See [Configuration](configuration.md).

### With inbound authentication

If the proxy is started with `PROXY_API_KEY`, send the key on every request:

```bash
curl http://localhost:11434/v1/models \
  -H "Authorization: Bearer $PROXY_API_KEY"
# or:  -H "X-Api-Key: $PROXY_API_KEY"
```

### Statistics & metrics

```bash
# Machine-readable snapshot (requests, latency, tokens, upstream, process)
curl http://localhost:11434/stats.json

# Or open the auto-refreshing HTML dashboard in a browser:
#   http://localhost:11434/stats
```

`/stats` and `/stats.json` require `PROXY_API_KEY` when it is configured (send it
as above). Counters are per gunicorn worker — see
[API Reference → `/stats`](api-reference.md#get-stats).

## Python — OpenAI SDK

Point the official OpenAI SDK at llmproxy by overriding `base_url`. Unless the
proxy is protected with `PROXY_API_KEY`, the API key is not validated, so any
non-empty string works. When `PROXY_API_KEY` is set, pass it as the `api_key`
(it is sent as `Authorization: Bearer …`).

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="not-needed",
)

resp = client.chat.completions.create(
    model="anything",  # ignored; llmproxy uses NVIDIA_MODEL
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Streaming:

```python
stream = client.chat.completions.create(
    model="anything",
    messages=[{"role": "user", "content": "Count to three."}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

## Python — Ollama SDK

```python
import ollama

client = ollama.Client(host="http://localhost:11434")

resp = client.chat(
    model="anything",
    messages=[{"role": "user", "content": "Hi there!"}],
)
print(resp["message"]["content"])
```

## Open WebUI

In Open WebUI, add a connection pointing at llmproxy:

- **Ollama API** base URL: `http://localhost:11434`
- or **OpenAI API** base URL: `http://localhost:11434/v1` (API key: any value)

Every model listed in `NVIDIA_MODELS` appears in Open WebUI's model picker, and
selecting one routes that request to the matching upstream model. To expose
several models, set a comma-separated `NVIDIA_MODELS` and restart:

```dotenv
NVIDIA_MODELS=meta/llama-3.1-8b-instruct,meta/llama-3.1-70b-instruct,mistralai/mistral-7b-instruct-v0.3
```

See [Configuration → Multi-model support](configuration.md#multi-model-support).

## Tips

- llmproxy can expose **multiple models** via `NVIDIA_MODELS`. The client's
  `model` field is honored when it matches an exposed model; otherwise the
  default (first in the list) is used. See
  [Configuration → Multi-model support](configuration.md#multi-model-support).
- On the Ollama, `/v1/completions`, and `/completion` endpoints a normalized set
  of sampling params is forwarded (`temperature`, `top_p`, `max_tokens`, `stop`,
  `presence_penalty`, `frequency_penalty`, `seed`, `n`; `num_predict`/`n_predict`
  map to `max_tokens`). For full parameter pass-through, use
  `/v1/chat/completions`. See
  [API Reference → Sampling parameters](api-reference.md#sampling-parameters).
