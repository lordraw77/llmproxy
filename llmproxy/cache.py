"""In-process response cache for non-streaming upstream replies.

A single thread-safe :class:`ResponseCache` per worker memoizes the parsed JSON
body of non-streaming upstream calls (chat/text completions and embeddings),
keyed by a hash of the outbound payload. On a hit the proxy returns the stored
body without touching the network, saving an upstream round-trip (and its token
cost / latency).

The cache is:

- **Optional** — disabled unless ``CACHE_ENABLED`` is truthy.
- **TTL-bounded** — every entry expires ``CACHE_TTL`` seconds after it is stored.
- **Size-bounded** — at most ``CACHE_MAX_SIZE`` entries; the least-recently-used
  entry is evicted when the cap is exceeded (classic LRU via ``OrderedDict``).
- **Policy-bounded** — ``CACHE_POLICY`` decides *which* requests are eligible at
  all (see :data:`POLICIES`). Replaying a completion produced with
  ``temperature > 0`` hands the client the same "sampled" answer for the whole
  TTL, which is a legitimate cost trade-off but must be opted into rather than
  inherited from ``CACHE_ENABLED`` alone.

Eligibility levels (``CACHE_POLICY``), from strictest to loosest:

===================  ===========================================================
``off``              Nothing is cached, even with ``CACHE_ENABLED=true``. Useful
                     to keep the cache wired (and its counters visible) while
                     bypassing it.
``embeddings``       Only embeddings, whose determinism is a property of the
                     model rather than of the request.
``deterministic``    *(default)* Embeddings, plus completions that can only have
                     one answer: an explicit ``seed``, or ``temperature == 0``
                     with ``top_p == 1``.
``all``              Every successful non-streaming reply, sampling parameters
                     ignored. Maximum token savings, repeated answers within the
                     TTL.
===================  ===========================================================

Ineligible requests are counted as ``skipped`` in :meth:`snapshot`, so a policy
that never lets anything through is visible at ``/stats`` instead of looking
like a cache that simply never hits.

Like :mod:`llmproxy.metrics`, it lives entirely in memory with no persistence and
is **per-worker**: under gunicorn each worker keeps its own cache, so the observed
hit rate is a per-process figure. Streaming responses are never cached (they are
consumed incrementally and are not replayable), and only successful (2xx) replies
enter the cache.
"""

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict


#: Eligibility levels accepted by ``CACHE_POLICY``, strictest first.
POLICIES = ("off", "embeddings", "deterministic", "all")

#: Level applied when ``CACHE_POLICY`` is unset or unrecognized.
DEFAULT_POLICY = "deterministic"


def normalize_policy(value):
    """Return a valid policy name, falling back to :data:`DEFAULT_POLICY`.

    Mirrors the rest of :mod:`llmproxy.config`: an unrecognized value degrades to
    the safe default instead of failing start-up.
    """
    name = (value or "").strip().lower()
    return name if name in POLICIES else DEFAULT_POLICY


def is_deterministic(payload):
    """Whether ``payload`` can only produce one answer, so replaying it is honest.

    An explicit ``seed`` pins the sampler regardless of temperature. Otherwise the
    request must disable sampling outright: greedy decoding (``temperature == 0``)
    with an unrestricted nucleus (``top_p == 1``). Absent fields take the OpenAI
    defaults (``temperature=1``, ``top_p=1``), i.e. *not* deterministic — the
    common case of a client sending neither.
    """
    if payload.get("seed") is not None:
        return True
    temperature = payload.get("temperature")
    top_p = payload.get("top_p")
    if temperature is None:
        return False
    return float(temperature) == 0.0 and float(1 if top_p is None else top_p) == 1.0


class CachedResponse:
    """A stored upstream body replayed as a minimal ``requests.Response`` stand-in.

    Exposes the subset of the response interface the web layer consumes
    (``ok``, ``status_code``, ``raise_for_status``, ``json`` and the memoized
    ``_llmproxy_json`` used by :func:`llmproxy.upstream.client.resp_json`), plus a
    ``from_cache`` marker. Mirrors ``AggregatedResponse`` so route handlers treat a
    cache hit exactly like any other non-streaming upstream response.
    """

    def __init__(self, data):
        self._llmproxy_json = data
        self.status_code = 200
        self.ok = True
        self.text = ""
        self.from_cache = True

    def raise_for_status(self):
        return None

    def json(self):
        return self._llmproxy_json

    def close(self):
        """No-op: there is no connection behind a replayed response."""


