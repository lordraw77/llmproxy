"""Flask application factory.

Wires the configuration, logging, domain registry, upstream client, and services
into a :class:`~llmproxy.web.container.Container`, then registers the middleware,
error handlers, and route blueprints. Keeping construction in one factory makes
the dependency graph explicit and the app straightforward to instantiate in tests.
"""

from flask import Flask

from ..cache import ResponseCache
from ..config import load_settings
from ..domain.models import ModelRegistry
from ..logging_setup import configure_logging
from ..metrics import MetricsCollector
from ..services.completions import CompletionService
from ..services.embeddings import EmbeddingService
from ..upstream.client import NvidiaUpstream
from .container import Container
from .errors import register_error_handlers
from .middleware import register_middleware
from .routes import health, llamacpp, ollama, openai, stats


def create_app(settings=None):
    """Build and return the configured Flask application."""
    settings = settings or load_settings()
    logger = configure_logging(settings)

    registry = ModelRegistry.from_settings(settings)
    metrics = MetricsCollector()
    cache = ResponseCache(
        enabled=settings.cache_enabled,
        ttl=settings.cache_ttl,
        max_size=settings.cache_max_size,
    )
    upstream = NvidiaUpstream(settings, logger, metrics)
    completions = CompletionService(upstream, settings.default_model, cache=cache)
    embeddings = EmbeddingService(upstream, registry, settings.embeddings_input_type, cache=cache)

    app = Flask(__name__)
    Container(
        settings, logger, registry, upstream, completions, embeddings, metrics, cache
    ).attach(app)

    register_middleware(app)
    register_error_handlers(app)
    for module in (ollama, openai, llamacpp, health, stats):
        app.register_blueprint(module.bp)

    return app
