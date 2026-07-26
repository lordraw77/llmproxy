"""F11: which streaming endpoints report the exposed model name, and which don't.

The proxy exposes its own names (an alias, or ``provider:model`` when several
providers coexist) and rewrites ``model`` to the native id only on the way
upstream. Every re-framing route builds its chunks itself and therefore echoes
the **exposed** name; ``/v1/chat/completions`` in streaming mode relays the
upstream SSE bytes verbatim, so its chunks carry the provider's **native** id.

That asymmetry is a deliberate, documented limit of the byte relay (rewriting it
would mean parsing and re-serializing every chunk, per token) — see
``docs/api-reference.md``. These tests pin both halves so the limit cannot drift
silently into the endpoints that do rewrite it.
"""

import json

import pytest

from llmproxy.providers.openai_compatible import OpenAICompatibleProvider

from .conftest import make_provider_config

NATIVE_ID = "meta/llama-3.3-70b"
EXPOSED = "big-llama"  # an alias: distinct from the native id on purpose


class FakeStream:
    """Streaming upstream whose chunks carry the *native* model id, as a real one would."""

    def __init__(self):
        self.closed = False

    def _lines(self):
        yield "data: " + json.dumps({
            "model": NATIVE_ID,
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
        })
        yield "data: [DONE]"

    def iter_lines(self, decode_unicode=True):
        return self._lines()

    def iter_content(self, chunk_size=None):
        for line in self._lines():
            yield (line + "\n\n").encode("utf-8")

    def close(self):
        self.closed = True


@pytest.fixture
def aliased_app(app_factory, monkeypatch):
    """App exposing one model under an alias, with every upstream call streamed."""
    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "post",
        lambda self, payload, stream, rid, path="/chat/completions": FakeStream(),
    )
    return app_factory(
        providers=(make_provider_config("groq", models=[(NATIVE_ID, EXPOSED)]),),
    )


REFRAMING_ROUTES = [
    ("/v1/completions", {"prompt": "hi"}),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/api/generate", {"prompt": "hi"}),
]


@pytest.mark.parametrize("path,payload", REFRAMING_ROUTES)
def test_reframing_routes_stream_the_exposed_model_name(aliased_app, path, payload):
    body = aliased_app.test_client().post(
        path, json={"model": EXPOSED, "stream": True, **payload}
    ).get_data(as_text=True)

    assert EXPOSED in body
    assert NATIVE_ID not in body


def test_non_streaming_chat_completions_rewrites_the_model_name(aliased_app):
    """The non-streaming branch overwrites ``model`` after parsing the body."""
    container = aliased_app.extensions["llmproxy"]
    container.completions.passthrough = lambda payload, stream, rid, model=None: _Json(
        {"model": NATIVE_ID, "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    )

    data = aliased_app.test_client().post(
        "/v1/chat/completions",
        json={"model": EXPOSED, "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    ).get_json()

    assert data["model"] == EXPOSED


def test_streaming_chat_completions_relays_the_native_model_name(aliased_app):
    """Documented limit: the byte relay does not rewrite ``model`` in the chunks.

    Asserted, not fixed: a client comparing ``model`` across the two modes sees
    the native id here and the exposed one above. Rewriting would cost a parse
    and a re-serialization per chunk.
    """
    body = aliased_app.test_client().post(
        "/v1/chat/completions",
        json={"model": EXPOSED, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ).get_data(as_text=True)

    assert NATIVE_ID in body
    assert EXPOSED not in body


class _Json:
    """Minimal non-streaming response stand-in."""

    def __init__(self, data):
        self._llmproxy_json = data
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._llmproxy_json
