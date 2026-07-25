"""Application configuration.

All environment-variable parsing lives here, isolated from the rest of the
codebase. The rest of the application depends on the immutable :class:`Settings`
object rather than reading ``os.environ`` directly, which keeps the domain,
service, and web layers free of environment coupling and trivially testable.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.9/3.10 fallback
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None


# Default base URL per provider type (used when a provider omits ``base_url``).
_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}

_ENV_REF = re.compile(r"\$\{([^}]+)\}")


def _interp(value):
    """Expand ``${ENV_VAR}`` references in a string against the environment."""
    if not isinstance(value, str):
        return value
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _auth_for(ptype, api_key):
    """Return ``(auth_header, auth_value, extra_headers)`` for a provider type."""
    if ptype in ("anthropic",):
        return "x-api-key", api_key, {"anthropic-version": "2023-06-01"}
    if ptype in ("gemini", "google"):
        return "x-goog-api-key", api_key, {}
    if ptype in ("azure", "azure_openai"):
        return "api-key", api_key, {}
    # OpenAI-compatible family.
    return "Authorization", f"Bearer {api_key}", {}


def _normalize_models(raw):
    """Normalize a TOML ``models`` list into ``[(model_id, alias_or_None), ...]``.

    Each item is either a bare string or a ``{id, alias}`` table.
    """
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append((item, None))
        elif isinstance(item, dict) and item.get("id"):
            out.append((item["id"], item.get("alias")))
    return tuple(out)


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
class ProviderConfig:
    """Immutable description of one upstream provider.

    Consumed by :class:`~llmproxy.providers.base.Provider` (base_url, auth headers,
    proxy, timeout, api_version, max_tokens) and by the registry (name, models).
    """

    name: str
    type: str
    base_url: str
    auth_header: str
    auth_value: str
    extra_headers: dict = field(default_factory=dict)
    models: tuple = ()            # ((model_id, alias|None), ...)
    embeddings_models: tuple = ()  # ((model_id, alias|None), ...)
    timeout: float = 0.0          # 0 = use the global upstream_timeout
    api_version: str = ""
    max_tokens: int = 4096        # required by Anthropic; ignored by OpenAI-family
    proxy: dict = None            # per-provider proxies, else the global ones


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

    # Response cache (non-streaming replies only). Disabled by default.
    cache_enabled: bool = False
    cache_ttl: float = 300.0
    cache_max_size: int = 512

    # Configured upstream providers (from providers.toml, or a single provider
    # synthesized from the NVIDIA_* env vars as a backward-compatible fallback).
    providers: tuple = ()


def _provider_from_dict(d):
    """Build a :class:`ProviderConfig` from one ``[[provider]]`` TOML table."""
    ptype = (d.get("type") or "openai_compatible").strip()
    api_key = _interp(d.get("api_key", ""))
    base_url = _interp(d.get("base_url", "")) or _PROVIDER_BASE_URLS.get(ptype, "")
    auth_header, auth_value, extra = _auth_for(ptype, api_key)
    extra.update(d.get("extra_headers") or {})
    return ProviderConfig(
        name=d.get("name", ptype),
        type=ptype,
        base_url=base_url.rstrip("/"),
        auth_header=auth_header,
        auth_value=auth_value,
        extra_headers=extra,
        models=_normalize_models(d.get("models")),
        embeddings_models=_normalize_models(d.get("embeddings_models")),
        timeout=float(d.get("timeout", 0) or 0),
        api_version=str(d.get("api_version", "")),
        max_tokens=int(d.get("max_tokens", 4096)),
        proxy=d.get("proxy") or None,
    )


def _providers_from_toml(path):
    """Parse a ``providers.toml`` file into a tuple of :class:`ProviderConfig`."""
    if _toml is None:
        raise RuntimeError(
            "reading providers.toml requires the 'tomli' package on Python < 3.11 "
            "(pip install tomli)"
        )
    with open(path, "rb") as fh:
        doc = _toml.load(fh)
    entries = doc.get("provider") or doc.get("providers") or []
    return tuple(_provider_from_dict(d) for d in entries)


def _provider_from_env(base, key, models, embeddings_model):
    """Synthesize the single legacy NVIDIA provider from the ``NVIDIA_*`` env vars."""
    auth_header, auth_value, extra = _auth_for("openai_compatible", key)
    return ProviderConfig(
        name="nvidia",
        type="openai_compatible",
        base_url=base.rstrip("/"),
        auth_header=auth_header,
        auth_value=auth_value,
        extra_headers=extra,
        models=tuple((m, None) for m in models),
        embeddings_models=((embeddings_model, None),) if embeddings_model else (),
    )


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

    # Providers: a declarative providers.toml when present, otherwise a single
    # provider synthesized from the NVIDIA_* env vars (zero-config back-compat).
    nvidia_api_base = os.environ.get("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1")
    embeddings_model = os.environ.get("NVIDIA_EMBEDDINGS_MODEL", "nvidia/nv-embedqa-e5-v5")
    providers_path = os.environ.get("PROVIDERS_CONFIG", "providers.toml")
    if os.path.isfile(providers_path):
        providers = _providers_from_toml(providers_path)
    else:
        providers = (_provider_from_env(
            nvidia_api_base, os.environ.get("NVIDIA_API_KEY", ""), models, embeddings_model,
        ),)

    return Settings(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "11434")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_tz=log_tz,
        log_tzinfo=log_tzinfo,
        nvidia_api_base=nvidia_api_base,
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
        embeddings_model=embeddings_model,
        embeddings_input_type=os.environ.get("EMBEDDINGS_INPUT_TYPE", "query").strip(),
        cache_enabled=os.environ.get("CACHE_ENABLED", "false").strip().lower()
        in ("1", "true", "yes", "on"),
        cache_ttl=float(os.environ.get("CACHE_TTL", "300")),
        cache_max_size=int(os.environ.get("CACHE_MAX_SIZE", "512")),
        providers=providers,
    )
