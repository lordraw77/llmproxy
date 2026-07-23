# Deployment

## Docker Compose (recommended)

The bundled [`docker-compose.yml`](../docker-compose.yml) is the simplest way to
run llmproxy as a long-lived service:

```yaml
services:
  llmproxy:
    build: .
    container_name: llmproxy
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "${PORT:-11434}:${PORT:-11434}"
```

Start it:

```bash
docker compose up -d --build
```

Key properties:

- **`restart: unless-stopped`** — the container is restarted automatically after
  a crash or host reboot, unless you stopped it manually.
- **`env_file: .env`** — all configuration comes from `.env`
  (see [Configuration](configuration.md)).
- **Port mapping** — `${PORT}` controls both the container's listening port and
  the published host port. They are always kept in sync.

### The container image

The [`Dockerfile`](../Dockerfile) builds a small image:

- Base: `python:3.12-slim`
- Installs only `flask`, `requests`, `python-dotenv`
- Runs as a **non-root user** (`appuser`)
- Exposes port `11434`
- Includes a `HEALTHCHECK` that polls `GET /` every 30s

Because the healthcheck reads `PORT` from the environment, it stays correct even
if you change the port.

## Common operational tasks

```bash
# View logs
docker compose logs -f llmproxy

# Restart after changing .env
docker compose restart llmproxy

# Rebuild after changing main.py or dependencies
docker compose up -d --build

# Stop and remove
docker compose down

# Check container health
docker inspect --format '{{.State.Health.Status}}' llmproxy
```

## Production considerations

llmproxy runs Flask's **built-in development server** (`app.run(...)`). It is set
to `threaded=True`, which handles concurrent requests, but the development
server is not hardened for high-load production use. For production-grade
serving you may want to:

- Put a **WSGI server** (e.g. gunicorn or uWSGI) in front of the Flask app.
  Note that streaming endpoints rely on generator responses, so configure worker
  types and timeouts accordingly (gunicorn's `gthread` workers with a long
  `--timeout` are a reasonable starting point).
- Place llmproxy behind a **reverse proxy** (nginx, Caddy, Traefik) for TLS
  termination and request buffering control. Disable proxy buffering on
  streaming routes so SSE / NDJSON is flushed to clients in real time
  (e.g. nginx `proxy_buffering off;`).

### Security

llmproxy performs **no inbound authentication**. Anyone who can reach the port
can send requests that consume your NVIDIA API quota. Therefore:

- Do **not** expose the port directly to the public internet.
- Bind it to a private interface, or restrict access with a firewall, VPN, or an
  authenticating reverse proxy.
- Keep `.env` (and thus `NVIDIA_API_KEY`) out of version control and off shared
  images. The `.dockerignore` should exclude it from the build context — verify
  before publishing images.

### Scaling

llmproxy is stateless, so you can run multiple replicas behind a load balancer
without any coordination. The practical limit is your upstream NVIDIA API rate
limit and quota, not llmproxy itself.

## Upgrading

1. Pull or edit the new `main.py` / `requirements.txt`.
2. Rebuild and recreate the container:

   ```bash
   docker compose up -d --build
   ```

Configuration in `.env` is preserved across rebuilds.
