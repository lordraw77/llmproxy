"""Application service for embeddings."""


class EmbeddingService:
    """Routes embeddings payloads to the provider that owns the requested model."""

    def __init__(self, registry, input_type, cache=None):
        self._registry = registry
        self._input_type = input_type
        self._cache = cache

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

        The payload carries the exposed model name; it is cached on that name and
        rewritten to the provider-native id before the upstream call. Embeddings are
        deterministic by construction, so they stay eligible under every
        ``CACHE_POLICY`` level except ``off`` (see :mod:`llmproxy.cache`).
        """
        provider, native_id = self._registry.embeddings_provider_for(payload["model"])
        cache = self._cache

        if cache is None or not cache.allows("embeddings", payload):
            return provider.post(self._with_native(payload, native_id), stream=False, rid=rid, path="/embeddings")

        from ..cache import CachedResponse
        from ..providers.base import resp_json

        key = cache.make_key("embeddings", payload)
        hit = cache.get(key)
        if hit is not None:
            provider.log_cache_hit(rid, key)
            return CachedResponse(hit)

        resp = provider.post(self._with_native(payload, native_id), stream=False, rid=rid, path="/embeddings")
        if getattr(resp, "ok", False):
            try:
                cache.set(key, resp_json(resp))
            except ValueError:
                pass
        return resp

    @staticmethod
    def _with_native(payload, native_id):
        """Return a copy of ``payload`` with the model set to the provider-native id."""
        out = dict(payload)
        out["model"] = native_id
        return out
