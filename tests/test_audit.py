"""Tests for the deferred audit trail.

They exercise the trail end-to-end through the app — a record is only correct if
every layer it crosses filled in its part — and drain the writer thread before
reading the file, since the whole point of the design is that nothing is written
while the request is still running.
"""

import json
import os

import pytest
import requests

from llmproxy import audit


COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Lorem ipsum"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}

CHUNKS = [
    {"choices": [{"index": 0, "delta": {"content": "Lorem"}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {"content": " ipsum"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
]


class FakeUpstream:
    """An upstream HTTP reply, streaming or not, in the OpenAI dialect.

    Faked at the ``requests`` session rather than at ``Provider.post``: the
    provider layer is where half of the audit record is filled in, so a test that
    replaced it would assert against a record the real code never produces.
    """

    def __init__(self, stream, status=200, data=None):
        self._stream = stream
        self._data = data if data is not None else COMPLETION
        self.status_code = status
        self.ok = status < 400
        self.text = json.dumps(self._data)
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def _lines(self):
        for chunk in CHUNKS:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"

    def iter_lines(self, decode_unicode=True):
        return self._lines()

    def iter_content(self, chunk_size=None):
        for line in self._lines():
            yield (line + "\n\n").encode("utf-8")

    def close(self):
        pass


@pytest.fixture
def upstream(monkeypatch):
    """Answer every outbound HTTP call with a :class:`FakeUpstream`."""
    import requests

    def fake_post(self, url, headers=None, json=None, stream=False, timeout=None):
        return FakeUpstream(stream)

    monkeypatch.setattr(requests.Session, "post", fake_post)


@pytest.fixture
def audited(app_factory, tmp_path):
    """Return a builder for an app with the audit trail enabled on a temp file."""
    def build(**overrides):
        path = str(tmp_path / overrides.pop("filename", "audit.jsonl"))
        app = app_factory(audit_enabled=True, audit_file=path, **overrides)
        return app, app.extensions["llmproxy"].audit, path

    return build


def drain(trail, path):
    """Stop the writer and return the records it wrote, in order."""
    trail.close(timeout=5)
    if not os.path.exists(path):
        return []  # nothing was ever submitted, so the file was never opened
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    decoder = json.JSONDecoder()
    records, index = [], 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        record, end = decoder.raw_decode(text, index)
        records.append(record)
        index = end
    return records


# -- the record ------------------------------------------------------------


def test_non_streaming_request_is_recorded_end_to_end(audited, upstream):
    """One record correlates the inbound call, the upstream leg, and the tokens."""
    app, trail, path = audited()
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model",
        "messages": [{"role": "user", "content": "ciao"}],
        "temperature": 0.2,
        "max_tokens": 64,
    })

    record, = drain(trail, path)
    assert record["endpoint"] == {
        "method": "POST", "path": "/v1/chat/completions", "route": "/v1/chat/completions",
    }
    assert record["model"]["requested"] == "test-model"
    assert record["model"]["provider"] == "test"
    assert record["params"]["temperature"] == 0.2
    assert record["params"]["max_tokens"] == 64
    assert record["request"]["messages"] == [{"role": "user", "content": "ciao"}]
    assert record["request"]["message_count"] == 1
    assert record["response"]["content"] == "Lorem ipsum"
    assert record["response"]["finish_reason"] == "stop"
    assert record["tokens"] == {"prompt": 11, "completion": 7, "total": 18}
    assert record["response"]["status"] == 200
    assert record["upstream"]["status"] == 200
    assert record["timing"]["duration_ms"] >= 0
    assert record["request_id"] == record["request_id"]  # present, correlates the log lines
    assert record["error"] is None


def test_reframed_stream_records_content_and_tokens(audited, upstream):
    """A re-framed stream (Ollama NDJSON) is recorded once its last frame is out."""
    app, trail, path = audited()
    response = app.test_client().post("/api/chat", json={
        "model": "test-model",
        "messages": [{"role": "user", "content": "ciao"}],
        "stream": True,
    })
    response.get_data()  # drain the generator: the record is written at its end

    record, = drain(trail, path)
    assert record["response"]["content"] == "Lorem ipsum"
    assert record["response"]["finish_reason"] == "stop"
    assert record["tokens"]["total"] == 18
    assert record["timing"]["ttfb_ms"] is not None
    assert record["upstream"]["stream"] is True


def test_relayed_stream_is_parsed_off_the_request_thread(audited, upstream):
    """The byte relay buffers the SSE; the writer decodes content and usage from it."""
    app, trail, path = audited()
    response = app.test_client().post("/v1/chat/completions", json={
        "model": "test-model",
        "messages": [{"role": "user", "content": "ciao"}],
        "stream": True,
    })
    response.get_data()

    record, = drain(trail, path)
    assert record["response"]["content"] == "Lorem ipsum"
    assert record["response"]["finish_reason"] == "stop"
    assert record["tokens"] == {"prompt": 11, "completion": 7, "total": 18}


def test_an_upstream_refusal_keeps_its_reason(audited, monkeypatch):
    """The provider's error body is recorded: the status alone rarely says why."""
    def refusing_post(self, url, headers=None, json=None, stream=False, timeout=None):
        return FakeUpstream(stream, status=400, data={"error": {"message": "unknown model"}})

    monkeypatch.setattr(requests.Session, "post", refusing_post)
    app, trail, path = audited()
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
    })

    record, = drain(trail, path)
    assert record["response"]["status"] == 400
    assert record["upstream"]["status"] == 400
    assert "unknown model" in record["error"]["message"]
    assert record["tokens"]["total"] is None


