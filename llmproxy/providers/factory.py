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


def build_providers(settings, logger, metrics=None):
    """Instantiate every configured provider and return the wired registry.

    Raises:
        ValueError: on an unknown provider ``type`` or an exposed-name collision.
    """
    providers = []
    model_specs = {}
    for cfg in settings.providers:
        provider_cls = _TYPES.get(cfg.type)
        if provider_cls is None:
            raise ValueError(
                f"unknown provider type '{cfg.type}' for provider '{cfg.name}' "
                f"(known: {', '.join(sorted(_TYPES))})"
            )
        providers.append(provider_cls(cfg, settings, logger, metrics))
        model_specs[cfg.name] = {"chat": cfg.models, "embeddings": cfg.embeddings_models}

    registry = ProviderRegistry.build(providers, model_specs)
    logger.info(
        "Provider configurati: %s | modelli esposti: %d",
        ", ".join(p.name for p in providers), len(registry.models),
    )
    return registry
