"""Regression tests for the hot-path optimizations.

Each of these pins a property that a later "harmless" refactor would silently
undo, turning a fast path back into the slow one it replaced. They assert
*behaviour*, not timings: a benchmark in CI is a flaky test, but "this code path
does not serialize anything" is a fact that can be checked directly.
"""

import json

import pytest

from llmproxy.audit import content_chars, input_chars
from llmproxy.providers.base import TranslatedStream
from llmproxy.upstream.sse import iter_openai_sse


# --- native streaming takes the decoded fast path ---------------------------


def chunk(text, finish_reason=None, usage=None):
    """One OpenAI ``chat.completion.chunk``, as a native provider builds it."""
    out = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
           "choices": [{"index": 0, "delta": {"content": text} if text else {},
                        "finish_reason": finish_reason}]}
    if usage:
        out["usage"] = usage
    return out


class LinesOnly:
    """A stream offering only ``iter_lines`` — the pre-optimization surface."""

    def __init__(self, chunks):
        self._chunks = chunks

    def iter_lines(self, decode_unicode=True):
        for c in self._chunks:
            yield "data: " + json.dumps(c)
        yield "data: [DONE]"


CHUNKS = [
    chunk("Hello"),
    chunk(" world"),
    chunk("", finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2,
                                           "total_tokens": 5}),
]


def test_decoded_and_serialized_paths_agree_exactly():
    """The fast path must be indistinguishable from the SSE text it replaced."""
    fast_usage, fast_meta = {}, {}
    fast = "".join(iter_openai_sse(TranslatedStream(iter(CHUNKS)), fast_usage, fast_meta))

    slow_usage, slow_meta = {}, {}
    slow = "".join(iter_openai_sse(LinesOnly(CHUNKS), slow_usage, slow_meta))

    assert fast == slow == "Hello world"
    assert fast_usage == slow_usage == {"prompt_tokens": 3, "completion_tokens": 2,
                                        "total_tokens": 5}
    assert fast_meta == slow_meta


def test_a_translated_stream_is_never_serialized_to_be_reparsed():
    """The whole point: no ``json.dumps`` per token just to cross the boundary.

    ``iter_lines`` is the serializing surface. If the parser ever goes back to
    preferring it over ``iter_chunks``, this fails.
    """
    class Tripwire(TranslatedStream):
        def iter_lines(self, decode_unicode=True):
            raise AssertionError("iter_openai_sse re-serialized an already-decoded stream")

    assert "".join(iter_openai_sse(Tripwire(iter(CHUNKS)), {}, {})) == "Hello world"


def test_translated_stream_still_serializes_for_the_byte_relay():
    """``iter_content``/``iter_lines`` stay: the relay route needs the wire form."""
    stream = TranslatedStream(iter(CHUNKS))
    body = b"".join(stream.iter_content())
    assert body.endswith(b"data: [DONE]\n\n")
    assert b'"content": "Hello"' in body or b'"content":"Hello"' in body


# --- input_chars counts without materializing -------------------------------


def test_content_chars_counts_text_blocks_not_the_repr():
    """A multimodal message is measured by its text, not by ``str()`` of the list."""
    content = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}},
    ]
    # The old spelling built the whole repr — base64 payload included — to count it.
    assert content_chars(content) == len("hello")
    assert content_chars("plain") == 5
    assert content_chars(None) == 0


def test_input_chars_handles_every_inbound_shape():
    assert input_chars([{"role": "user", "content": "abcd"}]) == 4
    assert input_chars(None, "prompt") == 6
    assert input_chars(None, ["ab", "cd"]) == 4
    assert input_chars(None, None) == 0


# --- authentication precedes body parsing -----------------------------------


@pytest.fixture
def count_json_parses(monkeypatch):
    """Count how many times the inbound body is deserialized, per request."""
    from flask.wrappers import Request

    calls = []
    original = Request.get_json

    def counting(self, *args, **kwargs):
        calls.append(self.path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Request, "get_json", counting)
    return calls


