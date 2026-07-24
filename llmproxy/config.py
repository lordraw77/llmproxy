"""Application configuration.

All environment-variable parsing lives here, isolated from the rest of the
codebase. The rest of the application depends on the immutable :class:`Settings`
object rather than reading ``os.environ`` directly, which keeps the domain,
service, and web layers free of environment coupling and trivially testable.
"""

import os
from dataclasses import dataclass, field
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def _parse_models(raw, fallback):
    """Parse a comma-separated list of models, falling back to a single model.

    Args:
        raw: Comma-separated string of model names (may be None or empty).
        fallback: Model name returned as a single-item list when ``raw`` is empty.

    Returns:
        A non-empty list of stripped model names.
    """
    models = [m.strip() for m in (raw or "").split(",") if m.strip()]
    return models or [fallback]


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process configuration, built from the environment."""

    # Server bind address.
    host: str
    port: int

    # Logging / telemetry.
    log_level: str
    log_tz: str
    log_tzinfo: object

    # Upstream (NVIDIA OpenAI-compatible API).
    nvidia_api_base: str
    nvidia_api_key: str
    upstream_timeout: float
    pool_size: int

    # Retry policy on transient upstream errors.
    retry_max: int
    retry_backoff: float
    retry_status: frozenset = field(default_factory=lambda: frozenset({429, 500, 502, 503, 504}))

    # Force streaming towards the upstream regardless of what the caller asked
    # for. Avoids read-timeouts on slow non-streaming generations: the upstream
    # keeps sending SSE bytes, so the read timeout never trips. Transparent to
    # the caller (a non-streaming client still gets a single JSON response).
    force_upstream_stream: bool = False

    # Outbound HTTP proxy for reaching the upstream (corporate egress proxy).
    # Empty = no explicit proxy (``requests`` still honors the ambient
    # HTTP(S)_PROXY / NO_PROXY environment via ``trust_env``).
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""

    @property
    def proxies(self):
        """A ``requests``-style proxies mapping, or ``None`` when none is set."""
        mapping = {}
        if self.http_proxy:
            mapping["http"] = self.http_proxy
        if self.https_proxy:
            mapping["https"] = self.https_proxy
        return mapping or None

    # Inbound authentication.
    proxy_api_key: str = ""
    auth_exempt_paths: frozenset = field(default_factory=lambda: frozenset({"/", "/health"}))

    # Exposed chat models.
    models: tuple = ()
    default_model: str = ""

    # Embeddings.
    embeddings_model: str = ""
    embeddings_input_type: str = ""


def load_settings():
    """Build a :class:`Settings` from the current environment (loads ``.env`` first)."""
    load_dotenv()

    log_tz = os.environ.get("LOG_TZ", os.environ.get("TZ", "UTC"))
    try:
        log_tzinfo = ZoneInfo(log_tz)
    except (ZoneInfoNotFoundError, ValueError):
        log_tzinfo = timezone.utc
        log_tz = "UTC"

    nvidia_model = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
    models = _parse_models(os.environ.get("NVIDIA_MODELS", nvidia_model), nvidia_model)

    # UPSTREAM_POOL_SIZE, then THREADS, then a sane default: sized on the worker threads.
    pool_size = int(os.environ.get("UPSTREAM_POOL_SIZE", os.environ.get("THREADS", "8")))

    return Settings(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "11434")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_tz=log_tz,
        log_tzinfo=log_tzinfo,
        nvidia_api_base=os.environ.get("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1"),
        nvidia_api_key=os.environ.get("NVIDIA_API_KEY", ""),
        upstream_timeout=float(os.environ.get("UPSTREAM_TIMEOUT", "120")),
        pool_size=pool_size,
        force_upstream_stream=os.environ.get("FORCE_UPSTREAM_STREAM", "false").strip().lower()
        in ("1", "true", "yes", "on"),
        # Accept the conventional upper- and lower-case proxy env var names.
        http_proxy=os.environ.get("HTTP_PROXY", os.environ.get("http_proxy", "")).strip(),
        https_proxy=os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", "")).strip(),
        no_proxy=os.environ.get("NO_PROXY", os.environ.get("no_proxy", "")).strip(),
        retry_max=int(os.environ.get("RETRY_MAX", "2")),
        retry_backoff=float(os.environ.get("RETRY_BACKOFF", "0.5")),
        proxy_api_key=os.environ.get("PROXY_API_KEY", "").strip(),
        models=tuple(models),
        default_model=models[0],
        embeddings_model=os.environ.get("NVIDIA_EMBEDDINGS_MODEL", "nvidia/nv-embedqa-e5-v5"),
        embeddings_input_type=os.environ.get("EMBEDDINGS_INPUT_TYPE", "query").strip(),
    )
