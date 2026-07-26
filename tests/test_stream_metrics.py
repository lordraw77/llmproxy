"""Regression tests for F9 — a streaming request is accounted for when it ends.

``after_request`` runs when the headers are emitted. For a streaming route that
is before the first token exists, so recording there measured the handler setup
and called it the request latency, and ``teardown_request`` dropped the in-flight
gauge on a request that had not started producing output. Both are now deferred
to the generator's ``finally``.

The upstream here deliberately sleeps between chunks: it is the only way to tell
"measured the whole stream" from "measured the handler" with a clock.
"""

import json
import time

import pytest

from llmproxy.providers.openai_compatible import OpenAICompatibleProvider


#: Wall time the fake upstream spends producing its body, in seconds. Large
#: enough to dwarf the handler setup, small enough not to slow the suite.
STREAM_SECONDS = 0.05

CHUNKS = [
    {"choices": [{"index": 0, "delta": {"content": "Lorem"}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {"content": " ipsum"}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
]


class SlowStream:
    """A streaming upstream that takes :data:`STREAM_SECONDS` to deliver its body."""

    def __init__(self):
        self.closed = False

    def _lines(self):
        for chunk in CHUNKS:
            time.sleep(STREAM_SECONDS / len(CHUNKS))
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"

    def iter_lines(self, decode_unicode=True):
        return self._lines()

    def iter_content(self, chunk_size=None):
        for line in self._lines():
            yield (line + "\n\n").encode("utf-8")

    def close(self):
        self.closed = True


@pytest.fixture
def slow_upstream(monkeypatch):
    """Make every provider call return a fresh :class:`SlowStream`."""
    def fake_post(self, payload, stream, rid, path="/chat/completions"):
        return SlowStream()

    monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)


STREAMING_ROUTES = [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/completions", {"prompt": "hi"}),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/api/generate", {"prompt": "hi"}),
    ("/completion", {"prompt": "hi"}),
]


def _body(payload):
    return {"model": "test-model", "stream": True, **payload}


@pytest.mark.parametrize("path,payload", STREAMING_ROUTES)
def test_latency_covers_the_whole_stream_not_just_the_handler(app_factory, slow_upstream, path, payload):
    app = app_factory()
    metrics = app.extensions["llmproxy"].metrics

    # ``get_data`` matters: the test client does not drain a streamed body on its
    # own, and an undrained stream settles early exactly like a client hang-up.
    app.test_client().post(path, json=_body(payload)).get_data()

    assert metrics.lat_count == 1
    # Pre-fix this was the handler setup only — well under a millisecond.
    assert metrics.lat_max >= STREAM_SECONDS * 1000


@pytest.mark.parametrize("path,payload", STREAMING_ROUTES)
def test_the_request_stays_in_flight_until_the_last_frame(app_factory, slow_upstream, path, payload):
    """The gauge must still count a request whose body is being generated."""
    app = app_factory()
    metrics = app.extensions["llmproxy"].metrics

    with app.test_request_context(path, method="POST", json=_body(payload)):
        response = app.full_dispatch_request()  # before_request + route + after_request
    # The request context is gone, but the body has not been produced yet.
    assert metrics.in_flight == 1
    assert metrics.total_requests == 0, "recorded before the stream even started"

    generator = response.response
    assert next(iter(generator)), "one frame delivered, still streaming"
    assert metrics.in_flight == 1

    list(generator)
    assert metrics.in_flight == 0
    assert metrics.total_requests == 1


@pytest.mark.parametrize("path,payload", STREAMING_ROUTES)
def test_a_client_hang_up_still_settles_the_request(app_factory, slow_upstream, path, payload):
    """An aborted stream must not leak the in-flight gauge — the common failure mode."""
    app = app_factory()
    metrics = app.extensions["llmproxy"].metrics

    with app.test_request_context(path, method="POST", json=_body(payload)):
        response = app.full_dispatch_request()

    generator = response.response
    next(iter(generator))
    generator.close()  # the client disconnects mid-stream

    assert metrics.in_flight == 0
    assert metrics.total_requests == 1


def test_the_status_code_survives_the_deferral(app_factory, slow_upstream):
    """``after_request`` no longer records, so it must still hand over the status."""
    app = app_factory()
    metrics = app.extensions["llmproxy"].metrics

    app.test_client().post("/api/chat", json=_body({"messages": [{"role": "user", "content": "hi"}]}))

    assert metrics.by_status == {"200": 1}
    assert metrics.by_path == {"/api/chat": 1}
    assert metrics.errors == 0


def test_non_streaming_requests_are_still_recorded_by_after_request(app_factory, slow_upstream):
    """The deferral must not swallow the ordinary path."""
    app = app_factory()
    metrics = app.extensions["llmproxy"].metrics

    app.test_client().get("/api/version")

    assert metrics.total_requests == 1
    assert metrics.in_flight == 0
    assert metrics.by_path == {"/api/version": 1}


def test_exempt_paths_are_not_tracked_by_the_stream_wrapper(app_factory, slow_upstream):
    """``/stats`` is excluded from metrics; the deferral must keep it that way."""
    app = app_factory()
    metrics = app.extensions["llmproxy"].metrics

    app.test_client().get("/stats.json")

    assert metrics.total_requests == 0
    assert metrics.in_flight == 0
