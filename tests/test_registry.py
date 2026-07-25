"""Unit tests for :class:`llmproxy.providers.registry.ProviderRegistry`.

The naming rule is the piece of the multi-provider migration with the widest
blast radius (every exposed model name and every routing decision goes through
it) and had no coverage at all. The multi-provider cases mirror the real
configuration described in the plan: ``llama-3.3-70b`` served at once by Groq,
Cerebras and OpenRouter.
"""

import pytest

from llmproxy.providers.registry import ProviderRegistry


class FakeProvider:
    """Stand-in for a :class:`~llmproxy.providers.base.Provider`.

    The registry only ever reads ``name`` and stores the object, so a bare
    name-holder exercises the whole naming and routing logic without opening a
    connection pool.
    """

    def __init__(self, name):
        self.name = name


def build(spec):
    """Build a registry from ``{provider_name: {"chat": [...], "embeddings": [...]}}``.

    Model entries may be bare ids or ``(id, alias)`` pairs. Provider order is the
    declaration order of ``spec``, which is what decides the default model and the
    winner of a bare-id collision.
    """
    providers, model_specs = [], {}
    for name, kinds in spec.items():
        providers.append(FakeProvider(name))
        model_specs[name] = {
            kind: [m if isinstance(m, tuple) else (m, None) for m in kinds.get(kind, [])]
            for kind in ("chat", "embeddings")
        }
    return ProviderRegistry.build(providers, model_specs)


# --- naming: single provider -----------------------------------------------

def test_single_provider_exposes_bare_names():
    """The pre-multi-provider behaviour must be preserved exactly for one provider."""
    reg = build({"nvidia": {"chat": ["meta/llama-3.1-8b-instruct", "qwen/qwen3-8b"]}})
    assert reg.models == ("meta/llama-3.1-8b-instruct", "qwen/qwen3-8b")
    assert reg.default_model == "meta/llama-3.1-8b-instruct"
    assert reg.has("meta/llama-3.1-8b-instruct")
    assert not reg.has("nvidia:meta/llama-3.1-8b-instruct")


def test_alias_overrides_the_bare_name_with_a_single_provider():
    reg = build({"nvidia": {"chat": [("meta/llama-3.1-8b-instruct", "fast")]}})
    assert reg.models == ("fast",)
    assert reg.has("fast")
    assert not reg.has("meta/llama-3.1-8b-instruct")


# --- naming: multiple providers --------------------------------------------

def test_two_providers_switch_to_prefixed_names():
    reg = build({
        "groq": {"chat": ["llama-3.3-70b"]},
        "cerebras": {"chat": ["llama-3.3-70b"]},
    })
    assert reg.models == ("groq:llama-3.3-70b", "cerebras:llama-3.3-70b")
    assert not reg.has("llama-3.3-70b"), "the bare name is not exposed when disambiguating"


def test_prefix_leaves_slashes_in_the_model_id_intact():
    """The separator is ':' precisely so ids containing '/' survive unchanged."""
    reg = build({
        "nvidia": {"chat": ["meta/llama-3.1-8b-instruct"]},
        "groq": {"chat": ["llama-3.3-70b"]},
    })
    assert reg.has("nvidia:meta/llama-3.1-8b-instruct")


def test_alias_overrides_the_prefix_too():
    reg = build({
        "groq": {"chat": [("llama-3.3-70b", "llama-fast")]},
        "cerebras": {"chat": ["llama-3.3-70b"]},
    })
    assert reg.models == ("llama-fast", "cerebras:llama-3.3-70b")


# --- collisions ------------------------------------------------------------

def test_two_aliases_colliding_fail_fast():
    with pytest.raises(ValueError, match="model name collision"):
        build({
            "groq": {"chat": [("llama-3.3-70b", "llama")]},
            "cerebras": {"chat": [("llama-3.3-70b", "llama")]},
        })


def test_collision_message_names_both_providers():
    with pytest.raises(ValueError) as excinfo:
        build({
            "groq": {"chat": [("m", "shared")]},
            "cerebras": {"chat": [("m", "shared")]},
        })
    message = str(excinfo.value)
    assert "groq" in message and "cerebras" in message and "alias" in message


def test_alias_colliding_with_a_bare_name_fails_fast():
    """A single provider exposes bare names, so an alias can shadow a sibling."""
    with pytest.raises(ValueError, match="model name collision"):
        build({"nvidia": {"chat": ["a", ("b", "a")]}})


