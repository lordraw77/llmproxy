"""Dependency container wired at application start-up.

Holds the fully-constructed collaborators (settings, logger, registry, services,
upstream client) and is attached to the Flask app under ``app.extensions``. Route
modules retrieve it via :func:`deps` instead of importing module-level globals,
which keeps the wiring explicit and the app testable.
"""

from flask import current_app

_EXT_KEY = "llmproxy"


class Container:
    """Bundle of application-scoped collaborators."""

    def __init__(self, settings, logger, registry, upstream, completions, embeddings, metrics, cache=None):
        self.settings = settings
        self.logger = logger
        self.registry = registry
        self.upstream = upstream
        self.completions = completions
        self.embeddings = embeddings
        self.metrics = metrics
        self.cache = cache

    def attach(self, app):
        """Register this container on ``app`` for later retrieval via :func:`deps`."""
        app.extensions[_EXT_KEY] = self


def deps():
    """Return the :class:`Container` attached to the current Flask app."""
    return current_app.extensions[_EXT_KEY]
