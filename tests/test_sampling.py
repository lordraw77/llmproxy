"""Unit tests for :func:`llmproxy.domain.sampling.build_sampling_params`.

The function sits on every completion path and has one job with three rules:
pass through the OpenAI-named options, translate the Ollama aliases, and drop
everything else so the upstream never rejects the request with a 400.
"""

import pytest

from llmproxy.domain.sampling import build_sampling_params


# --- passthrough -----------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("temperature", 0.7),
    ("top_p", 0.9),
    ("max_tokens", 128),
    ("stop", ["\n\n"]),
    ("presence_penalty", 0.5),
    ("frequency_penalty", -0.5),
    ("seed", 42),
    ("n", 2),
])
def test_known_openai_option_passes_through(key, value):
    assert build_sampling_params({key: value}) == {key: value}


def test_several_options_pass_through_together():
    options = {"temperature": 0.2, "top_p": 0.95, "max_tokens": 64}
    assert build_sampling_params(options) == options


# --- rejected input --------------------------------------------------------

@pytest.mark.parametrize("options", [None, {}])
def test_absent_options_produce_no_parameters(options):
    assert build_sampling_params(options) == {}


def test_unknown_keys_are_dropped():
    """Ollama clients send options the OpenAI API would reject outright."""
    params = build_sampling_params({
        "temperature": 0.1,
        "num_ctx": 4096,
        "repeat_penalty": 1.1,
        "mirostat": 2,
        "keep_alive": "5m",
    })
    assert params == {"temperature": 0.1}


def test_none_valued_options_are_dropped():
    """A client sending an explicit null must not forward ``None`` upstream."""
    assert build_sampling_params({"temperature": None, "max_tokens": None}) == {}


@pytest.mark.parametrize("value", [0, 0.0, False, "", []])
def test_falsy_but_present_values_are_preserved(value):
    """``temperature: 0`` is meaningful (determinism) — the check is ``is not None``."""
    assert build_sampling_params({"temperature": value}) == {"temperature": value}


# --- aliases ---------------------------------------------------------------

def test_num_predict_is_translated_to_max_tokens():
    assert build_sampling_params({"num_predict": 256}) == {"max_tokens": 256}


def test_explicit_max_tokens_wins_over_the_alias():
    params = build_sampling_params({"max_tokens": 100, "num_predict": 256})
    assert params == {"max_tokens": 100}


def test_alias_is_ignored_when_null():
    assert build_sampling_params({"num_predict": None}) == {}


def test_alias_and_passthrough_coexist():
    params = build_sampling_params({"num_predict": 32, "temperature": 0.3, "stop": ["END"]})
    assert params == {"max_tokens": 32, "temperature": 0.3, "stop": ["END"]}


# --- purity ----------------------------------------------------------------

def test_the_caller_options_are_not_mutated():
    options = {"num_predict": 16, "num_ctx": 2048}
    build_sampling_params(options)
    assert options == {"num_predict": 16, "num_ctx": 2048}
