"""Tests for :func:`llmproxy.config.load_settings` and R7.

``Settings`` used to carry ``models`` / ``default_model`` / ``embeddings_model``
/ ``nvidia_api_base`` / ``nvidia_api_key`` beside the providers built from them.
With a ``providers.toml`` those fields were stale — the direct cause of ``F2``
(a guard on a key that need not exist) and ``F8`` (a start-up banner listing
models that are not exposed). They are now construction inputs only.

``load_dotenv`` is neutralized in every test here: left alone it would read the
developer's real ``.env``.
"""

import pytest

from llmproxy import config
from llmproxy.config import Settings, load_settings

LEGACY_FIELDS = ("models", "default_model", "embeddings_model",
                 "nvidia_api_base", "nvidia_api_key")

ENV_VARS = ("NVIDIA_MODEL", "NVIDIA_MODELS", "NVIDIA_API_KEY", "NVIDIA_API_BASE",
            "NVIDIA_EMBEDDINGS_MODEL", "PROVIDERS_CONFIG", "CACHE_POLICY",
            "PROXY_API_KEY", "HOST", "PORT")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """An isolated environment: no ``.env``, no ambient ``NVIDIA_*``, no toml."""
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Point at a path that does not exist, so the env-var fallback is taken
    # regardless of the developer's working directory.
    monkeypatch.setenv("PROVIDERS_CONFIG", str(tmp_path / "absent.toml"))
    return monkeypatch


# --- R7: one catalogue, in the registry ------------------------------------

@pytest.mark.parametrize("name", LEGACY_FIELDS)
def test_settings_no_longer_carries_a_model_catalogue(env, name):
    """A second copy of the catalogue is what F2 and F8 were made of."""
    settings = load_settings()

    assert not hasattr(settings, name)


def test_the_catalogue_is_reachable_only_through_the_providers(env):
    env.setenv("NVIDIA_MODELS", "a,b")
    settings = load_settings()

    assert [m for m, _ in settings.providers[0].models] == ["a", "b"]


# --- back-compat: the NVIDIA_* fallback still configures a provider ---------

def test_env_vars_synthesize_a_single_provider(env):
    env.setenv("NVIDIA_API_KEY", "nv-secret")
    env.setenv("NVIDIA_API_BASE", "https://example.invalid/v1")
    env.setenv("NVIDIA_MODELS", "meta/llama-3.3-70b, meta/llama-3.1-8b")
    env.setenv("NVIDIA_EMBEDDINGS_MODEL", "nvidia/nv-embedqa-e5-v5")

    (provider,) = load_settings().providers

    assert provider.name == "nvidia"
    assert provider.type == "openai_compatible"
    assert provider.base_url == "https://example.invalid/v1"
    assert provider.auth_value == "Bearer nv-secret"
    assert [m for m, _ in provider.models] == ["meta/llama-3.3-70b", "meta/llama-3.1-8b"]
    assert [m for m, _ in provider.embeddings_models] == ["nvidia/nv-embedqa-e5-v5"]


def test_the_single_model_variable_is_the_fallback_for_the_list(env):
    env.setenv("NVIDIA_MODEL", "only-one")

    (provider,) = load_settings().providers

    assert [m for m, _ in provider.models] == ["only-one"]


def test_a_default_model_exists_with_no_configuration_at_all(env):
    """Zero-config must still expose something to route to."""
    (provider,) = load_settings().providers

    assert provider.models, "the historic default model is gone"


def test_providers_toml_replaces_the_env_var_fallback(env, tmp_path):
    toml = tmp_path / "providers.toml"
    toml.write_text(
        '[[provider]]\n'
        'name = "groq"\n'
        'api_key = "gsk_x"\n'
        'base_url = "https://api.groq.com/openai/v1"\n'
        'models = ["llama-3.3-70b"]\n'
    )
    env.setenv("PROVIDERS_CONFIG", str(toml))
    env.setenv("NVIDIA_MODELS", "ignored-when-toml-present")

    (provider,) = load_settings().providers

    assert provider.name == "groq"
    assert [m for m, _ in provider.models] == ["llama-3.3-70b"]


def test_toml_interpolates_env_references(env, tmp_path):
    toml = tmp_path / "providers.toml"
    toml.write_text(
        '[[provider]]\nname = "groq"\napi_key = "${GROQ_TOKEN}"\n'
        'base_url = "https://api.groq.com/openai/v1"\nmodels = ["m"]\n'
    )
    env.setenv("PROVIDERS_CONFIG", str(toml))
    env.setenv("GROQ_TOKEN", "gsk_from_env")

    (provider,) = load_settings().providers

    assert provider.auth_value == "Bearer gsk_from_env"


# --- R11: an unresolved ${ENV_VAR} is reported, not swallowed ---------------

