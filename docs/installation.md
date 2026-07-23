# Installation

llmproxy can be run either directly with Python or inside Docker. Docker is the
recommended path for most users.

## Prerequisites

- A **NVIDIA API key** for the OpenAI-compatible endpoint
  (`https://integrate.api.nvidia.com/v1`). Get one from
  [build.nvidia.com](https://build.nvidia.com).
- Either:
  - **Docker** and **Docker Compose** (recommended), or
  - **Python 3.12+** and `pip`.

## Option A — Docker Compose (recommended)

1. Create your environment file from the template:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set at least `NVIDIA_API_KEY` (see
   [Configuration](configuration.md)).

3. Build and start the container:

   ```bash
   docker compose up -d --build
   ```

4. Verify it is running:

   ```bash
   curl http://localhost:11434/
   # → "Ollama is running"
   ```

The service is defined in [`docker-compose.yml`](../docker-compose.yml) with
`restart: unless-stopped`, so it will come back up automatically after a reboot
or crash.

To stop it:

```bash
docker compose down
```

## Option B — Plain Docker

Build locally:

```bash
docker build -t llmproxy .
docker run -d --name llmproxy \
  --env-file .env \
  -p 11434:11434 \
  llmproxy
```

…or use the **prebuilt image** from Docker Hub
([`lordraw/llmproxy`](https://hub.docker.com/r/lordraw/llmproxy)):

```bash
docker run -d --name llmproxy \
  --env-file .env \
  -p 11434:11434 \
  lordraw/llmproxy:latest
```

## Option C — Local Python

1. (Optional but recommended) create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create and configure `.env`:

   ```bash
   cp .env.example .env
   # edit .env and set NVIDIA_API_KEY
   ```

4. Run the server:

   ```bash
   python main.py
   ```

   You should see (the startup banner also reports whether inbound auth is on):

   ```
   llmproxy in ascolto su http://0.0.0.0:11434
   Modelli esposti: meta/llama-3.1-8b-instruct, ...
   Default: meta/llama-3.1-8b-instruct | log level=INFO | timezone log=Europe/Rome
   Autenticazione in ingresso: disattivata
   ```

   > If `NVIDIA_API_KEY` is not set, the server still starts but prints a
   > warning and every inference call will fail with HTTP 500.

## Verifying the installation

```bash
# Liveness (reports basic config too)
curl http://localhost:11434/health
# → {"status":"ok","api_key_configured":true,"models":1,"default_model":"..."}

# Liveness + live upstream check
curl "http://localhost:11434/health?upstream=1"

# Model list (Ollama style)
curl http://localhost:11434/api/tags
```

If both respond, llmproxy is installed and running. Continue to
[Usage Examples](usage.md).
