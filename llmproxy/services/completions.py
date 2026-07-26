"""Application service for chat/text completions.

Builds upstream payloads from the various inbound dialects and hands them to the
:class:`~llmproxy.services.routing.CachedRouter`, which resolves the provider,
applies the cache policy, and rewrites the model to its native id. Framing of the
response back into a specific client dialect is the responsibility of the web
layer; this service speaks only the canonical OpenAI format.
"""

from ..domain.sampling import build_sampling_params
from .routing import CachedRouter


class CompletionService:
    """Turns messages/options (or a raw OpenAI payload) into routed chat-completion calls."""

    def __init__(self, registry, cache=None):
        self._registry = registry
        self._router = CachedRouter(cache)

    def _send(self, payload, stream, rid):
        """Resolve the provider for ``payload``'s model and route the call."""
        provider, native_id = self._registry.provider_for(payload["model"])
        return self._router.send(provider, native_id, payload, "chat", stream, rid)

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
        return self._send(payload, stream, rid)

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
        return self._send(payload, stream, rid)
