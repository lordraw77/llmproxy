"""Translation of client sampling options into upstream-accepted parameters.

Pure functions: no I/O, no framework dependencies. Covers both the Ollama and the
OpenAI/llama.cpp option dialects.
"""

# Sampling parameters accepted by NVIDIA's OpenAI API (same name inbound/outbound).
_SAMPLING_PASSTHROUGH = (
    "temperature", "top_p", "max_tokens", "stop",
    "presence_penalty", "frequency_penalty", "seed", "n",
)
# Typical Ollama aliases -> OpenAI name.
_SAMPLING_ALIASES = {
    "num_predict": "max_tokens",
}


def build_sampling_params(options):
    """Translate options (Ollama-style or OpenAI-style) into upstream-accepted parameters.

    Covers both worlds:
      - Ollama:            options={"num_predict": ..., "temperature": ..., "stop": [...]}
      - OpenAI/llama.cpp:  keys already in OpenAI naming (temperature, top_p, max_tokens, stop, ...)
    Unknown keys are ignored so the upstream does not fail with a 400.

    Args:
        options: Mapping of client-provided sampling options (may be None).

    Returns:
        A dict of sampling parameters accepted by the upstream.
    """
    options = options or {}
    params = {}
    for key in _SAMPLING_PASSTHROUGH:
        if options.get(key) is not None:
            params[key] = options[key]
    for src, dst in _SAMPLING_ALIASES.items():
        if options.get(src) is not None and dst not in params:
            params[dst] = options[src]
    return params
