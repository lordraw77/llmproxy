"""Shared fixtures and builders for the unit test suite.

Everything here is offline: no network, no ``.env``, no ``providers.toml``.
:class:`~llmproxy.config.Settings` objects are constructed directly rather than
through :func:`~llmproxy.config.load_settings`, which calls ``load_dotenv()`` and
would leak the developer's real credentials and provider file into the tests.
"""

from datetime import timezone

import pytest

from llmproxy.config import ProviderConfig, Settings


def make_provider_config(name, models=(), embeddings_models=(), api_key="k", type="openai_compatible"):
    """Build a :class:`ProviderConfig` with the auth wrapping the config layer applies.

    ``models``/``embeddings_models`` accept bare ids or ``(id, alias)`` pairs and
    are normalized to the ``((id, alias|None), ...)`` shape the registry expects.
    """
    def norm(items):
        return tuple(m if isinstance(m, tuple) else (m, None) for m in items)

    return ProviderConfig(
        name=name,
        type=type,
        base_url="https://upstream.invalid/v1",
        auth_header="Authorization",
        auth_value=f"Bearer {api_key}",
        models=norm(models),
        embeddings_models=norm(embeddings_models),
    )


def make_settings(**overrides):
    """Build a :class:`Settings` with test-safe defaults, overridable field by field."""
    base = dict(
        host="127.0.0.1",
        port=11434,
        log_level="CRITICAL",  # keep the test output clean
        log_tz="UTC",
        log_tzinfo=timezone.utc,
        nvidia_api_base="https://upstream.invalid/v1",
        nvidia_api_key="test-key",
        upstream_timeout=1.0,
        pool_size=2,
        retry_max=0,
        retry_backoff=0.0,
        models=("test-model",),
        default_model="test-model",
        embeddings_model="test-embed",
    )
    base.update(overrides)
    if "providers" not in base:
        base["providers"] = (
            make_provider_config(
                "test",
                models=base["models"],
                embeddings_models=(base["embeddings_model"],),
                api_key=base["nvidia_api_key"],
            ),
        )
    return Settings(**base)


@pytest.fixture
def app_factory():
    """Return a builder for a fully-wired Flask app from ad-hoc settings overrides."""
    from llmproxy.web import create_app

    def build(**overrides):
        return create_app(make_settings(**overrides))

    return build


@pytest.fixture
def client(app_factory):
    """Flask test client over an app with the default single test provider."""
    return app_factory().test_client()