def test_an_unreachable_upstream_is_recorded_as_such(audited, monkeypatch):
    """A call that never got a response still produces a record, with the cause."""
    def unreachable(self, url, headers=None, json=None, stream=False, timeout=None):
        raise requests.exceptions.ConnectTimeout("no route to host")

    monkeypatch.setattr(requests.Session, "post", unreachable)
    app, trail, path = audited()
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
    })

    record, = drain(trail, path)
    assert record["response"]["status"] == 502
    assert record["error"]["type"] == "ConnectTimeout"
    assert record["upstream"]["status"] is None


# -- sessions --------------------------------------------------------------


def test_session_id_comes_from_the_header_when_the_client_sends_one(audited, upstream):
    """An explicit conversation header is authoritative and reported as such."""
    app, trail, path = audited()
    app.test_client().post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "ciao"}]},
        headers={"X-Session-Id": "conv-42"},
    )

    record, = drain(trail, path)
    assert record["session"] == {"id": "conv-42", "source": "header:X-Session-Id"}


def test_turns_of_one_conversation_fingerprint_to_the_same_session(audited, upstream):
    """Without a header, the opening user message groups the turns of a chat."""
    app, trail, path = audited()
    client = app.test_client()
    first = [{"role": "user", "content": "ciao"}]
    second = first + [
        {"role": "assistant", "content": "Lorem ipsum"},
        {"role": "user", "content": "e poi?"},
    ]
    for messages in (first, second):
        client.post("/v1/chat/completions", json={"model": "test-model", "messages": messages})
    other = [{"role": "user", "content": "un altro discorso"}]
    client.post("/v1/chat/completions", json={"model": "test-model", "messages": other})

    turn_one, turn_two, unrelated = drain(trail, path)
    assert turn_one["session"]["id"] == turn_two["session"]["id"]
    assert turn_one["session"]["source"] == "fingerprint"
    assert unrelated["session"]["id"] != turn_one["session"]["id"]


def test_a_custom_session_header_can_be_configured(audited, upstream):
    """``AUDIT_SESSION_HEADER`` adds a front-end's own header to the known ones."""
    app, trail, path = audited(audit_session_header="X-OpenWebUI-Chat-Id")
    app.test_client().post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "ciao"}]},
        headers={"X-OpenWebUI-Chat-Id": "chat-7"},
    )

    record, = drain(trail, path)
    assert record["session"]["id"] == "chat-7"


# -- capture policy --------------------------------------------------------


def test_bodies_none_keeps_the_accounting_and_drops_the_content(audited, upstream):
    """``AUDIT_BODIES=none`` audits cost and routing without recording prompts."""
    app, trail, path = audited(audit_bodies="none")
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "segreto"}],
    })

    record, = drain(trail, path)
    assert "messages" not in record["request"]
    assert record["request"]["input_chars"] == len("segreto")
    assert record["response"]["content"] is None
    assert record["response"]["content_chars"] == len("Lorem ipsum")
    assert record["tokens"]["total"] == 18


