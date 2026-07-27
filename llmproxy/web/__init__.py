"""Flask application factory.

Wires the configuration, logging, domain registry, upstream client, and services
into a :class:`~llmproxy.web.container.Container`, then registers the middleware,
error handlers, and route blueprints. Keeping construction in one factory makes
the dependency graph explicit and the app straightforward to instantiate in tests.
"""

import atexit

from flask import Flask

from ..audit import open_trail
from ..cache import ResponseCache
from ..config import load_settings
from ..logging_setup import configure_logging
from ..metrics import MetricsCollector
from ..providers import build_providers
from ..services.completions import CompletionService
from ..services.embeddings import EmbeddingService
from .container import Container
from .errors import register_error_handlers
from .middleware import register_middleware
from .routes import health, llamacpp, ollama, openai, stats


def create_app(settings=None):
    """Build and return the configured Flask application."""
    settings = settings or load_settings()
    logger = configure_logging(settings)
    if settings.unresolved_env:
        # Warned here rather than in load_settings, which runs before logging is
        # configured. Under gunicorn this is the only start-up path there is.
        logger.warning(
            "providers.toml references environment variables that are not set: %s. "
            "They expanded to an empty string — a provider whose api_key ended up "
            "empty will fail with 401 on its first request.",
            ", ".join(settings.unresolved_env),
        )

    metrics = MetricsCollector()
    cache = ResponseCache(
        enabled=settings.cache_enabled,
        ttl=settings.cache_ttl,
        max_size=settings.cache_max_size,
        policy=settings.cache_policy,
    )
    # Started here (post-fork under gunicorn, so the writer thread belongs to the
    # worker that will feed it) and drained at interpreter exit; the queue is
    # flushed whenever it runs dry, so a hard kill loses at most what is in it.
    audit = open_trail(settings, logger)
    if audit.enabled:
        atexit.register(audit.close)
        logger.info(
            "audit trail enabled | file=%s format=%s bodies=%s",
            audit.path, audit.format, audit.options.bodies,
        )

    registry = build_providers(settings, logger, metrics)
    completions = CompletionService(registry, cache=cache)
    embeddings = EmbeddingService(registry, settings.embeddings_input_type, cache=cache)

    app = Flask(__name__)
    Container(
        settings, logger, registry, completions, embeddings, metrics, cache, audit
    ).attach(app)

    register_middleware(app)
    register_error_handlers(app)
    for module in (ollama, openai, llamacpp, health, stats):
        app.register_blueprint(module.bp)

    return app
