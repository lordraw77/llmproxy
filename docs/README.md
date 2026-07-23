# llmproxy — Documentation

**llmproxy** is a lightweight Flask server that emulates the HTTP APIs of several
popular local LLM runtimes ([Ollama](https://ollama.com), the
[OpenAI](https://platform.openai.com) `/v1` API, and
[llama.cpp](https://github.com/ggerganov/llama.cpp)'s `llama-server`) and
transparently **forwards every request to NVIDIA's OpenAI-compatible API**
(`https://integrate.api.nvidia.com/v1`).

This lets any tool that already speaks Ollama, OpenAI, or llama.cpp talk to a
NVIDIA-hosted model **without any client-side changes** — you simply point the
client at llmproxy instead of at a real local runtime.

```
┌──────────────┐     Ollama / OpenAI / llama.cpp      ┌──────────┐   OpenAI    ┌───────────────────┐
│  Your client │  ─────────── HTTP ─────────────────▶ │ llmproxy  │ ──────────▶ │  NVIDIA API       │
│ (Open WebUI, │ ◀────── streaming / JSON ─────────── │  (Flask) │ ◀────────── │ integrate.api...  │
│  curl, SDK)  │                                      └──────────┘             └───────────────────┘
└──────────────┘
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

# 2. Run with Docker Compose
docker compose up -d

# 3. Test it
curl http://localhost:11434/
# → "Ollama is running"
```

## License

No license file is bundled with this project. Consult the repository owner
before redistribution.
