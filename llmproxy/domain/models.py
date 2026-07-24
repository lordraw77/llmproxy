"""Model registry: the set of exposed models and the resolution rules.

Pure domain logic, free of Flask/HTTP/environment concerns.
"""


class ModelRegistry:
    """Holds the exposed chat models and the embeddings default, and resolves requests."""

    def __init__(self, models, default_model, embeddings_model):
        self.models = tuple(models)
        self.default_model = default_model
        self.embeddings_model = embeddings_model

    def resolve(self, requested):
        """Return the requested model if it is exposed, otherwise the default."""
        if requested and requested in self.models:
            return requested
        return self.default_model

    def resolve_embeddings(self, requested):
        """For embeddings, use the requested model as-is, otherwise the dedicated default."""
        return requested or self.embeddings_model

    def has(self, model_id):
        """Whether ``model_id`` is one of the exposed chat models."""
        return model_id in self.models

    @classmethod
    def from_settings(cls, settings):
        """Build a registry from a :class:`~llmproxy.config.Settings`."""
        return cls(settings.models, settings.default_model, settings.embeddings_model)
