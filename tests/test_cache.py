"""Unit tests for :class:`llmproxy.cache.ResponseCache`.

Covers the three properties the cache is expected to hold — TTL expiry, LRU
eviction under the size cap, and copy-on-read/copy-on-write isolation — plus the
counters reported through ``/stats``.
"""

import pytest

from llmproxy.cache import CachedResponse, ResponseCache


def make_cache(**kwargs):
    kwargs.setdefault("enabled", True)
    return ResponseCache(**kwargs)


# --- enablement ------------------------------------------------------------

def test_disabled_cache_never_stores_or_hits():
    cache = ResponseCache(enabled=False)
    cache.set("k", {"a": 1})
    assert cache.get("k") is None
    assert cache.enabled is False
    # A disabled cache must not even move its counters: it is a pure no-op.
    assert (cache.hits, cache.misses, cache.stores) == (0, 0, 0)


@pytest.mark.parametrize("kwargs", [{"ttl": 0}, {"ttl": -1}, {"max_size": 0}, {"max_size": -5}])
def test_nonsensical_configuration_degrades_to_disabled(kwargs):
    """A ttl/size that would make every entry useless turns the cache off instead."""
    cache = make_cache(**kwargs)
    assert cache.enabled is False
    cache.set("k", {"a": 1})
    assert cache.get("k") is None


# --- basic round-trip ------------------------------------------------------

def test_store_then_hit_returns_the_value():
    cache = make_cache()
    cache.set("k", {"a": 1})
    assert cache.get("k") == {"a": 1}
    assert (cache.hits, cache.misses, cache.stores) == (1, 0, 1)


def test_miss_on_unknown_key_counts_as_a_miss():
    cache = make_cache()
    assert cache.get("absent") is None
    assert (cache.hits, cache.misses) == (0, 1)


def test_overwriting_a_key_does_not_grow_the_store():
    cache = make_cache(max_size=2)
    cache.set("k", {"v": 1})
    cache.set("k", {"v": 2})
    assert cache.get("k") == {"v": 2}
    assert cache.snapshot()["entries"] == 1
    assert cache.evictions == 0


# --- TTL -------------------------------------------------------------------

def test_entry_expires_after_ttl(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("llmproxy.cache.time.time", lambda: clock["now"])
    cache = make_cache(ttl=10)
    cache.set("k", {"a": 1})

    clock["now"] = 1009.9
    assert cache.get("k") == {"a": 1}, "still inside the TTL window"

    clock["now"] = 1010.0
    assert cache.get("k") is None, "TTL is inclusive: expires_at <= now is expired"
    assert cache.expirations == 1
    assert cache.misses == 1


def test_expired_entry_is_not_counted_by_snapshot(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("llmproxy.cache.time.time", lambda: clock["now"])
    cache = make_cache(ttl=5)
    cache.set("a", {"v": 1})
    clock["now"] = 3.0
    cache.set("b", {"v": 2})

    clock["now"] = 6.0  # 'a' is stale, 'b' is not
    assert cache.snapshot()["entries"] == 1


def test_set_refreshes_the_ttl(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("llmproxy.cache.time.time", lambda: clock["now"])
    cache = make_cache(ttl=10)
    cache.set("k", {"v": 1})
    clock["now"] = 8.0
    cache.set("k", {"v": 2})
    clock["now"] = 15.0  # past the original deadline, inside the refreshed one
    assert cache.get("k") == {"v": 2}


# --- LRU eviction ----------------------------------------------------------

def test_evicts_least_recently_used_when_full():
    cache = make_cache(max_size=2)
    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    cache.set("c", {"v": 3})  # pushes out 'a'

    assert cache.get("a") is None
    assert cache.get("b") == {"v": 2}
    assert cache.get("c") == {"v": 3}
    assert cache.evictions == 1


def test_a_hit_refreshes_recency():
    cache = make_cache(max_size=2)
    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    cache.get("a")            # 'a' becomes the most recently used
    cache.set("c", {"v": 3})  # so 'b' is the one evicted

    assert cache.get("a") == {"v": 1}
    assert cache.get("b") is None


def test_overwrite_marks_the_entry_most_recently_used():
    cache = make_cache(max_size=2)
    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    cache.set("a", {"v": 10})  # refreshes recency of 'a'
    cache.set("c", {"v": 3})

    assert cache.get("a") == {"v": 10}
    assert cache.get("b") is None


# --- isolation -------------------------------------------------------------

def test_stored_value_is_isolated_from_later_caller_mutation():
    cache = make_cache()
    payload = {"choices": [{"message": {"content": "hi"}}]}
    cache.set("k", payload)
    payload["choices"][0]["message"]["content"] = "MUTATED"
    assert cache.get("k")["choices"][0]["message"]["content"] == "hi"


def test_returned_value_is_isolated_from_the_store():
    """Route handlers overwrite ``model`` on the returned body; that must not stick."""
    cache = make_cache()
    cache.set("k", {"model": "native-id", "nested": {"a": [1, 2]}})

    first = cache.get("k")
    first["model"] = "exposed-name"
    first["nested"]["a"].append(3)

    assert cache.get("k") == {"model": "native-id", "nested": {"a": [1, 2]}}


# --- keys ------------------------------------------------------------------

def test_key_is_stable_across_field_order():
    a = ResponseCache.make_key("chat", {"model": "m", "messages": [1], "temperature": 0})
    b = ResponseCache.make_key("chat", {"temperature": 0, "messages": [1], "model": "m"})
    assert a == b


def test_namespace_separates_key_spaces():
    payload = {"model": "m"}
    assert ResponseCache.make_key("chat", payload) != ResponseCache.make_key("embeddings", payload)


def test_different_payloads_produce_different_keys():
    assert ResponseCache.make_key("chat", {"t": 0}) != ResponseCache.make_key("chat", {"t": 1})


def test_key_survives_a_non_serializable_value():
    """``default=str`` keeps key derivation from raising on an exotic payload."""
    key = ResponseCache.make_key("chat", {"when": object()})
    assert key.startswith("chat:")


# --- snapshot / clear ------------------------------------------------------

def test_snapshot_reports_configuration_and_counters():
    cache = make_cache(ttl=42.4, max_size=7)
    cache.set("a", {"v": 1})
    cache.get("a")
    cache.get("missing")

    snap = cache.snapshot()
    assert snap["enabled"] is True
    assert snap["ttl_seconds"] == 42.4
    assert snap["max_size"] == 7
    assert snap["entries"] == 1
    assert (snap["hits"], snap["misses"], snap["stores"]) == (1, 1, 1)
    assert snap["hit_rate"] == 0.5


def test_hit_rate_is_zero_before_any_lookup():
    assert make_cache().snapshot()["hit_rate"] == 0.0


def test_clear_drops_entries_but_keeps_counters():
    cache = make_cache()
    cache.set("a", {"v": 1})
    cache.get("a")
    cache.clear()

    assert cache.get("a") is None
    assert cache.snapshot()["entries"] == 0
    assert cache.hits == 1
    assert cache.stores == 1


# --- CachedResponse --------------------------------------------------------

def test_cached_response_mimics_an_upstream_response():
    """Route handlers must treat a hit exactly like a real non-streaming reply."""
    from llmproxy.providers import resp_json

    resp = CachedResponse({"id": "x"})
    assert resp.ok is True
    assert resp.status_code == 200
    assert resp.from_cache is True
    assert resp.raise_for_status() is None
    assert resp.json() == {"id": "x"}
    assert resp_json(resp) == {"id": "x"}
