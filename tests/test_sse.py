"""Unit tests for :func:`llmproxy.upstream.sse.iter_openai_sse`.

This is the parser every streaming route depends on, and the only place where a
tool call is reconstructed from its incremental fragments. It is fed a fake
response object exposing just ``iter_lines``, which is all the function reads.
"""

import json

from llmproxy.upstream.sse import iter_openai_sse


class FakeStream:
    """Minimal stand-in for a streaming ``requests.Response``."""

    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


def sse(*chunks):
    """Frame ``chunks`` as SSE data lines, terminated by ``[DONE]``."""
    return FakeStream([f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"])


def delta(content=None, tool_calls=None, finish_reason=None, usage=None):
    """Build one OpenAI ``chat.completion.chunk``."""
    d = {}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    chunk = {"choices": [{"delta": d, "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


# --- content ---------------------------------------------------------------

def test_yields_the_content_of_each_delta():
    stream = sse(delta("Hello"), delta(" "), delta("world"))
    assert list(iter_openai_sse(stream)) == ["Hello", " ", "world"]


def test_empty_and_missing_content_is_skipped():
    stream = sse(delta("a"), delta(""), delta(), delta("b"))
    assert list(iter_openai_sse(stream)) == ["a", "b"]


def test_done_marker_stops_the_iteration():
    stream = FakeStream([
        f"data: {json.dumps(delta('kept'))}",
        "data: [DONE]",
        f"data: {json.dumps(delta('after the end'))}",
    ])
    assert list(iter_openai_sse(stream)) == ["kept"]


def test_blank_lines_and_comments_are_ignored():
    """SSE keep-alives arrive as empty lines or ':' comments, not JSON."""
    stream = FakeStream([
        "",
        ": keep-alive",
        f"data: {json.dumps(delta('x'))}",
        "",
        "data: [DONE]",
    ])
    assert list(iter_openai_sse(stream)) == ["x"]


def test_malformed_json_is_skipped_instead_of_raising():
    stream = FakeStream([
        "data: {not json",
        f"data: {json.dumps(delta('ok'))}",
        "data: [DONE]",
    ])
    assert list(iter_openai_sse(stream)) == ["ok"]


def test_chunk_without_choices_is_skipped():
    """The usage-only final chunk of ``include_usage`` carries an empty choices list."""
    stream = sse({"choices": [], "usage": {"total_tokens": 5}}, delta("hi"))
    assert list(iter_openai_sse(stream)) == ["hi"]


def test_stream_without_a_done_marker_terminates_on_exhaustion():
    stream = FakeStream([f"data: {json.dumps(delta('a'))}"])
    assert list(iter_openai_sse(stream)) == ["a"]


# --- usage -----------------------------------------------------------------

def test_usage_is_collected_into_the_output_dict():
    usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    stream = sse(delta("hi"), {"choices": [], "usage": usage})
    out = {}
    list(iter_openai_sse(stream, usage_out=out))
    assert out == usage


def test_usage_from_a_later_chunk_overrides_the_earlier_one():
    stream = sse(
        {"choices": [], "usage": {"total_tokens": 1}},
        {"choices": [], "usage": {"total_tokens": 42}},
    )
    out = {}
    list(iter_openai_sse(stream, usage_out=out))
    assert out["total_tokens"] == 42


def test_usage_out_stays_empty_when_the_upstream_sends_none():
    out = {}
    list(iter_openai_sse(sse(delta("hi")), usage_out=out))
    assert out == {}


# --- meta: finish_reason ---------------------------------------------------

def test_finish_reason_is_the_last_non_null_one():
    stream = sse(delta("a"), delta("b"), delta(finish_reason="stop"))
    meta = {}
    list(iter_openai_sse(stream, meta_out=meta))
    assert meta["finish_reason"] == "stop"


def test_finish_reason_is_none_when_never_sent():
    meta = {}
    list(iter_openai_sse(sse(delta("a")), meta_out=meta))
    assert meta["finish_reason"] is None


# --- meta: tool calls ------------------------------------------------------

def test_tool_call_is_reassembled_from_its_fragments():
    """Only the first fragment carries id/type/name; the rest append arguments."""
    stream = sse(
        delta(tool_calls=[{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        }]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"ci'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ty": "Rome"}'}}]),
        delta(finish_reason="tool_calls"),
    )
    meta = {}
    assert list(iter_openai_sse(stream, meta_out=meta)) == []
    assert meta["finish_reason"] == "tool_calls"
    assert len(meta["tool_calls"]) == 1

    call = meta["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert call["function"] == {"name": "get_weather", "arguments": '{"city": "Rome"}'}


def test_parallel_tool_calls_are_kept_apart_and_ordered_by_index():
    stream = sse(
        delta(tool_calls=[{"index": 1, "id": "b", "function": {"name": "second"}}]),
        delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "first"}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": "{}"}}]),
        delta(tool_calls=[{"index": 1, "function": {"arguments": "[]"}}]),
    )
    meta = {}
    list(iter_openai_sse(stream, meta_out=meta))
    names = [c["function"]["name"] for c in meta["tool_calls"]]
    assert names == ["first", "second"], "sorted by index, not by arrival order"
    assert [c["id"] for c in meta["tool_calls"]] == ["a", "b"]


def test_a_fragment_without_index_defaults_to_slot_zero():
    stream = sse(
        delta(tool_calls=[{"id": "x", "function": {"name": "f", "arguments": "{"}}]),
        delta(tool_calls=[{"function": {"arguments": "}"}}]),
    )
    meta = {}
    list(iter_openai_sse(stream, meta_out=meta))
    assert len(meta["tool_calls"]) == 1
    assert meta["tool_calls"][0]["function"]["arguments"] == "{}"


def test_a_split_function_name_is_concatenated():
    stream = sse(
        delta(tool_calls=[{"index": 0, "id": "x", "function": {"name": "get_"}}]),
        delta(tool_calls=[{"index": 0, "function": {"name": "weather"}}]),
    )
    meta = {}
    list(iter_openai_sse(stream, meta_out=meta))
    assert meta["tool_calls"][0]["function"]["name"] == "get_weather"


def test_tool_calls_is_none_when_the_stream_carries_none():
    meta = {}
    list(iter_openai_sse(sse(delta("plain text")), meta_out=meta))
    assert meta["tool_calls"] is None


def test_content_and_tool_calls_can_coexist():
    stream = sse(
        delta("Let me check. "),
        delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "f", "arguments": "{}"}}]),
        delta(finish_reason="tool_calls"),
    )
    meta = {}
    assert list(iter_openai_sse(stream, meta_out=meta)) == ["Let me check. "]
    assert meta["tool_calls"][0]["id"] == "c"


def test_meta_is_untouched_when_not_requested():
    """Without ``meta_out`` the accumulator work is skipped entirely."""
    assert list(iter_openai_sse(sse(delta("a")))) == ["a"]