class ResponseCache:
    """Thread-safe TTL + LRU cache of non-streaming upstream response bodies."""

    def __init__(self, enabled=False, ttl=300.0, max_size=512, policy=DEFAULT_POLICY):
        """Build a cache.

        Args:
            enabled: When false, :meth:`get`/:meth:`set` are no-ops and every
                lookup misses — the feature is off with negligible overhead.
            ttl: Time-to-live of an entry in seconds. Non-positive disables the
                cache (an entry would expire immediately).
            max_size: Maximum number of entries; the least-recently-used entry is
                evicted past this cap. Non-positive disables the cache.
            policy: Eligibility level (see :data:`POLICIES`). Unrecognized values
                degrade to :data:`DEFAULT_POLICY`.
        """
        self.policy = normalize_policy(policy)
        self._ttl = float(ttl)
        self._max_size = int(max_size)
        # A misconfiguration (ttl<=0 or max_size<=0) degrades gracefully to "off".
        self._enabled = bool(enabled) and self._ttl > 0 and self._max_size > 0

        self._lock = threading.Lock()
        self._store = OrderedDict()  # key -> (expires_at, value)

        # Counters (reported by :meth:`snapshot`).
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.evictions = 0    # dropped because the cache was full (LRU).
        self.expirations = 0  # dropped because the TTL elapsed.
        self.skipped = 0      # never looked up: ineligible under the policy.

    @property
    def enabled(self):
        """Whether the cache is active (and configured with sane ttl/size)."""
        return self._enabled

    def allows(self, namespace, payload):
        """Whether ``payload`` is eligible for caching under the current policy.

        Args:
            namespace: The key space of the call, as passed to :meth:`make_key`
                (``"embeddings"`` or a completion namespace such as ``"chat"``).
            payload: The outbound payload, inspected for sampling parameters.

        Returns:
            True when the caller should consult and populate the cache. A False
            for a policy reason (rather than because the cache is off) is counted
            under ``skipped``.
        """
        if not self._enabled or self.policy == "off":
            return False
        if namespace == "embeddings" or self.policy == "all":
            return True
        if self.policy == "deterministic" and is_deterministic(payload):
            return True
        with self._lock:
            self.skipped += 1
        return False

    @staticmethod
    def make_key(namespace, payload):
        """Derive a stable cache key from a namespace and an outbound payload.

        The payload is canonicalized (sorted keys) so semantically identical
        requests map to the same key regardless of field order. ``namespace``
        separates the key spaces of different upstream paths (e.g. chat vs.
        embeddings) sharing one cache instance.
        """
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def get(self, key):
        """Return a deep copy of the cached body for ``key``, or ``None`` on a miss.

        Expired entries are dropped and counted as a miss. A copy is returned so a
        caller mutating the response (route handlers overwrite ``model``) cannot
        corrupt the stored entry.
        """
        if not self._enabled:
            return None
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at <= now:
                # Lazily evict the expired entry.
                del self._store[key]
                self.expirations += 1
                self.misses += 1
                return None
            # Refresh recency for LRU ordering.
            self._store.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(value)

    def set(self, key, value):
        """Store ``value`` under ``key`` with a fresh TTL, evicting LRU if full.

        A deep copy is stored so later mutation of the caller's object does not
        leak into the cache.
        """
        if not self._enabled:
            return
        expires_at = time.time() + self._ttl
        snapshot = copy.deepcopy(value)
        with self._lock:
            if key in self._store:
                # Overwrite in place and mark as most-recently-used.
                self._store[key] = (expires_at, snapshot)
                self._store.move_to_end(key)
            else:
                self._store[key] = (expires_at, snapshot)
            self.stores += 1
            # Enforce the size cap (LRU eviction from the front).
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
                self.evictions += 1

    def clear(self):
        """Drop every entry (counters are preserved)."""
        with self._lock:
            self._store.clear()

    def snapshot(self):
        """Return a JSON-serializable view of the cache configuration and counters."""
        with self._lock:
            # Count only still-valid entries so the reported size is not inflated
            # by lazily-expired ones.
            now = time.time()
            live = sum(1 for expires_at, _ in self._store.values() if expires_at > now)
            lookups = self.hits + self.misses
            hit_rate = (self.hits / lookups) if lookups else 0.0
            return {
                "enabled": self._enabled,
                "policy": self.policy,
                "ttl_seconds": round(self._ttl, 1),
                "max_size": self._max_size,
                "entries": live,
                "hits": self.hits,
                "misses": self.misses,
                "stores": self.stores,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "skipped": self.skipped,
                "hit_rate": round(hit_rate, 4),
            }
