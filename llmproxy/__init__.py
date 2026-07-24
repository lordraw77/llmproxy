"""llmproxy: an Ollama/OpenAI/llama.cpp-compatible proxy to NVIDIA's API.

The public entrypoint is the application factory :func:`llmproxy.web.create_app`.
"""

from .web import create_app

__version__ = "1.1.0"

__all__ = ["create_app", "__version__"]
