#!/usr/bin/env python3
"""Process entrypoint for llmproxy.

Multi-dialect server in front of any number of configured upstreams.

This proxy exposes the Ollama-native endpoints (``/api/*``), the
OpenAI-compatible endpoints (``/v1/*``) and the llama.cpp-native ``/completion``
endpoint, and routes each incoming request to the provider that serves the
requested model — an OpenAI-compatible upstream, or a native Anthropic, Gemini or
Azure one, whose formats are translated in both directions. It supports streaming
(SSE / NDJSON), transient-error retries with backoff, optional inbound API-key
authentication, a policy-gated response cache, and per-request correlation
logging.

The implementation lives in the ``llmproxy`` package (see the layered structure
under ``llmproxy/``). This module only builds the WSGI application and provides
the development server. ``app`` is exported for ``gunicorn main:app``.
"""

from llmproxy import __version__
from llmproxy.banner import log_startup, render_banner
from llmproxy.config import load_settings
from llmproxy.logging_setup import configure_logging
from llmproxy.web import create_app

_settings = load_settings()
app = create_app(_settings)


def main():
    """Run the built-in development server (production uses gunicorn ``main:app``)."""
    print(render_banner(__version__), flush=True)
    logger = configure_logging(_settings)
    # Missing provider credentials are reported per-provider at start-up by
    # llmproxy.providers.factory.build_providers.
    # The catalogue comes from the registry, not from Settings: with a
    # providers.toml the NVIDIA_* env vars are not the source of truth and would
    # advertise models that are not exposed (and omit the ones that are).
    log_startup(logger, _settings, app.extensions["llmproxy"].registry)
    app.run(host=_settings.host, port=_settings.port, threaded=True)


if __name__ == "__main__":
    main()
