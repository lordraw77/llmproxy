"""Startup banner and start-up summary.

Renders an ASCII-art "LLMPROXY" banner (block-letter font) followed by the
version line and the project URL, in the spirit of other self-hosted LLM tools.
The banner is emitted once, at process startup, by the development entrypoint,
together with the start-up summary of :func:`log_startup`.
"""

PROJECT_URL = "https://github.com/lordraw77/llmproxy"

# Each glyph is five rows tall; rows are joined column-wise to build a line.
_GLYPHS = {
    "L": (" _     ", "| |    ", "| |    ", "| |___ ", "|_____|"),
    "M": (" __  __ ", "|  \\/  |", "| |\\/| |", "| |  | |", "|_|  |_|"),
    "P": (" ____  ", "|  _ \\ ", "| |_) |", "|  __/ ", "|_|    "),
    "R": (" ____  ", "|  _ \\ ", "| |_) |", "|  _ < ", "|_| \\_\\"),
    "O": ("  ___  ", " / _ \\ ", "| | | |", "| |_| |", " \\___/ "),
    "X": ("__  __", "\\ \\/ /", " \\  / ", " /  \\ ", "/_/\\_\\"),
    "Y": ("__   __", "\\ \\ / /", " \\ V / ", "  | |  ", "  |_|  "),
}

_WORD = "LLMPROXY"


def _render_art():
    """Return the multi-line ASCII-art title as a single string."""
    rows = ["".join(_GLYPHS[ch][r] for ch in _WORD) for r in range(5)]
    return "\n".join(rows)


def render_banner(version="dev"):
    """Return the full startup banner (art + version line + URL) as a string.

    Args:
        version: Version string to display (e.g. ``"1.1.0"``).

    Returns:
        The banner text, without a trailing newline.
    """
    return (
        "\n"
        f"{_render_art()}\n\n"
        f"v{version} - building a fast, multi-endpoint LLM proxy.\n\n"
        f"{PROJECT_URL}\n"
    )


def log_startup(logger, settings, registry):
    """Log the start-up summary: bind address, providers, exposed models, auth.

    The catalogue is read from ``registry``, never from ``settings``: the
    ``NVIDIA_*`` env vars are only a fallback source of providers, so with a
    ``providers.toml`` in place ``settings.models`` describes models that are not
    exposed at all (see ``F8``).

    Args:
        logger: The configured application logger.
        settings: The :class:`~llmproxy.config.Settings` in use.
        registry: The :class:`~llmproxy.providers.registry.ProviderRegistry`
            actually serving requests.
    """
    logger.info("llmproxy in ascolto su http://%s:%s", settings.host, settings.port)
    logger.info("Provider configurati: %s", ", ".join(p.name for p in registry.providers) or "nessuno")
    logger.info("Modelli esposti: %s", ", ".join(registry.models) or "nessuno")
    logger.info("Modello embeddings: %s", registry.embeddings_model or "nessuno")
    logger.info(
        "Default: %s | log level=%s | timezone log=%s",
        registry.default_model or "nessuno", settings.log_level, settings.log_tz,
    )
    logger.info("Cache risposte: %s", _cache_line(settings))
    logger.info("Autenticazione in ingresso: %s", "ATTIVA" if settings.proxy_api_key else "disattivata")


def _cache_line(settings):
    """Describe the response-cache configuration in one line."""
    if not settings.cache_enabled:
        return "disattivata"
    return f"attiva (ttl={int(settings.cache_ttl)}s, max={settings.cache_max_size})"
