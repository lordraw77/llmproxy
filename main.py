#!/usr/bin/env python3
"""Process entrypoint for llmproxy.

Ollama-compatible server that forwards requests to NVIDIA (OpenAI-compatible API).

This proxy exposes both the Ollama-native endpoints (``/api/*``) and the
OpenAI-compatible endpoints (``/v1/*``), plus the llama.cpp-native
``/completion`` endpoint, and translates each incoming request into a call to
NVIDIA's OpenAI-compatible upstream. It supports streaming (SSE / NDJSON),
transient-error retries with backoff, optional inbound API-key authentication,
and per-request correlation logging.

The implementation lives in the ``llmproxy`` package (see the layered structure
under ``llmproxy/``). This module only builds the WSGI application and provides
the development server. ``app`` is exported for ``gunicorn main:app``.
"""

from llmproxy import __version__
from llmproxy.banner import render_banner
from llmproxy.config import load_settings
from llmproxy.logging_setup import configure_logging
from llmproxy.web import create_app

_settings = load_settings()
app = create_app(_settings)


def main():
    """Run the built-in development server (production uses gunicorn ``main:app``)."""
    print(render_banner(__version__), flush=True)
    logger = configure_logging(_settings)
    if not _settings.nvidia_api_key:
        logger.warning("NVIDIA_API_KEY non impostata in .env: le chiamate falliranno.")
    logger.info("llmproxy in ascolto su http://%s:%s", _settings.host, _settings.port)
    logger.info("Modelli esposti: %s", ", ".join(_settings.models))
    logger.info(
        "Default: %s | log level=%s | timezone log=%s",
        _settings.default_model, _settings.log_level, _settings.log_tz,
    )
    logger.info("Autenticazione in ingresso: %s", "ATTIVA" if _settings.proxy_api_key else "disattivata")
    app.run(host=_settings.host, port=_settings.port, threaded=True)


if __name__ == "__main__":
    main()
