"""Application service for embeddings."""


class EmbeddingService:
    """Forwards embeddings payloads to the upstream ``/embeddings`` endpoint."""

    def __init__(self, upstream, registry, input_type, cache=None):
        self._upstream = upstream
        self._registry = registry
        self._input_type = input_type
        self._cache = cache

    def resolve_model(self, requested):
        """Resolve the embeddings model for a request."""
        return self._registry.resolve_embeddings(requested)

    def with_input_type(self, payload):
        """Return ``payload`` with the default ``input_type`` applied when absent."""
        if self._input_type and "input_type" not in payload:
            payload["input_type"] = self._input_type
        return payload

    def embed(self, payload, rid):
        """POST ``payload`` to the upstream ``/embeddings`` endpoint and return the response.

        Embeddings are deterministic, so identical payloads are served from the
        response cache when it is enabled (see :mod:`llmproxy.cache`), skipping the
        upstream call entirely.
        """
        cache = self._cache
        if cache is None or not cache.enabled:
            return self._upstream.post(payload, stream=False, rid=rid, path="/embeddings")

        from ..cache import CachedResponse
        from ..upstream.client import resp_json

        key = cache.make_key("embeddings", payload)
        hit = cache.get(key)
        if hit is not None:
            self._upstream.log_cache_hit(rid, key)
            return CachedResponse(hit)

        resp = self._upstream.post(payload, stream=False, rid=rid, path="/embeddings")
        if getattr(resp, "ok", False):
            try:
                cache.set(key, resp_json(resp))
            except ValueError:
                pass
        return resp
