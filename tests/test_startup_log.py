"""Regression tests for F8: the start-up summary must describe the registry.

Before the fix ``main()`` logged ``settings.models`` / ``settings.default_model``,
which derived from the ``NVIDIA_*`` env vars. With a ``providers.toml`` in place
those fields were stale: the banner advertised models that are not exposed and
omitted the ones that are.

``R7`` then removed those fields outright, so the summary can no longer read a
second catalogue even by accident. What is left to pin is that it reports the
registry's catalogue faithfully — including when that catalogue is empty.
"""

from llmproxy.banner import log_startup
from llmproxy.providers.registry import ProviderRegistry, _Entry

from .conftest import make_settings


class _FakeProvider:
    def __init__(self, name):
        self.name = name


class _RecordingLogger:
    """Collects the interpolated messages passed to ``logger.info``."""

    def __init__(self):
        self.lines = []

    def info(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    @property
    def text(self):
        return "\n".join(self.lines)


def _registry(chat, embeddings=()):
    providers = {name for name, _ in chat} | {name for name, _ in embeddings}
    return ProviderRegistry(
        [_FakeProvider(n) for n in sorted(providers)],
        [_Entry(exposed, _FakeProvider(p), exposed.split(":")[-1]) for p, exposed in chat],
        [_Entry(exposed, _FakeProvider(p), exposed.split(":")[-1]) for p, exposed in embeddings],
    )


def test_startup_log_lists_the_registry_catalogue():
    """Every exposed model is named, chat and embeddings alike."""
    settings = make_settings()
    registry = _registry(
        chat=[("groq", "groq:llama-3.3-70b"), ("gemini", "gemini:gemini-2.0-flash")],
        embeddings=[("gemini", "gemini:text-embedding-004")],
    )
    logger = _RecordingLogger()

    log_startup(logger, settings, registry)

    assert "test-model" not in logger.text, "the fixture's own provider is not this registry"
    assert "groq:llama-3.3-70b" in logger.text
    assert "gemini:gemini-2.0-flash" in logger.text
    assert "gemini:text-embedding-004" in logger.text


def test_startup_log_reports_registry_default_and_providers():
    settings = make_settings()
    registry = _registry(chat=[("groq", "groq:llama-3.3-70b"), ("cerebras", "cerebras:llama-3.3-70b")])
    logger = _RecordingLogger()

    log_startup(logger, settings, registry)

    assert "Default: groq:llama-3.3-70b" in logger.text
    assert "Provider configurati: cerebras, groq" in logger.text


def test_startup_log_survives_an_empty_registry():
    """An empty catalogue must not blow up the entrypoint (nor print an empty list)."""
    logger = _RecordingLogger()

    log_startup(logger, make_settings(), _registry(chat=[]))

    assert "Modelli esposti: nessuno" in logger.text
    assert "Default: nessuno" in logger.text


def test_startup_log_reports_auth_and_cache_state():
    logger = _RecordingLogger()
    log_startup(
        logger,
        make_settings(proxy_api_key="secret", cache_enabled=True, cache_ttl=60.0, cache_max_size=8),
        _registry(chat=[("test", "m")]),
    )

    assert "Autenticazione in ingresso: ATTIVA" in logger.text
    assert "Cache risposte: attiva (policy=deterministic, ttl=60s, max=8)" in logger.text
