"""Unit tests for :class:`llmproxy.services.routing.CachedRouter` (R1).

The router is the sequence both services used to carry their own copy of:
rewrite the model to the provider-native id, consult the cache, call upstream,
store a successful reply. These pin the parts that were easy to get subtly
different between the two copies — which model name is cached vs. sent, when the
cache is bypassed, and what happens to a non-JSON body.
"""

import pytest

from llmproxy.cache import ResponseCache
from llmproxy.services.routing import CachedRouter

DETERMINISTIC = {"model": "exposed:m", "temperature": 0}

_DEFAULT = object()


class FakeProvider:
    """Records what it was called with; returns a canned JSON body."""

    name = "fake"

    def __init__(self, body=_DEFAULT, ok=True):
        self.calls = []
        self.hits = []
        # ``None`` is a meaningful body here (a reply that is not JSON), so the
        # default has to be a distinct sentinel.
        self._body = {"id": "resp"} if body is _DEFAULT else body
        self._ok = ok

    def post(self, payload, stream, rid, path=None):
        self.calls.append({"payload": payload, "stream": stream, "rid": rid, "path": path})
        return FakeResponse(self._body, ok=self._ok)

    def log_cache_hit(self, rid, key):
        self.hits.append(key)


class FakeResponse:
    def __init__(self, data, ok=True):
        self._llmproxy_json = data
        self.ok = ok
        self.status_code = 200 if ok else 500

    def json(self):
        if self._llmproxy_json is None:
            raise ValueError("not JSON")
        return self._llmproxy_json


def make_cache(**kwargs):
    kwargs.setdefault("enabled", True)
    return ResponseCache(**kwargs)


# --- model rewriting -------------------------------------------------------

def test_the_native_id_goes_upstream_and_the_exposed_name_stays_in_the_payload():
    provider = FakeProvider()
    payload = dict(DETERMINISTIC)

    CachedRouter().send(provider, "native/m", payload, "chat", stream=False, rid="r")

    assert provider.calls[0]["payload"]["model"] == "native/m"
    assert payload["model"] == "exposed:m", "the caller's payload must not be mutated"


def test_the_cache_is_keyed_on_the_exposed_name_not_the_native_id():
    """Two providers serving the same native id must not share an entry."""
    cache = make_cache()
    router = CachedRouter(cache)
    a, b = FakeProvider({"id": "from-a"}), FakeProvider({"id": "from-b"})

    router.send(a, "native/m", {"model": "one:m", "temperature": 0}, "chat", False, "r")
    resp = router.send(b, "native/m", {"model": "two:m", "temperature": 0}, "chat", False, "r")

    assert resp.json() == {"id": "from-b"}
    assert len(b.calls) == 1, "the second provider was called, not served from the first's entry"


def test_the_path_override_is_forwarded_only_when_given():
    provider = FakeProvider()
    router = CachedRouter()

    router.send(provider, "n", dict(DETERMINISTIC), "chat", False, "r")
    router.send(provider, "n", dict(DETERMINISTIC), "embeddings", False, "r", path="/embeddings")

    assert provider.calls[0]["path"] is None
    assert provider.calls[1]["path"] == "/embeddings"


# --- cache interaction -----------------------------------------------------

def test_a_second_identical_call_is_served_from_the_cache():
    provider = FakeProvider()
    router = CachedRouter(make_cache())

    for _ in range(2):
        resp = router.send(provider, "n", dict(DETERMINISTIC), "chat", False, "r")

    assert len(provider.calls) == 1
    assert resp.from_cache is True
    assert provider.hits, "a hit must be logged for the correlation id"


def test_streaming_always_bypasses_the_cache():
    """A stream is consumed incrementally and cannot be replayed."""
    provider = FakeProvider()
    router = CachedRouter(make_cache())

    for _ in range(2):
        router.send(provider, "n", dict(DETERMINISTIC), "chat", stream=True, rid="r")

    assert len(provider.calls) == 2


def test_no_cache_wired_at_all_still_routes():
    provider = FakeProvider()

    resp = CachedRouter(None).send(provider, "n", dict(DETERMINISTIC), "chat", False, "r")

    assert resp.json() == {"id": "resp"}
    assert len(provider.calls) == 1


def test_the_policy_decides_eligibility():
    """A sampled completion is ineligible under the default policy."""
    provider = FakeProvider()
    router = CachedRouter(make_cache())
    sampled = {"model": "exposed:m", "temperature": 0.7}

    for _ in range(2):
        router.send(provider, "n", dict(sampled), "chat", False, "r")

    assert len(provider.calls) == 2


@pytest.mark.parametrize("ok,body,expected_calls", [
    (False, {"error": "boom"}, 2),  # non-2xx: never stored
    (True, None, 2),                # 2xx but not JSON: nothing worth caching
])
def test_only_successful_json_replies_enter_the_cache(ok, body, expected_calls):
    provider = FakeProvider(body=body, ok=ok)
    router = CachedRouter(make_cache())

    for _ in range(2):
        router.send(provider, "n", dict(DETERMINISTIC), "chat", False, "r")

    assert len(provider.calls) == expected_calls


def test_the_namespace_separates_the_two_key_spaces():
    """An identical payload under two namespaces must not collide."""
    cache = make_cache()
    router = CachedRouter(cache)
    provider = FakeProvider()
    payload = {"model": "exposed:m", "input": "x"}

    router.send(provider, "n", dict(payload), "embeddings", False, "r")
    router.send(provider, "n", dict(payload), "chat", False, "r")

    assert len(provider.calls) == 2
