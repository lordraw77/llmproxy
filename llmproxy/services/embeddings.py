"""Application service for embeddings."""


class EmbeddingService:
    """Forwards embeddings payloads to the upstream ``/embeddings`` endpoint."""

    def __init__(self, upstream, registry, input_type):
        self._upstream = upstream
        self._registry = registry
        self._input_type = input_type

    def resolve_model(self, requested):
        """Resolve the embeddings model for a request."""
        return self._registry.resolve_embeddings(requested)

    def with_input_type(self, payload):
        """Return ``payload`` with the default ``input_type`` applied when absent."""
        if self._input_type and "input_type" not in payload:
            payload["input_type"] = self._input_type
        return payload

    def embed(self, payload, rid):
        """POST ``payload`` to the upstream ``/embeddings`` endpoint and return the response."""
        return self._upstream.post(payload, stream=False, rid=rid, path="/embeddings")
