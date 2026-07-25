"""Generic OpenAI-compatible provider.

Covers every upstream that speaks the OpenAI HTTP API: NVIDIA, OpenAI itself,
Mistral, vLLM, Groq, OpenRouter, LM Studio, and a local Ollama/llama.cpp server
exposing ``/v1``. No translation is needed — the :class:`~llmproxy.providers.base.Provider`
defaults already speak this dialect — so this class exists only to give the
``openai_compatible`` config type a concrete home.
"""

from .base import Provider


class OpenAICompatibleProvider(Provider):
    """An upstream reachable through the standard OpenAI API (no shape translation)."""
