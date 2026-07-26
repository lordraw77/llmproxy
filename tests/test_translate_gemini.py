"""Unit tests for F5a — OpenAI ``messages`` -> Gemini ``contents``.

Before the fix ``_to_contents`` did ``str(content)`` on anything that was not a
string (so a block list was sent as a Python ``repr``), dropped ``tool_calls``
entirely, and turned a ``role="tool"`` message into plain user text — the tool
result never reached the model. Consecutive same-role turns were emitted as-is.

No network here: the translation is a pure function.
"""

import json

import pytest

from llmproxy.providers.translate.gemini import to_contents


def user(text):
    return {"role": "user", "content": text}


# --- plain text ------------------------------------------------------------

def test_simple_exchange_maps_roles():
    _, contents = to_contents([
        user("hi"),
        {"role": "assistant", "content": "hello"},
        user("how are you"),
    ])

    assert contents == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
        {"role": "user", "parts": [{"text": "how are you"}]},
    ]


def test_system_messages_are_lifted_and_concatenated():
    system, contents = to_contents([
        {"role": "system", "content": "be terse"},
        user("hi"),
        {"role": "system", "content": "and polite"},
    ])

    assert system == {"parts": [{"text": "be terse"}, {"text": "and polite"}]}
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_no_system_message_yields_no_instruction():
    system, _ = to_contents([user("hi")])
    assert system is None


def test_empty_and_missing_content_produces_no_turn():
    """Content.parts must be non-empty upstream: an empty turn is dropped."""
    _, contents = to_contents([user(""), {"role": "assistant"}, user("hi")])
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


# --- role alternation ------------------------------------------------------

def test_consecutive_same_role_messages_are_merged():
    _, contents = to_contents([user("first"), user("second")])

    assert contents == [{"role": "user", "parts": [{"text": "first"}, {"text": "second"}]}]


def test_a_system_message_between_two_user_turns_does_not_split_them():
    """The common case: dropping the system message leaves two adjacent users."""
    _, contents = to_contents([
        user("first"),
        {"role": "system", "content": "be terse"},
        user("second"),
    ])

    assert [c["role"] for c in contents] == ["user"]


def test_unknown_roles_are_treated_as_user():
    _, contents = to_contents([{"role": "developer", "content": "hi"}])
    assert contents[0]["role"] == "user"


# --- block content (the str() repr bug) ------------------------------------

def test_text_blocks_become_text_parts():
    _, contents = to_contents([{"role": "user", "content": [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": " and this"},
    ]}])

    assert contents[0]["parts"] == [{"text": "look at this"}, {"text": " and this"}]


def test_block_content_never_leaks_a_python_repr():
    _, contents = to_contents([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])

    rendered = json.dumps(contents)
    assert "'type'" not in rendered and "{'" not in rendered


def test_data_uri_image_becomes_inline_data():
    _, contents = to_contents([{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]}])

    assert contents[0]["parts"] == [
        {"text": "what is this?"},
        {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
    ]


def test_data_uri_keeps_only_the_mime_type_from_the_header():
    _, contents = to_contents([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;charset=utf-8;base64,QQ=="}},
    ]}])

    assert contents[0]["parts"][0]["inlineData"]["mimeType"] == "image/jpeg"


def test_remote_image_url_becomes_file_data():
    """Gemini will refuse a URI it does not own — better than dropping the image."""
    _, contents = to_contents([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/cat.png"}},
    ]}])

    assert contents[0]["parts"] == [{"fileData": {"fileUri": "https://example.invalid/cat.png"}}]


def test_input_audio_becomes_inline_data():
    _, contents = to_contents([{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "mp3"}},
    ]}])

    assert contents[0]["parts"] == [{"inlineData": {"mimeType": "audio/mp3", "data": "QUJD"}}]


def test_unsupported_blocks_are_dropped_not_stringified():
    _, contents = to_contents([{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "https://example.invalid/v.mp4"}},
        {"type": "text", "text": "hi"},
    ]}])

    assert contents[0]["parts"] == [{"text": "hi"}]


def test_a_turn_of_only_unsupported_blocks_is_dropped():
    _, contents = to_contents([{"role": "user", "content": [{"type": "video_url"}]}])
    assert contents == []


# --- the tool round-trip ---------------------------------------------------

