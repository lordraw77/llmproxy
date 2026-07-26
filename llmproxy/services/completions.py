"""Application service for chat/text completions.

Builds upstream payloads from the various inbound dialects and forwards them to
the provider that owns the requested model (resolved through the
:class:`~llmproxy.providers.registry.ProviderRegistry`). Framing of the response
back into a specific client dialect is the responsibility of the web layer; this
service speaks only the canonical OpenAI format.
"""

from ..domain.sampling import build_sampling_params


class CompletionService:
    """Turns messages/options (or a raw OpenAI payload) into routed chat-completion calls."""

    def __init__(self, registry, cache=None):
        self._registry = registry
        self._cache = cache

    def _post_cached(self, payload, stream, rid):
        """Route ``payload`` to its provider, consulting/populating the response cache.

        The payload carries the **exposed** model name (e.g. ``nvidia:llama-3.3-70b``);
        the cache is keyed on it (so the same native id served by two providers
        yields distinct entries), but the model is rewritten to the provider's
        **native** id before the request leaves the proxy. Only non-streaming calls
        are cacheable: a streaming response cannot be replayed.

        Eligibility is not automatic: :meth:`~llmproxy.cache.ResponseCache.allows`
        applies the configured ``CACHE_POLICY``, so a sampled completion
        (``temperature > 0`` without a ``seed``) goes upstream every time unless
        the deployment has explicitly opted into replaying it.
        """
        provider, native_id = self._registry.provider_for(payload["model"])
        cache = self._cache

        if stream or cache is None or not cache.allows("chat", payload):
            return provider.post(self._with_native(payload, native_id), stream, rid)

        # Import here to avoid a hard dependency when caching is disabled.
        from ..cache import CachedResponse
        from ..providers.base import resp_json

        key = cache.make_key("chat", payload)
        hit = cache.get(key)
        if hit is not None:
            provider.log_cache_hit(rid, key)
            return CachedResponse(hit)

        resp = provider.post(self._with_native(payload, native_id), stream, rid)
        if getattr(resp, "ok", False):
            try:
                cache.set(key, resp_json(resp))
            except ValueError:
                pass  # Non-JSON body: nothing worth caching.
        return resp

    @staticmethod
    def _with_native(payload, native_id):
        """Return a copy of ``payload`` with the model set to the provider-native id."""
        out = dict(payload)
        out["model"] = native_id
        return out

    def chat(self, messages, stream, rid, options=None, model=None):
        """Build a chat-completions payload from messages/options and route it upstream.

        Args:
            messages: List of chat messages (OpenAI ``{"role", "content"}`` format).
            stream: Whether to request a streaming response.
            rid: Correlation id for logging.
            options: Optional sampling options (Ollama- or OpenAI-style).
            model: Exposed model name; falls back to the registry default.

        Returns:
            The upstream response (or a cached stand-in).
        """
        payload = {
            "model": model or self._registry.default_model,
            "messages": messages,
            "stream": stream,
        }
        payload.update(build_sampling_params(options))
        if stream:
            # Ask for usage even in streaming, so we can log it and re-expose it.
            payload["stream_options"] = {"include_usage": True}
        return self._post_cached(payload, stream, rid)

    def passthrough(self, payload, stream, rid, model=None):
        """Forward an already OpenAI-formatted payload, overriding only model/stream.

        Args:
            payload: The client-provided OpenAI-format body.
            stream: Whether to request a streaming response.
            rid: Correlation id for logging.
            model: Exposed model name; falls back to the registry default.

        Returns:
            The upstream response (or a cached stand-in).
        """
        payload = dict(payload)
        payload["model"] = model or self._registry.default_model
        payload["stream"] = stream
        return self._post_cached(payload, stream, rid)