def test_unauthenticated_request_is_rejected_without_parsing_the_body(
        app_factory, count_json_parses):
    """A refused caller must not get the worker to parse what they sent.

    Parsing before authenticating let anyone who can reach the port spend a
    worker thread on an arbitrarily large JSON document, and hash a session
    fingerprint over it, before being told no.
    """
    app = app_factory(proxy_api_key="secret")
    with app.test_client() as client:
        resp = client.post(
            "/v1/chat/completions",
            data=json.dumps({"model": "test-model",
                             "messages": [{"role": "user", "content": "x" * 10_000}]}),
            content_type="application/json",
            headers={"Authorization": "Bearer wrong"},
        )

    assert resp.status_code == 401
    assert resp.get_json()["error"]["type"] == "authentication_error"
    assert count_json_parses == [], "the rejected body was deserialized anyway"


def test_an_accepted_request_is_still_parsed_once(app_factory, count_json_parses):
    """The reordering must not cost the access log its body-derived fields.

    One parse, not zero and not two: the middleware reads ``model``/``stream``
    for the log line and Flask memoizes the result for the route.
    """
    app = app_factory(proxy_api_key="secret")
    with app.test_client() as client:
        resp = client.post(
            "/api/show", json={"model": "test-model"},
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 200
    assert len(count_json_parses) >= 1


def test_a_rejected_key_is_still_audited(app_factory, tmp_path):
    """Auth failures stay in the trail — that is what it is read for.

    Moving the parse after the check must not turn a 401 into a request the
    audit never saw; only its *body* is left unrecorded.
    """
    trail = tmp_path / "audit.jsonl"
    app = app_factory(proxy_api_key="secret", audit_enabled=True,
                      audit_file=str(trail), audit_queue_size=16)
    container = app.extensions["llmproxy"]
    with app.test_client() as client:
        client.post("/v1/chat/completions", json={"model": "test-model"},
                    headers={"Authorization": "Bearer wrong"})
    container.audit.close(timeout=5)

    records = [json.loads(line) for line in trail.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["endpoint"]["path"] == "/v1/chat/completions"
    assert records[0]["response"]["status"] == 401
    # The refused body is not in the file.
    assert records[0]["request"]["input_chars"] == 0


# --- inbound body size cap --------------------------------------------------


def test_oversized_body_is_refused_in_the_openai_error_shape(app_factory):
    """A 413 must stay JSON: an SDK reads Werkzeug's HTML page as a decode error."""
    app = app_factory(max_request_bytes=1024)
    with app.test_client() as client:
        resp = client.post(
            "/v1/chat/completions",
            data=json.dumps({"model": "test-model",
                             "messages": [{"role": "user", "content": "x" * 5000}]}),
            content_type="application/json",
        )
    assert resp.status_code == 413
    body = resp.get_json()
    assert body["error"]["code"] == "request_too_large"
    assert body["error"]["type"] == "invalid_request_error"


def test_a_body_within_the_cap_is_not_refused(app_factory):
    app = app_factory(max_request_bytes=1024 * 1024)
    with app.test_client() as client:
        resp = client.post("/api/show", json={"model": "test-model"})
    assert resp.status_code == 200


def test_zero_disables_the_cap(app_factory):
    """0 restores Flask's own default of no limit."""
    app = app_factory(max_request_bytes=0)
    assert app.config["MAX_CONTENT_LENGTH"] is None


# --- cache isolation contract ------------------------------------------------


@pytest.mark.parametrize("mutate_key", ["model", "object"])
def test_served_cache_body_is_isolated_at_the_top_level(mutate_key):
    """The guarantee the shallow copy actually makes, per key the routes touch."""
    from llmproxy.cache import ResponseCache

    cache = ResponseCache(enabled=True, ttl=300, max_size=8, policy="all")
    cache.set("k", {"model": "native", "object": "list", "data": [1]})
    served = cache.get("k")
    served[mutate_key] = "rewritten"
    assert cache.get("k")[mutate_key] != "rewritten"