ASSISTANT_CALL = {
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "call_0",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
    }],
}


def test_assistant_tool_calls_become_function_call_parts():
    _, contents = to_contents([user("weather in Rome?"), ASSISTANT_CALL])

    assert contents[1] == {"role": "model", "parts": [
        {"functionCall": {"name": "get_weather", "args": {"city": "Rome"}}},
    ]}


def test_text_and_tool_calls_coexist_in_one_model_turn():
    _, contents = to_contents([{**ASSISTANT_CALL, "content": "let me check"}])

    assert contents[0]["parts"] == [
        {"text": "let me check"},
        {"functionCall": {"name": "get_weather", "args": {"city": "Rome"}}},
    ]


def test_tool_result_becomes_a_function_response_named_after_the_call():
    """The name comes from the call being answered: Gemini matches by name, not id."""
    _, contents = to_contents([
        user("weather in Rome?"),
        ASSISTANT_CALL,
        {"role": "tool", "tool_call_id": "call_0", "content": '{"temp_c": 31}'},
    ])

    assert contents[2] == {"role": "user", "parts": [
        {"functionResponse": {"name": "get_weather", "response": {"temp_c": 31}}},
    ]}


def test_a_full_round_trip_alternates_user_model_user():
    _, contents = to_contents([
        user("weather in Rome?"),
        ASSISTANT_CALL,
        {"role": "tool", "tool_call_id": "call_0", "content": '{"temp_c": 31}'},
        {"role": "assistant", "content": "It is 31°C."},
        user("and tomorrow?"),
    ])

    assert [c["role"] for c in contents] == ["user", "model", "user", "model", "user"]


def test_parallel_tool_results_merge_into_one_user_turn():
    calls = {
        "role": "assistant",
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "g", "arguments": "{}"}},
        ],
    }
    _, contents = to_contents([
        user("go"),
        calls,
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ])

    assert len(contents) == 3
    assert [p["functionResponse"]["name"] for p in contents[2]["parts"]] == ["f", "g"]


@pytest.mark.parametrize("arguments,expected", [
    ('{"a": 1}', {"a": 1}),
    ("", {}),
    (None, {}),
    ("not json", {}),      # a truncated/garbled tool call must not raise
    ("[1, 2]", {}),        # args is an object upstream: a list is not usable
    ({"a": 1}, {"a": 1}),  # some clients already send it parsed
])
def test_tool_call_arguments_are_parsed_defensively(arguments, expected):
    _, contents = to_contents([{"role": "assistant", "tool_calls": [
        {"id": "x", "function": {"name": "f", "arguments": arguments}},
    ]}])

    assert contents[0]["parts"][0]["functionCall"]["args"] == expected


def test_a_nameless_tool_call_is_skipped():
    _, contents = to_contents([{"role": "assistant", "tool_calls": [{"id": "x", "function": {}}]}])
    assert contents == []


@pytest.mark.parametrize("content,expected", [
    ('{"temp_c": 31}', {"temp_c": 31}),      # JSON object: passed through
    ("31", {"result": 31}),                  # JSON scalar: wrapped
    ("sunny", {"result": "sunny"}),          # plain text: wrapped
    ("[1, 2]", {"result": [1, 2]}),          # JSON array: wrapped
    ({"temp_c": 31}, {"temp_c": 31}),        # already an object
    (None, {"result": None}),
])
def test_tool_results_are_coerced_to_an_object(content, expected):
    """Gemini requires ``functionResponse.response`` to be an object."""
    _, contents = to_contents([
        {"role": "assistant", "tool_calls": [
            {"id": "call_0", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_0", "content": content},
    ])

    assert contents[1]["parts"][0]["functionResponse"]["response"] == expected


def test_tool_result_falls_back_to_the_message_name_when_the_call_is_missing():
    """Some clients trim the assistant turn but still send ``name``."""
    _, contents = to_contents([
        {"role": "tool", "tool_call_id": "gone", "name": "get_weather", "content": "ok"},
    ])

    assert contents[0]["parts"][0]["functionResponse"]["name"] == "get_weather"


def test_tool_result_without_any_resolvable_name_still_produces_a_valid_part():
    _, contents = to_contents([{"role": "tool", "content": "ok"}])

    assert contents[0]["parts"][0]["functionResponse"] == {
        "name": "function", "response": {"result": "ok"},
    }
