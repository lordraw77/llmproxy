"""Build the :class:`~llmproxy.providers.registry.ProviderRegistry` from config.

Maps each :class:`~llmproxy.config.ProviderConfig` ``type`` to a concrete
:class:`~llmproxy.providers.base.Provider` subclass, instantiates one per
configured provider (sharing the global retry/proxy/timeout settings), and hands
the lot to :meth:`ProviderRegistry.build` together with each provider's model
specs.
"""

from .anthropic import AnthropicProvider
from .azure import AzureOpenAIProvider
from .gemini import GeminiProvider
from .openai_compatible import OpenAICompatibleProvider
from .registry import ProviderRegistry

_TYPES = {
    "openai_compatible": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
    "nvidia": OpenAICompatibleProvider,
    "mistral": OpenAICompatibleProvider,
    "ollama": OpenAICompatibleProvider,
    "azure": AzureOpenAIProvider,
    "azure_openai": AzureOpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "google": GeminiProvider,
}


def _has_credential(cfg):
    """Whether ``cfg`` carries a non-empty API credential.

    The credential is already wrapped in its auth-header value at config time
    (``Bearer <key>`` for the OpenAI family, the bare key elsewhere), so the
    scheme prefix is stripped before checking for emptiness. An unresolved
    ``${ENV_VAR}`` reference expands to an empty string, which is the case this
    catches.
    """
    value = (cfg.auth_value or "").strip()
    if value.lower().startswith("bearer"):
        value = value[len("bearer"):].strip()
    return bool(value)


def build_providers(settings, logger, metrics=None):
    """Instantiate every configured provider and return the wired registry.

    A provider without a credential is reported as a warning rather than an
    error: a local upstream (Ollama, vLLM, LM Studio) legitimately needs none,
    and a missing key for a remote one surfaces as an upstream 401 that the error
    handler already propagates.

    Raises:
        ValueError: on an unknown provider ``type`` or an exposed-name collision.
    """
    providers = []
    model_specs = {}
    unauthenticated = []
    for cfg in settings.providers:
        provider_cls = _TYPES.get(cfg.type)
        if provider_cls is None:
            raise ValueError(
                f"unknown provider type '{cfg.type}' for provider '{cfg.name}' "
                f"(known: {', '.join(sorted(_TYPES))})"
            )
        if not _has_credential(cfg):
            unauthenticated.append(cfg.name)
        providers.append(provider_cls(cfg, settings, logger, metrics))
        model_specs[cfg.name] = {"chat": cfg.models, "embeddings": cfg.embeddings_models}

    if unauthenticated:
        logger.warning(
            "Provider senza credenziale configurata: %s | le chiamate falliranno con "
            "401 se l'upstream richiede autenticazione (verifica api_key e le env var "
            "referenziate come ${...})",
            ", ".join(unauthenticated),
        )

    registry = ProviderRegistry.build(providers, model_specs)
    logger.info(
        "Provider configurati: %s | modelli esposti: %d",
        ", ".join(p.name for p in providers), len(registry.models),
    )
    return registry
