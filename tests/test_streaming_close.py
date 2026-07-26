"""Regression tests for F4 — streaming upstream responses are always closed.

``iter_openai_sse`` stops at the ``[DONE]`` marker, so the upstream body is never
drained to EOF: unless the route generator closes it explicitly the connection is
dropped instead of returning to the pool, and under repeated client aborts the
pool starts logging ``Connection pool is full, discarding connection``.

Every streaming route must therefore close the upstream both on the normal path
and when the client hangs up mid-stream (the generator is closed early, which
raises ``GeneratorExit`` inside it).
"""

import json

import pytest

from llmproxy.providers.openai_compatible import OpenAICompatibleProvider


CHUNKS = [
    {"choices": [{"index": 0, "delta": {"content": "Lorem"}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {"content": " ipsum"}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
]


class FakeStream:
    """A streaming upstream response that records whether it was closed.

    Mirrors the two read surfaces the web layer uses: ``iter_lines`` (re-framing
    routes) and ``iter_content`` (the raw relay in ``/v1/chat/completions``).
    """

    def __init__(self, chunks=CHUNKS):
        self._chunks = chunks
        self.closed = False
        self.consumed = 0

    def _lines(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"
        yield "data: never-reached"  # the parser breaks at [DONE]: body left undrained

    def iter_lines(self, decode_unicode=True):
        return self._lines()

    def iter_content(self, chunk_size=None):
        for line in self._lines():
            yield (line + "\n\n").encode("utf-8")

    def close(self):
        self.closed = True


@pytest.fixture
def stream(monkeypatch):
    """Make every provider call return a single shared :class:`FakeStream`."""
    fake = FakeStream()

    def fake_post(self, payload, stream, rid, path="/chat/completions"):
        return fake

    monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)
    return fake


STREAMING_ROUTES = [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/completions", {"prompt": "hi"}),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/api/generate", {"prompt": "hi"}),
    ("/completion", {"prompt": "hi"}),
]


@pytest.mark.parametrize("path,payload", STREAMING_ROUTES)
def test_upstream_is_closed_after_a_fully_consumed_stream(app_factory, stream, path, payload):
    client = app_factory().test_client()
    resp = client.post(path, json={"model": "test-model", "stream": True, **payload})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert body, "the route must still emit its frames"
    assert stream.closed


@pytest.mark.parametrize("path,payload", STREAMING_ROUTES)
def test_upstream_is_closed_when_the_client_hangs_up_midway(app_factory, stream, path, payload):
    """The common case in practice: a client aborting after the first token."""
    app = app_factory()
    with app.test_request_context(
        path, method="POST", json={"model": "test-model", "stream": True, **payload}
    ):
        response = app.dispatch_request()

    generator = response.response
    next(iter(generator))  # one frame delivered...
    assert not stream.closed
    generator.close()  # ...then the client disconnects

    assert stream.closed
    assert stream.consumed < len(CHUNKS), "the upstream was abandoned mid-body"


def test_the_relay_route_streams_the_upstream_bytes_unchanged(app_factory, stream):
    """Closing must not disturb what /v1/chat/completions relays."""
    client = app_factory().test_client()
    body = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ).get_data(as_text=True)

    assert "Lorem" in body and " ipsum" in body
    assert "data: [DONE]" in body


def test_aggregating_a_forced_stream_closes_the_upstream(app_factory):
    """FORCE_UPSTREAM_STREAM has the same leak: the [DONE] break skips the tail."""
    fake = FakeStream()
    fake.status_code, fake.ok, fake.headers = 200, True, {}
    app = app_factory(force_upstream_stream=True)
    for provider in app.extensions["llmproxy"].registry.providers:
        provider._session = _StubSession(fake)

    client = app.test_client()
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert resp.get_json()["choices"][0]["message"]["content"] == "Lorem ipsum"
    assert fake.closed


class _StubSession:
    """Stands in for ``requests.Session`` so ``Provider.post`` runs offline."""

    def __init__(self, stream):
        self._stream = stream

    def post(self, url, **kwargs):
        self._stream.status_code = 200
        self._stream.ok = True
        self._stream.headers = {}
        return self._stream
