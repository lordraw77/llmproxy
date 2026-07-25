# Deployment

## Docker Compose (recommended)

Three compose files are provided:

| File | Use |
|------|-----|
| [`docker-compose.yml`](../docker-compose.yml) | Development / quick start (`build: .`). Multi-provider lines commented. |
| [`docker-compose.prod.env.yml`](../docker-compose.prod.env.yml) | **Production, single provider** from `.env` (`NVIDIA_*`). |
| [`docker-compose.prod.toml.yml`](../docker-compose.prod.toml.yml) | **Production, multi-provider** from a mounted `providers.toml`. |

The production files use the prebuilt image, bind the port to loopback (front them
with a TLS reverse proxy), add log rotation, a memory limit, and
`no-new-privileges`. Pick with `-f`, e.g.
`docker compose -f docker-compose.prod.toml.yml up -d`; see the
[Migration guide](migration.md#docker-production) for the full walkthrough.

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

### Multi-provider in Docker

To serve several upstreams, mount a [`providers.toml`](configuration.md#multi-provider)
into the container and point `PROVIDERS_CONFIG` at it. The bundled compose file
ships both lines commented out — uncomment them:

```yaml
    environment:
      PROVIDERS_CONFIG: /config/providers.toml
    volumes:
      - ./providers.toml:/config/providers.toml:ro
```

You can generate that `providers.toml` from the same `.env`, without a local
Python install, straight from the image (writes to the host via stdout):

```bash
docker compose run --rm --no-TTY llmproxy \
  python -m llmproxy.scripts.env_to_toml - > providers.toml
# or: make migrate-config-docker
```

Then edit it (add providers / API keys as `${ENV_VAR}` refs), make sure those env
vars are in `.env`, and `docker compose up -d`. Verify with
`curl localhost:$PORT/health` (the `providers` count) and
`curl 'localhost:$PORT/health?upstream=1'` (per-provider reachability). Full
walkthrough and the `docker run` equivalent are in
[Configuration → Migrating the env config](configuration.md#migrating-the-env-config-to-providerstoml).

### The container image

The [`Dockerfile`](../Dockerfile) builds a small image:

- Base: `python:3.12-slim`
- Installs `flask`, `requests`, `python-dotenv`, `gunicorn`
- Runs as a **non-root user** (`appuser`)
- Exposes port `11434`
- Includes a `HEALTHCHECK` that polls `GET /health` every 30s
- Serves the app under **gunicorn** (`gthread` workers) — SSE/NDJSON streaming
  friendly — tunable via `WEB_CONCURRENCY`, `THREADS`, `GUNICORN_TIMEOUT`

Because the healthcheck reads `PORT` from the environment, it stays correct even
if you change the port.

### Prebuilt image (Docker Hub)

A prebuilt image is published as [`lordraw/llmproxy`](https://hub.docker.com/r/lordraw/llmproxy):

```bash
docker run -d --name llmproxy -p 11434:11434 --env-file .env \
  lordraw/llmproxy:latest
```

Pin a specific version in production (e.g. `lordraw/llmproxy:1.2.3`) rather than
`latest`. To point Compose at the published image, replace `build: .` with
`image: lordraw/llmproxy:latest`.

### Building & publishing (Makefile)

The repository [`Makefile`](../Makefile) builds and publishes the image, deriving
the version from **git tags**:

```bash
make build            # local build of :<version>
git tag v1.2.3
make release          # build + push :1.2.3 and :latest
make buildx-release   # same, multi-arch, in one step
make help             # list all targets
```

On an exact git tag the image is tagged `:<version>` **and** `:latest`; off a
tag, `:latest` is left untouched. Override with `IMAGE`, `VERSION`, `PLATFORMS`.

## Common operational tasks

```bash
# View logs
docker compose logs -f llmproxy

# Restart after changing .env
docker compose restart llmproxy

# Rebuild after changing the code (main.py / llmproxy/) or dependencies
docker compose up -d --build

# Stop and remove
docker compose down

# Check container health
docker inspect --format '{{.State.Health.Status}}' llmproxy
```

## Production considerations

The Docker image already serves llmproxy under **gunicorn** with `gthread`
workers and a long timeout (streaming-friendly), tunable via `WEB_CONCURRENCY`,
`THREADS`, and `GUNICORN_TIMEOUT`. (Running `python main.py` directly uses
Flask's built-in development server with `threaded=True`, which is fine for local
use but not hardened for high load.) For production you may also want to:

- Place llmproxy behind a **reverse proxy** (nginx, Caddy, Traefik) for TLS
  termination and request buffering control. Disable proxy buffering on
  streaming routes so SSE / NDJSON is flushed to clients in real time
  (e.g. nginx `proxy_buffering off;`).
- **Monitor** the live dashboard at `GET /stats` (HTML) or scrape `GET
  /stats.json`: request/latency/token counters, upstream call telemetry, and the
  process-manager view (PID, worker pool, memory, uptime). See
  [API Reference → `/stats`](api-reference.md#get-stats).

### Security

llmproxy supports **optional inbound authentication**: set `PROXY_API_KEY` and
every request (except `/` and `/health`) must present that key via
`Authorization: Bearer <key>` or `X-Api-Key: <key>`. When it is empty the proxy
is open and anyone who can reach the port can consume your NVIDIA API quota.
Therefore:

- Set `PROXY_API_KEY` when the proxy is reachable by anything other than trusted
  local clients.
- Do **not** expose the port directly to the public internet; also bind it to a
  private interface, or restrict access with a firewall, VPN, or an
  authenticating reverse proxy.
- Keep `.env` (and thus `NVIDIA_API_KEY` / `PROXY_API_KEY`) out of version
  control and off shared images. The `.dockerignore` should exclude it from the
  build context — verify before publishing images.

### Scaling

llmproxy is stateless, so you can run multiple replicas behind a load balancer
without any coordination. The practical limit is your upstream NVIDIA API rate
limit and quota, not llmproxy itself.

Note that the `/stats` metrics are kept **in memory per gunicorn worker** (and
per replica): each response reflects only the worker that served it. This is fine
for a quick operational glance; for aggregated fleet-wide metrics, scrape
`/stats.json` per instance or place a dedicated metrics backend (e.g. Prometheus)
in front.

## Upgrading

1. Pull or edit the new code (`main.py`, the `llmproxy/` package) / `requirements.txt`.
2. Rebuild and recreate the container:

   ```bash
   docker compose up -d --build
   ```

Configuration in `.env` is preserved across rebuilds.
