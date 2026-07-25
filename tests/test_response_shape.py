"""Regression tests for F6 — the upstream response shape is never trusted.

Four routes read ``data["choices"][0]["message"]["content"]`` directly. An
upstream answering 200 with ``{"choices": []}`` (content filter, applicative
error, provider off-standard) turned that into an ``IndexError`` and a 500 with a
traceback. The shared helpers in ``web.formatting`` degrade to an empty assistant
message instead.
"""

import pytest

from llmproxy.providers.base import AggregatedResponse
from llmproxy.providers.openai_compatible import OpenAICompatibleProvider
from llmproxy.web.formatting import first_content, first_message


# --- the helpers themselves ------------------------------------------------

def test_first_message_returns_the_message_when_present():
    data = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    assert first_message(data) == {"role": "assistant", "content": "hi"}


@pytest.mark.parametrize("data", [
    {},
    {"choices": []},
    {"choices": None},
    {"choices": [{}]},
    {"choices": [{"message": None}]},
])
def test_first_message_degrades_to_an_empty_assistant_message(data):
    assert first_message(data) == {"role": "assistant", "content": ""}


def test_first_content_normalizes_a_tool_only_reply_to_the_empty_string():
    """``content`` is legitimately None when the reply carries only tool_calls."""
    data = {"choices": [{"message": {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "f", "arguments": "{}"}}],
    }}]}
    assert first_content(data) == ""


def test_first_content_keeps_the_tool_calls_reachable():
    """Normalizing the text must not throw away what the dialects may grow to use."""
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    data = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    assert first_message(data)["tool_calls"] == [call]


# --- the routes ------------------------------------------------------------

MALFORMED = [
    pytest.param({"id": "x", "object": "chat.completion", "choices": []}, id="empty-choices"),
    pytest.param({"id": "x", "object": "chat.completion"}, id="no-choices-key"),
    pytest.param({"id": "x", "choices": [{"index": 0, "finish_reason": "content_filter"}]},
                 id="choice-without-message"),
    pytest.param({"id": "x", "choices": [{"index": 0, "message": {"role": "assistant",
                                                                  "content": None}}]},
                 id="null-content"),
]

ROUTES = [
    ("/v1/completions", {"prompt": "hi"}, lambda j: j["choices"][0]["text"]),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]},
     lambda j: j["message"]["content"]),
    ("/api/generate", {"prompt": "hi"}, lambda j: j["response"]),
    ("/completion", {"prompt": "hi"}, lambda j: j["content"]),
]


@pytest.fixture
def upstream_body(monkeypatch):
    """Return a setter that pins the JSON body every provider call replies with."""
    def use(body):
        def fake_post(self, payload, stream, rid, path="/chat/completions"):
            return AggregatedResponse(body)

        monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)

    return use


@pytest.mark.parametrize("body", MALFORMED)
@pytest.mark.parametrize("path,payload,extract", ROUTES,
                         ids=[r[0] for r in ROUTES])
def test_a_malformed_upstream_reply_does_not_500(
    client, upstream_body, body, path, payload, extract
):
    upstream_body(body)
    resp = client.post(path, json={"model": "test-model", "stream": False, **payload})

    assert resp.status_code == 200
    assert extract(resp.get_json()) == ""


@pytest.mark.parametrize("path,payload,extract", ROUTES, ids=[r[0] for r in ROUTES])
def test_a_well_formed_reply_is_still_passed_through(
    client, upstream_body, path, payload, extract
):
    """The guard must not swallow the normal case."""
    upstream_body({
        "id": "x",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Lorem"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    })
    resp = client.post(path, json={"model": "test-model", "stream": False, **payload})

    assert resp.status_code == 200
    assert extract(resp.get_json()) == "Lorem"


def test_the_embeddings_routes_were_already_defended(client, monkeypatch):
    """``(data.get("data") or [{}])[0]`` — pinned so the pattern is not lost."""
    def fake_post(self, payload, stream, rid, path="/chat/completions"):
        return AggregatedResponse({"object": "list", "data": []})

    monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)

    assert client.post("/api/embeddings", json={"model": "test-embed", "prompt": "hi"}).get_json() == {
        "embedding": []
    }
    assert client.post("/api/embed", json={"model": "test-embed", "input": "hi"}).status_code == 200
