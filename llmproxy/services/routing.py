"""The one place a payload becomes an upstream call.

Both application services do the same four things around their own payload:
resolve the provider that owns the exposed model, rewrite the model to that
provider's **native** id, consult the response cache, and populate it from a
successful reply. Only the payload construction and the cache namespace differ,
which is what each service keeps.

Keeping the sequence here also keeps the cacheability policy (``CACHE_POLICY``,
see :mod:`llmproxy.cache`) applied at exactly one call site.
"""

from .. import audit


class CachedRouter:
    """Sends an already-built payload to its provider, through the cache."""

    def __init__(self, cache=None):
        """Build a router.

        Args:
            cache: The :class:`~llmproxy.cache.ResponseCache`, or ``None`` when
                caching is not wired at all.
        """
        self._cache = cache

    def send(self, provider, native_id, payload, namespace, stream, rid, path=None):
        """Route ``payload`` to ``provider``, consulting/populating the cache.

        The payload arrives carrying the **exposed** model name (e.g.
        ``nvidia:llama-3.3-70b``): the cache is keyed on it, so the same native
        id served by two providers yields distinct entries, and the model is
        rewritten to ``native_id`` only for the request that leaves the proxy.

        Args:
            provider: The :class:`~llmproxy.providers.base.Provider` to call.
            native_id: The provider-native model id.
            payload: The outbound payload, keyed on the exposed model name.
            namespace: Cache key space for this call (``"chat"``/``"embeddings"``).
            stream: Whether a streaming response was requested. A stream is
                consumed incrementally and cannot be replayed, so it always
                bypasses the cache.
            rid: Correlation id for logging.
            path: Upstream path override, passed through to the provider.

        Returns:
            The upstream response, or a :class:`~llmproxy.cache.CachedResponse`
            stand-in on a hit.
        """
        cache = self._cache
        if stream or cache is None or not cache.allows(namespace, payload):
            return self._post(provider, native_id, payload, stream, rid, path)

        # Imported here to avoid a hard dependency when caching is disabled.
        from ..cache import CachedResponse
        from ..providers.base import resp_json

        key = cache.make_key(namespace, payload)
        hit = cache.get(key)
        if hit is not None:
            provider.log_cache_hit(rid, key)
            # A hit never reaches the provider layer, so the audit record would
            # otherwise show a request with no reply and no tokens: the served
            # body is the reply, and its usage is what the call would have cost.
            event = audit.current()
            event.cached(provider.name)
            event.record_body(hit)
            return CachedResponse(hit)

        resp = self._post(provider, native_id, payload, stream, rid, path)
        if getattr(resp, "ok", False):
            try:
                cache.set(key, resp_json(resp))
            except ValueError:
                pass  # Non-JSON body: nothing worth caching.
        return resp

    @staticmethod
    def _post(provider, native_id, payload, stream, rid, path):
        """Call the provider with the model rewritten to its native id."""
        # The only point where the exposed name and the native id coexist.
        audit.current().routed(payload.get("model"))
        outbound = dict(payload)
        outbound["model"] = native_id
        if path is None:
            return provider.post(outbound, stream, rid)
        return provider.post(outbound, stream, rid, path=path)
