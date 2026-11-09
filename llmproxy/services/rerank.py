"""Application service for reranking and pair scoring (vLLM/Jina/Cohere style)."""

from .routing import CachedRouter


class RerankService:
    """Routes ``/rerank`` and ``/score`` payloads to the provider owning the model."""

    def __init__(self, registry, cache=None):
        self._registry = registry
        self._router = CachedRouter(cache)

    def resolve_model(self, requested):
        """Resolve the rerank model for a request to its exposed name."""
        return self._registry.resolve_rerank(requested)

    def send(self, payload, rid, kind):
        """Forward ``payload`` to the upstream ``/rerank`` or ``/score`` endpoint.

        Args:
            payload: Request body carrying the exposed model name.
            rid: Correlation id for logging.
            kind: ``"rerank"`` or ``"score"``.
        """
        provider, native_id = self._registry.rerank_provider_for(payload["model"])
        return self._router.send(
            provider, native_id, payload, kind, stream=False, rid=rid, path=f"/{kind}",
        )
