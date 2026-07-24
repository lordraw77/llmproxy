"""Application service for chat/text completions.

Builds upstream payloads from the various inbound dialects and forwards them
through the :class:`~llmproxy.upstream.client.NvidiaUpstream`. Framing of the
response back into a specific client dialect is the responsibility of the web
layer; this service speaks only the upstream (OpenAI) format.
"""

from ..domain.sampling import build_sampling_params


class CompletionService:
    """Turns messages/options (or a raw OpenAI payload) into upstream chat-completion calls."""

    def __init__(self, upstream, default_model, cache=None):
        self._upstream = upstream
        self._default_model = default_model
        self._cache = cache

    def _post_cached(self, payload, stream, rid):
        """Forward ``payload`` upstream, consulting/populating the response cache.

        Only **non-streaming** calls are cacheable: a streaming response is
        consumed incrementally and cannot be replayed. On a hit the stored body is
        returned as a :class:`~llmproxy.cache.CachedResponse` without any network
        call; on a miss the successful reply is stored before being returned.
        """
        cache = self._cache
        if stream or cache is None or not cache.enabled:
            return self._upstream.post(payload, stream, rid)

        # Import here to avoid a hard dependency when caching is disabled.
        from ..cache import CachedResponse
        from ..upstream.client import resp_json

        key = cache.make_key("chat", payload)
        hit = cache.get(key)
        if hit is not None:
            self._upstream.log_cache_hit(rid, key)
            return CachedResponse(hit)

        resp = self._upstream.post(payload, stream, rid)
        if getattr(resp, "ok", False):
            try:
                cache.set(key, resp_json(resp))
            except ValueError:
                pass  # Non-JSON body: nothing worth caching.
        return resp

    def chat(self, messages, stream, rid, options=None, model=None):
        """Build a chat-completions payload from messages/options and forward it upstream.

        Args:
            messages: List of chat messages (OpenAI ``{"role", "content"}`` format).
            stream: Whether to request a streaming response.
            rid: Correlation id for logging.
            options: Optional sampling options (Ollama- or OpenAI-style).
            model: Optional model name; falls back to the configured default.

        Returns:
            The upstream ``requests.Response`` (or a cached stand-in).
        """
        payload = {
            "model": model or self._default_model,
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
            model: Optional model name; falls back to the configured default.

        Returns:
            The upstream ``requests.Response`` (or a cached stand-in).
        """
        payload = dict(payload)
        payload["model"] = model or self._default_model
        payload["stream"] = stream
        return self._post_cached(payload, stream, rid)
