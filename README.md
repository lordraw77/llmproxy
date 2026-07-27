<p align="center">
  <img src="img/llmproxy.png" alt="llmproxy logo" width="240">
</p>

# llmproxy
[![GitHub License](https://img.shields.io/github/license/lordraw77/llmproxy)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/lordraw77/llmproxy)](https://github.com/lordraw77/llmproxy/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/lordraw77/llmproxy)](https://github.com/lordraw77/llmproxy/issues)
[![Docker Pulls](https://img.shields.io/docker/pulls/lordraw/llmproxy)](https://hub.docker.com/r/lordraw/llmproxy)

**A lightweight, high-performance LLM proxy for caching, automatic failover, cost tracking, and seamless integration between local and cloud AI providers.**

**llmproxy** is a lightweight Flask server that emulates the HTTP APIs of several
popular local LLM runtimes ([Ollama](https://ollama.com), the
[OpenAI](https://platform.openai.com) `/v1` API, and
[llama.cpp](https://github.com/ggerganov/llama.cpp)'s `llama-server`) and
transparently forwards every request to one or more **upstream providers** —
NVIDIA and any other OpenAI-compatible endpoint (OpenAI, Mistral, vLLM, Groq,
OpenRouter, LM Studio, local Ollama/llama.cpp), **Azure OpenAI**, **Anthropic**,
and **Google Gemini** (the last two translated natively to/from the OpenAI shape).

This lets any tool that already speaks Ollama, OpenAI, or llama.cpp talk to any
configured model **without any client-side changes** — you simply point the
client at llmproxy instead of at a real local runtime. It covers chat,
completions, and **embeddings**, supports streaming, multi-model discovery,
optional inbound authentication, automatic retries on transient upstream errors,
an optional **response cache** (configurable TTL & size) for non-streaming
replies, and a live **`/stats`** metrics & process dashboard.

### Providers

Providers are declared in a `providers.toml` file (path via `PROVIDERS_CONFIG`).
Every provider's models are exposed together — the union. With a single provider
the model names stay **bare** (unchanged); with two or more they are prefixed as
`provider:model` to disambiguate (a per-model `alias` overrides that, and separates
the same model offered by two providers). When no `providers.toml` is present, a
single provider is synthesized from the `NVIDIA_*` env vars, so existing setups
keep working with **zero config** — and with identical model names. Generate a
starting file from your current environment with `make migrate-config`. See
[Configuration](docs/configuration.md) and the [Migration guide](docs/migration.md).

```toml
[[provider]]
name = "nvidia"
type = "openai_compatible"
base_url = "https://integrate.api.nvidia.com/v1"
api_key = "${NVIDIA_API_KEY}"
models = ["meta/llama-3.1-8b-instruct"]

[[provider]]
name = "anthropic"
type = "anthropic"
api_key = "${ANTHROPIC_API_KEY}"
models = ["claude-opus-4-8", "claude-sonnet-5"]
```

## Demo

<p align="center">
  <img src="assets/demo.gif" alt="llmproxy in action: health check, model discovery, OpenAI-compatible chat and Ollama streaming" width="820">
</p>

> The proxy starts, exposes the models, and answers both an OpenAI-compatible
> `/v1/chat/completions` call and a native Ollama streaming `/api/chat` call —
> every request forwarded to NVIDIA. The recording is scripted in
> [`scripts/demo.sh`](scripts/demo.sh) (source cast: [`assets/demo.cast`](assets/demo.cast)).

```mermaid
flowchart LR
    client["Your client<br/>(Open WebUI, curl, SDK)"]
    proxy["llmproxy<br/>(Flask · model→provider routing)"]
    nvidia["NVIDIA / OpenAI-compatible"]
    anthropic["Anthropic"]
    gemini["Google Gemini"]

    client -->|"Ollama / OpenAI / llama.cpp<br/>HTTP request"| proxy
    proxy -->|"OpenAI request"| nvidia
    proxy -->|"native Messages API"| anthropic
    proxy -->|"native generateContent"| gemini
    nvidia -->|"streaming / JSON"| proxy
    proxy -->|"streaming / JSON response"| client
```

## Documentation index

| Document | Description |
|----------|-------------|
| [Overview](docs/overview.md) | What llmproxy is, how it works, and its architecture |
| [Installation](docs/installation.md) | Local and Docker setup instructions |
| [Configuration](docs/configuration.md) | Environment variables and options |
| [Migration](docs/migration.md) | Moving from env config to multi-provider `providers.toml` (local & Docker) |
| [Logging & Telemetry](docs/logging.md) | Request/response logs, telemetry, and the configurable-timezone clock |
| [Audit Trail](docs/audit.md) | The deferred, per-request audit file: prompts, replies, parameters, tokens, sessions |
| [API Reference](docs/api-reference.md) | Every endpoint, with request/response examples |
| [Usage Examples](docs/usage.md) | End-to-end examples with curl and common clients |
| [Testing](docs/testing.md) | The offline `pytest` suite (`make test`) and the `scripts/tests.sh` endpoint runner |
| [Deployment](docs/deployment.md) | Running in production with Docker Compose |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and how to solve them |

## Quick start

```bash
# 1. Configure your NVIDIA API key
cp .env.example .env
# edit .env and set NVIDIA_API_KEY

# 2. Run with Docker Compose (or the prebuilt image)
docker compose up -d
# or: docker run -d -p 11434:11434 --env-file .env lordraw/llmproxy:latest

# 3. Test it
curl http://localhost:11434/
# → "Ollama is running"
```

The prebuilt image is published on Docker Hub as
[`lordraw/llmproxy`](https://hub.docker.com/r/lordraw/llmproxy); see
[Deployment](docs/deployment.md) for building and publishing with the `Makefile`.

## License

Released under the **MIT License** — see the [`LICENSE`](LICENSE) file for the
full text. In short: free to use, copy, modify, and distribute, with attribution
and no warranty.
