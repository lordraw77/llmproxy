"""llmproxy: an Ollama/OpenAI/llama.cpp-compatible proxy in front of many upstreams.

One instance exposes the models of every configured provider — OpenAI-compatible
endpoints (NVIDIA, Groq, Cerebras, Mistral, OpenRouter, Cloudflare, a local
Ollama, …) and the native Anthropic, Gemini and Azure APIs — through all three
client dialects at once.

The public entrypoint is the application factory :func:`llmproxy.web.create_app`.
"""

from .web import create_app

__version__ = "1.4.0"

__all__ = ["create_app", "__version__"]