def _toml_with_refs(tmp_path, api_key='"${ABSENT_TOKEN}"'):
    toml = tmp_path / "providers.toml"
    toml.write_text(
        f'[[provider]]\nname = "p"\napi_key = {api_key}\n'
        'base_url = "https://api.invalid/v1"\nmodels = ["m"]\n'
    )
    return toml


def test_an_unset_reference_is_collected(env, tmp_path):
    """It still expands to "", but the name is no longer lost."""
    env.delenv("ABSENT_TOKEN", raising=False)
    env.setenv("PROVIDERS_CONFIG", str(_toml_with_refs(tmp_path)))

    settings = load_settings()

    assert settings.unresolved_env == ("ABSENT_TOKEN",)
    assert settings.providers[0].auth_value == "Bearer "


def test_a_resolved_reference_is_not_reported(env, tmp_path):
    env.setenv("ABSENT_TOKEN", "tok")
    env.setenv("PROVIDERS_CONFIG", str(_toml_with_refs(tmp_path)))

    assert load_settings().unresolved_env == ()


def test_an_empty_but_defined_variable_is_not_reported(env, tmp_path):
    """Deliberately empty is a choice; undefined is a typo. Only the second warns."""
    env.setenv("ABSENT_TOKEN", "")
    env.setenv("PROVIDERS_CONFIG", str(_toml_with_refs(tmp_path)))

    assert load_settings().unresolved_env == ()


def test_every_unresolved_name_is_reported_once_and_sorted(env, tmp_path):
    toml = tmp_path / "providers.toml"
    toml.write_text(
        '[[provider]]\nname = "a"\napi_key = "${ZED_KEY}"\n'
        'base_url = "${HOST_REF}/v1"\nmodels = ["m"]\n'
        '[[provider]]\nname = "b"\napi_key = "${ZED_KEY}"\n'
        'base_url = "https://api.invalid/v1"\nmodels = ["m2"]\n'
    )
    for name in ("ZED_KEY", "HOST_REF"):
        env.delenv(name, raising=False)
    env.setenv("PROVIDERS_CONFIG", str(toml))

    assert load_settings().unresolved_env == ("HOST_REF", "ZED_KEY")


class _RecordingLogger:
    """Stands in for the app logger, which does not propagate to caplog."""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        pass


@pytest.mark.parametrize("unresolved,expected", [
    (("GROQ_TOKEN", "NVIDIA_API_KEY"), 1),
    ((), 0),
])
def test_create_app_warns_about_the_unresolved_names(monkeypatch, unresolved, expected):
    """The warning belongs to app construction: under gunicorn nothing else runs."""
    import llmproxy.web as web

    from .conftest import make_settings

    logger = _RecordingLogger()
    monkeypatch.setattr(web, "configure_logging", lambda _s: logger)
    web.create_app(make_settings(unresolved_env=unresolved))

    assert len(logger.warnings) == expected
    for name in unresolved:
        assert name in logger.warnings[0]


# --- R12: frozen means frozen, mapping fields included ----------------------

def test_the_mapping_fields_of_a_provider_config_are_read_only(env, tmp_path):
    """`frozen=True` blocked rebinding only: the dicts behind it stayed writable."""
    toml = tmp_path / "providers.toml"
    toml.write_text(
        '[[provider]]\nname = "a"\ntype = "anthropic"\napi_key = "k"\nmodels = ["m"]\n'
        '[provider.proxy]\nhttps = "http://egress:3128"\n'
    )
    env.setenv("PROVIDERS_CONFIG", str(toml))

    (provider,) = load_settings().providers

    assert provider.extra_headers["anthropic-version"] == "2023-06-01"
    assert provider.proxy["https"] == "http://egress:3128"
    with pytest.raises(TypeError):
        provider.extra_headers["x"] = "1"
    with pytest.raises(TypeError):
        provider.proxy["http"] = "1"


def test_a_provider_config_does_not_share_the_dict_it_was_built_from():
    from llmproxy.config import ProviderConfig

    headers = {"x": "1"}
    config = ProviderConfig(
        name="a", type="openai_compatible", base_url="https://x.invalid",
        auth_header="Authorization", auth_value="Bearer k", extra_headers=headers,
    )
    headers["x"] = "mutated"

    assert config.extra_headers["x"] == "1"


# --- the fields that did stay ----------------------------------------------

def test_the_global_knobs_are_still_settings_fields(env):
    env.setenv("PROXY_API_KEY", " secret ")
    env.setenv("CACHE_POLICY", "ALL")
    settings = load_settings()

    assert settings.proxy_api_key == "secret"
    assert settings.cache_policy == "all"
    assert settings.upstream_timeout == 120.0


def test_settings_is_immutable():
    """Configuration is a snapshot: nothing rebinds it after start-up."""
    settings = Settings(
        host="127.0.0.1", port=1, log_level="INFO", log_tz="UTC", log_tzinfo=None,
        upstream_timeout=1.0, pool_size=1, retry_max=0, retry_backoff=0.0,
    )

    with pytest.raises(Exception):
        settings.port = 2
