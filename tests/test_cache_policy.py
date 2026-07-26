"""Regression tests for F10: the cache must not replay non-deterministic answers.

Before the fix any successful non-streaming chat completion entered the cache,
so a request with ``temperature > 0`` and no ``seed`` got the same sampled answer
for the whole TTL. Eligibility is now governed by ``CACHE_POLICY``
(``off`` / ``embeddings`` / ``deterministic`` / ``all``), with ``deterministic``
as the default.
"""

import pytest

from llmproxy.cache import (
    DEFAULT_POLICY,
    POLICIES,
    ResponseCache,
    is_deterministic,
    normalize_policy,
)

from .conftest import make_settings


def make_cache(**kwargs):
    kwargs.setdefault("enabled", True)
    return ResponseCache(**kwargs)


# --- policy parsing --------------------------------------------------------

@pytest.mark.parametrize("value", POLICIES)
def test_every_documented_policy_is_accepted(value):
    assert normalize_policy(value) == value
    assert normalize_policy(f"  {value.upper()} ") == value


@pytest.mark.parametrize("value", [None, "", "aggressive", "yes", "determinstic"])
def test_unknown_policy_degrades_to_the_default(value):
    """A typo must not fail start-up, and must not silently loosen the policy."""
    assert normalize_policy(value) == DEFAULT_POLICY == "deterministic"


def test_settings_default_is_deterministic():
    assert make_settings().cache_policy == "deterministic"


# --- determinism predicate -------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"seed": 7},                                  # a seed pins the sampler
    {"seed": 0},                                  # including seed 0
    {"seed": 7, "temperature": 0.9},              # ...whatever the temperature
    {"temperature": 0},
    {"temperature": 0.0, "top_p": 1},
])
def test_deterministic_payloads(payload):
    assert is_deterministic(payload) is True


@pytest.mark.parametrize("payload", [
    {},                                           # OpenAI defaults: temperature=1
    {"top_p": 1},                                 # temperature still defaults to 1
    {"temperature": 0.7},
    {"temperature": 0, "top_p": 0.9},             # greedy but nucleus-restricted
    {"seed": None, "temperature": 0.7},
])
def test_non_deterministic_payloads(payload):
    assert is_deterministic(payload) is False


# --- eligibility per policy ------------------------------------------------

SAMPLED = {"model": "m", "temperature": 0.7}
GREEDY = {"model": "m", "temperature": 0}
SEEDED = {"model": "m", "temperature": 0.7, "seed": 1}


@pytest.mark.parametrize("policy,chat_sampled,chat_greedy,embeddings", [
    ("off", False, False, False),
    ("embeddings", False, False, True),
    ("deterministic", False, True, True),
    ("all", True, True, True),
])
def test_allows_per_policy(policy, chat_sampled, chat_greedy, embeddings):
    cache = make_cache(policy=policy)
    assert cache.allows("chat", SAMPLED) is chat_sampled
    assert cache.allows("chat", GREEDY) is chat_greedy
    assert cache.allows("embeddings", {"model": "e", "input": "x"}) is embeddings


def test_seeded_sampling_is_eligible_under_the_default_policy():
    assert make_cache().allows("chat", SEEDED) is True


def test_a_disabled_cache_allows_nothing_and_counts_no_skips():
    cache = ResponseCache(enabled=False, policy="all")
    assert cache.allows("chat", GREEDY) is False
    assert cache.snapshot()["skipped"] == 0


def test_policy_skips_are_counted_and_reported():
    cache = make_cache()
    for _ in range(3):
        cache.allows("chat", SAMPLED)
    cache.allows("chat", GREEDY)

    snap = cache.snapshot()
    assert snap["skipped"] == 3
    assert snap["policy"] == "deterministic"
    # A skip is not a lookup: it must not distort the hit rate.
    assert (snap["hits"], snap["misses"]) == (0, 0)


def test_off_policy_keeps_the_cache_wired_but_bypassed():
    cache = make_cache(policy="off")
    assert cache.enabled is True
    assert cache.allows("chat", GREEDY) is False
    assert cache.snapshot()["policy"] == "off"


# --- end-to-end through the services ---------------------------------------

class _FakeProvider:
    """Minimal provider stand-in returning a distinct body on every call."""

    name = "test"

    def __init__(self):
        self.calls = 0

    def post(self, payload, stream=False, rid=None, path=None):
        self.calls += 1
        return _FakeResponse({"id": f"resp-{self.calls}", "model": payload["model"]})

    def log_cache_hit(self, rid, key):
        pass


class _FakeResponse:
    def __init__(self, data):
        self._llmproxy_json = data
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._llmproxy_json


class _FakeRegistry:
    default_model = "m"

    def __init__(self, provider):
        self._provider = provider

    def provider_for(self, name):
        return self._provider, "native-m"

    def embeddings_provider_for(self, name):
        return self._provider, "native-e"

    def resolve_embeddings(self, requested):
        return requested or "e"


def _chat_twice(policy, options):
    from llmproxy.services.completions import CompletionService

    provider = _FakeProvider()
    service = CompletionService(_FakeRegistry(provider), cache=make_cache(policy=policy))
    for _ in range(2):
        service.chat([{"role": "user", "content": "hi"}], stream=False, rid=None, options=options)
    return provider.calls


def test_sampled_completion_hits_the_upstream_every_time():
    """The F10 regression: two identical sampled requests must not share a reply."""
    assert _chat_twice("deterministic", {"temperature": 0.7}) == 2


def test_greedy_completion_is_served_from_the_cache():
    assert _chat_twice("deterministic", {"temperature": 0}) == 1


def test_all_policy_restores_the_pre_fix_aggressive_behaviour():
    assert _chat_twice("all", {"temperature": 0.7}) == 1


def test_embeddings_stay_cached_under_the_default_policy():
    from llmproxy.services.embeddings import EmbeddingService

    provider = _FakeProvider()
    service = EmbeddingService(_FakeRegistry(provider), "query", cache=make_cache())
    for _ in range(2):
        service.embed({"model": "e", "input": "x"}, rid=None)
    assert provider.calls == 1


def test_off_policy_bypasses_embeddings_too():
    from llmproxy.services.embeddings import EmbeddingService

    provider = _FakeProvider()
    service = EmbeddingService(_FakeRegistry(provider), "query", cache=make_cache(policy="off"))
    for _ in range(2):
        service.embed({"model": "e", "input": "x"}, rid=None)
    assert provider.calls == 2
