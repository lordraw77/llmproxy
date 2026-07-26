"""Application service for embeddings."""

from .routing import CachedRouter


class EmbeddingService:
    """Routes embeddings payloads to the provider that owns the requested model."""

    def __init__(self, registry, input_type, cache=None):
        self._registry = registry
        self._input_type = input_type
        self._router = CachedRouter(cache)

    def resolve_model(self, requested):
        """Resolve the embeddings model for a request to its exposed name."""
        return self._registry.resolve_embeddings(requested)

    def with_input_type(self, payload):
        """Return ``payload`` with the default ``input_type`` applied when absent."""
        if self._input_type and "input_type" not in payload:
            payload["input_type"] = self._input_type
        return payload

    def embed(self, payload, rid):
        """Route ``payload`` to its provider's ``/embeddings`` endpoint and return the response.

        Embeddings are deterministic by construction, so they stay eligible under
        every ``CACHE_POLICY`` level except ``off`` (see :mod:`llmproxy.cache`).
        """
        provider, native_id = self._registry.embeddings_provider_for(payload["model"])
        return self._router.send(
            provider, native_id, payload, "embeddings", stream=False, rid=rid,
            path="/embeddings",
        )
