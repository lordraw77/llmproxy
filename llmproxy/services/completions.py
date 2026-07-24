"""Application service for chat/text completions.

Builds upstream payloads from the various inbound dialects and forwards them
through the :class:`~llmproxy.upstream.client.NvidiaUpstream`. Framing of the
response back into a specific client dialect is the responsibility of the web
layer; this service speaks only the upstream (OpenAI) format.
"""

from ..domain.sampling import build_sampling_params


class CompletionService:
    """Turns messages/options (or a raw OpenAI payload) into upstream chat-completion calls."""

    def __init__(self, upstream, default_model):
        self._upstream = upstream
        self._default_model = default_model

    def chat(self, messages, stream, rid, options=None, model=None):
        """Build a chat-completions payload from messages/options and forward it upstream.

        Args:
            messages: List of chat messages (OpenAI ``{"role", "content"}`` format).
            stream: Whether to request a streaming response.
            rid: Correlation id for logging.
            options: Optional sampling options (Ollama- or OpenAI-style).
            model: Optional model name; falls back to the configured default.

        Returns:
            The upstream ``requests.Response``.
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
        return self._upstream.post(payload, stream, rid)

    def passthrough(self, payload, stream, rid, model=None):
        """Forward an already OpenAI-formatted payload, overriding only model/stream.

        Args:
            payload: The client-provided OpenAI-format body.
            stream: Whether to request a streaming response.
            rid: Correlation id for logging.
            model: Optional model name; falls back to the configured default.

        Returns:
            The upstream ``requests.Response``.
        """
        payload = dict(payload)
        payload["model"] = model or self._default_model
        payload["stream"] = stream
        return self._upstream.post(payload, stream, rid)