def test_long_texts_are_clipped_to_the_configured_budget(audited, upstream):
    """Truncation caps the record's size and says how much it cut."""
    app, trail, path = audited(audit_max_chars=5)
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "x" * 100}],
    })

    record, = drain(trail, path)
    prompt = record["request"]["messages"][0]["content"]
    assert prompt.startswith("xxxxx")
    assert "+95 chars" in prompt
    assert record["request"]["input_chars"] == 100


def test_an_inbound_api_key_is_recorded_as_a_digest(audited, upstream):
    """Callers are correlated by a hash: the audit file never holds the credential."""
    app, trail, path = audited()
    app.test_client().post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "ciao"}]},
        headers={"Authorization": "Bearer super-secret"},
    )

    record, = drain(trail, path)
    assert record["client"]["api_key_id"].startswith("sha256:")
    assert "super-secret" not in json.dumps(record)


# -- what is worth a record ------------------------------------------------


def test_successful_discovery_calls_are_not_recorded(audited, upstream):
    """A 200 GET has no prompt and no tokens; a failing one is kept."""
    app, trail, path = audited()
    client = app.test_client()
    client.get("/v1/models")
    client.get("/health")
    client.get("/v1/models/nope")

    record, = drain(trail, path)
    assert record["endpoint"]["path"] == "/v1/models/nope"
    assert record["response"]["status"] == 404


def test_the_dashboard_does_not_audit_itself(audited, upstream):
    """``/stats`` is exempt, as it is for metrics: polling it must not fill the trail."""
    app, trail, path = audited()
    app.test_client().get("/stats.json")

    assert drain(trail, path) == []


def test_a_rejected_key_is_audited(audited, upstream):
    """A 401 is exactly what the trail is read for, so it produces a record."""
    app, trail, path = audited(proxy_api_key="right-key")
    app.test_client().post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "ciao"}]},
        headers={"Authorization": "Bearer wrong-key"},
    )

    record, = drain(trail, path)
    assert record["response"]["status"] == 401


# -- the trail itself ------------------------------------------------------


def test_the_trail_is_off_unless_enabled(app_factory, upstream):
    """Disabled is the default, and a disabled trail records nothing at all."""
    app = app_factory()
    trail = app.extensions["llmproxy"].audit
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
    })

    assert trail.enabled is False
    assert trail.snapshot()["written"] == 0


def test_a_full_queue_drops_records_instead_of_blocking(audited, upstream, monkeypatch):
    """Back-pressure must never reach the request: the record is dropped and counted."""
    app, trail, path = audited(audit_queue_size=1)
    # Stall the writer so the queue cannot drain while the requests run.
    stalled = []
    monkeypatch.setattr(trail, "_write", lambda event: stalled.append(event))
    trail._queue.put_nowait(object())  # occupy the single slot

    client = app.test_client()
    for _ in range(3):
        client.post("/v1/chat/completions", json={
            "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
        })

    assert trail.snapshot()["dropped"] >= 2


def test_the_file_rotates_at_the_configured_size(audited, upstream):
    """Past ``AUDIT_MAX_MB`` the file rolls over instead of growing without bound."""
    app, trail, path = audited()
    trail.max_bytes = 2500  # a couple of records
    client = app.test_client()
    for _ in range(6):
        client.post("/v1/chat/completions", json={
            "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
        })

    records = drain(trail, path)
    assert os.path.exists(path + ".1")
    assert len(records) < 6  # the rest is in the rolled-over files
    assert os.path.getsize(path) <= 2500


def test_pretty_format_writes_indented_records(audited, upstream):
    """``AUDIT_FORMAT=pretty`` trades one-line-per-record for readability."""
    app, trail, path = audited(audit_format="pretty")
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
    })
    trail.close(timeout=5)

    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("{\n  ")
    assert json.loads(text)["response"]["content"] == "Lorem ipsum"


def test_the_event_is_unbound_when_the_request_ends(audited, upstream):
    """A leftover binding would attribute the next request's upstream call to this one."""
    app, trail, path = audited()
    app.test_client().post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "ciao"}],
    })

    assert audit.current() is audit.NO_AUDIT