def test_a_chat_and_an_embeddings_model_share_one_name_space():
    with pytest.raises(ValueError, match="model name collision"):
        build({"nvidia": {"chat": [("m", "shared")], "embeddings": [("e", "shared")]}})


# --- resolution ------------------------------------------------------------

def test_resolve_returns_an_exact_exposed_name_unchanged():
    reg = build({"groq": {"chat": ["a"]}, "cerebras": {"chat": ["b"]}})
    assert reg.resolve("cerebras:b") == "cerebras:b"


def test_resolve_falls_back_to_the_default_for_an_unknown_name():
    reg = build({"nvidia": {"chat": ["a", "b"]}})
    assert reg.resolve("does-not-exist") == "a"
    assert reg.resolve(None) == "a"
    assert reg.resolve("") == "a"


def test_bare_native_id_resolves_to_the_first_declaring_provider():
    """Back-compat path: a client that still sends the unprefixed id must be served."""
    reg = build({
        "groq": {"chat": ["llama-3.3-70b"]},
        "cerebras": {"chat": ["llama-3.3-70b"]},
        "openrouter": {"chat": ["llama-3.3-70b"]},
    })
    assert reg.resolve("llama-3.3-70b") == "groq:llama-3.3-70b"


def test_an_alias_does_not_hide_the_bare_native_id():
    reg = build({
        "groq": {"chat": [("llama-3.3-70b", "fast")]},
        "cerebras": {"chat": ["other"]},
    })
    assert reg.resolve("llama-3.3-70b") == "fast"


def test_provider_for_returns_the_owner_and_the_native_id():
    reg = build({
        "groq": {"chat": [("llama-3.3-70b", "fast")]},
        "cerebras": {"chat": ["llama-3.3-70b"]},
    })
    provider, model_id = reg.provider_for("fast")
    assert provider.name == "groq"
    assert model_id == "llama-3.3-70b", "the upstream must receive its own native id"


def test_provider_for_routes_the_three_owners_of_one_id_distinctly():
    reg = build({
        "groq": {"chat": ["llama-3.3-70b"]},
        "cerebras": {"chat": ["llama-3.3-70b"]},
        "openrouter": {"chat": ["llama-3.3-70b"]},
    })
    for name in ("groq", "cerebras", "openrouter"):
        provider, model_id = reg.provider_for(f"{name}:llama-3.3-70b")
        assert (provider.name, model_id) == (name, "llama-3.3-70b")


def test_provider_for_an_unknown_name_falls_back_to_the_default():
    reg = build({"nvidia": {"chat": ["a", "b"]}})
    provider, model_id = reg.provider_for("unknown")
    assert (provider.name, model_id) == ("nvidia", "a")


# --- embeddings ------------------------------------------------------------

def test_embeddings_default_is_the_first_declared_one():
    reg = build({"nvidia": {"chat": ["c"], "embeddings": ["e1", "e2"]}})
    assert reg.embeddings_model == "e1"
    assert reg.resolve_embeddings(None) == "e1"
    assert reg.resolve_embeddings("") == "e1"


def test_resolve_embeddings_accepts_the_native_id_under_an_alias():
    reg = build({"nvidia": {"chat": ["c"], "embeddings": [("nv-embedqa-e5-v5", "embed")]}})
    assert reg.resolve_embeddings("embed") == "embed"
    assert reg.resolve_embeddings("nv-embedqa-e5-v5") == "embed"


def test_embeddings_provider_for_returns_the_native_id():
    reg = build({"nvidia": {"chat": ["c"], "embeddings": [("nv-embedqa-e5-v5", "embed")]}})
    provider, model_id = reg.embeddings_provider_for("embed")
    assert (provider.name, model_id) == ("nvidia", "nv-embedqa-e5-v5")


def test_embeddings_provider_for_an_unknown_model_raises_value_error():
    """F7 turns this ValueError into a 400; here we pin that it is raised at all."""
    reg = build({"nvidia": {"chat": ["c"]}})
    with pytest.raises(ValueError, match="no provider serves embeddings model"):
        reg.embeddings_provider_for("nope")


# --- degenerate configuration ----------------------------------------------

def test_a_provider_with_no_models_yields_an_empty_catalogue():
    reg = build({"nvidia": {}})
    assert reg.models == ()
    assert reg.default_model == ""
    assert reg.embeddings_model == ""
    assert reg.has("anything") is False
