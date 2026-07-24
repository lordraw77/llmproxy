<p align="center">
  <img src="../img/llmproxy.png" alt="llmproxy logo" width="240">
</p>

# llmproxy — Documentation
[![GitHub License](https://img.shields.io/github/license/lordraw77/llmproxy)](../LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/lordraw77/llmproxy)](https://github.com/lordraw77/llmproxy/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/lordraw77/llmproxy)](https://github.com/lordraw77/llmproxy/issues)
[![Docker Pulls](https://img.shields.io/docker/pulls/lordraw/llmproxy)](https://hub.docker.com/r/lordraw/llmproxy)

**A lightweight, high-performance LLM proxy for caching, automatic failover, cost tracking, and seamless integration between local and cloud AI providers.**

**llmproxy** is a lightweight Flask server that emulates the HTTP APIs of several
popular local LLM runtimes ([Ollama](https://ollama.com), the
[OpenAI](https://platform.openai.com) `/v1` API, and
[llama.cpp](https://github.com/ggerganov/llama.cpp)'s `llama-server`) and
transparently **forwards every request to NVIDIA's OpenAI-compatible API**
(`https://integrate.api.nvidia.com/v1`).

This lets any tool that already speaks Ollama, OpenAI, or llama.cpp talk to a
NVIDIA-hosted model **without any client-side changes** — you simply point the
client at llmproxy instead of at a real local runtime. It covers chat,
completions, and **embeddings**, supports streaming, multi-model discovery,
optional inbound authentication, automatic retries on transient upstream errors,
and a live **`/stats`** metrics & process dashboard.

```mermaid
flowchart LR
    client["Your client<br/>(Open WebUI, curl, SDK)"]
    proxy["llmproxy<br/>(Flask)"]
    nvidia["NVIDIA API<br/>integrate.api.nvidia.com/v1"]

    client -->|"Ollama / OpenAI / llama.cpp<br/>HTTP request"| proxy
    proxy -->|"OpenAI request"| nvidia
    nvidia -->|"streaming / JSON"| proxy
    proxy -->|"streaming / JSON response"| client
```

## Documentation index

| Document | Description |
|----------|-------------|
| [Overview](overview.md) | What llmproxy is, how it works, and its architecture |
| [Installation](installation.md) | Local and Docker setup instructions |
| [Configuration](configuration.md) | Environment variables and options |
| [Logging & Telemetry](logging.md) | Request/response logs, telemetry, and the configurable-timezone clock |
| [API Reference](api-reference.md) | Every endpoint, with request/response examples |
| [Usage Examples](usage.md) | End-to-end examples with curl and common clients |
| [Testing](testing.md) | The `scripts/tests.sh` runner (bash + optional TUI) |
| [Deployment](deployment.md) | Running in production with Docker Compose |
| [Troubleshooting](troubleshooting.md) | Common problems and how to solve them |

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
[Deployment](deployment.md) for building and publishing with the `Makefile`.

## License

Released under the **MIT License** — see the [`LICENSE`](../LICENSE) file for the
full text. In short: free to use, copy, modify, and distribute, with attribution
and no warranty.
