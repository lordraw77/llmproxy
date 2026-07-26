"""Unit tests for F5b — OpenAI ``messages`` -> Anthropic ``/v1/messages``.

Before the fix ``_split_system`` copied only ``content``, so ``tool_calls`` were
dropped silently; a ``role="tool"`` message became ``str(content)`` inside a
plain user turn, which the model reads as prose rather than as the answer to its
call; and consecutive same-role turns were emitted as-is.

**These tests pin the shape of the produced body only.** No Anthropic credential
is available, so nothing here has been exercised against the real API — the
residual risk is declared, not retired.
"""

import json

import pytest

from llmproxy.providers.translate.anthropic import split_system


def user(text):
    return {"role": "user", "content": text}


# --- plain text ------------------------------------------------------------

def test_simple_exchange_maps_roles_to_blocks():
    _, messages = split_system([
        user("hi"),
        {"role": "assistant", "content": "hello"},
        user("how are you"),
    ])

    assert messages == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        {"role": "user", "content": [{"type": "text", "text": "how are you"}]},
    ]


def test_system_messages_are_lifted_and_joined():
    system, messages = split_system([
        {"role": "system", "content": "be terse"},
        user("hi"),
        {"role": "system", "content": "and polite"},
    ])

    assert system == "be terse\n\nand polite"
    assert [m["role"] for m in messages] == ["user"]


def test_no_system_message_yields_an_empty_string():
    """``_build_body`` only sets the field when it is truthy."""
    system, _ = split_system([user("hi")])
    assert system == ""


def test_system_message_sent_as_blocks_keeps_its_text():
    system, _ = split_system([
        {"role": "system", "content": [{"type": "text", "text": "be terse"}]},
        user("hi"),
    ])

    assert system == "be terse"


def test_empty_and_missing_content_produces_no_turn():
    """Content must be non-empty upstream: an empty turn is dropped."""
    _, messages = split_system([user(""), {"role": "assistant"}, user("hi")])
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


# --- role alternation ------------------------------------------------------

def test_consecutive_same_role_messages_are_merged():
    _, messages = split_system([user("first"), user("second")])

    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]}]


def test_a_system_message_between_two_user_turns_does_not_split_them():
    """The common case: lifting the system message leaves two adjacent users."""
    _, messages = split_system([
        user("first"),
        {"role": "system", "content": "be terse"},
        user("second"),
    ])

    assert [m["role"] for m in messages] == ["user"]


def test_unknown_roles_are_treated_as_user():
    _, messages = split_system([{"role": "developer", "content": "hi"}])
    assert messages[0]["role"] == "user"


# --- block content ---------------------------------------------------------

def test_text_blocks_are_passed_through():
    _, messages = split_system([{"role": "user", "content": [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": " and this"},
    ]}])

    assert messages[0]["content"] == [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": " and this"},
    ]


def test_block_content_never_leaks_a_python_repr():
    _, messages = split_system([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])

    assert "'type'" not in json.dumps(messages)


def test_data_uri_image_becomes_a_base64_source():
    _, messages = split_system([{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]}])

    assert messages[0]["content"][1] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "QUJD",
    }}


def test_remote_image_url_becomes_a_url_source():
    """Unlike Gemini, Anthropic fetches a plain URL itself."""
    _, messages = split_system([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/cat.png"}},
    ]}])

    assert messages[0]["content"] == [{"type": "image", "source": {
        "type": "url", "url": "https://example.invalid/cat.png",
    }}]


def test_unsupported_blocks_are_dropped_not_stringified():
    _, messages = split_system([{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "QQ=="}},
        {"type": "text", "text": "hi"},
    ]}])

    assert messages[0]["content"] == [{"type": "text", "text": "hi"}]


# --- the tool round-trip ---------------------------------------------------

ASSISTANT_CALL = {
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "toolu_01abc",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
    }],
}


def test_assistant_tool_calls_become_tool_use_blocks():
    _, messages = split_system([user("weather in Rome?"), ASSISTANT_CALL])

    assert messages[1] == {"role": "assistant", "content": [{
        "type": "tool_use",
        "id": "toolu_01abc",
        "name": "get_weather",
        "input": {"city": "Rome"},
    }]}


def test_text_and_tool_use_coexist_in_one_assistant_turn():
    _, messages = split_system([{**ASSISTANT_CALL, "content": "let me check"}])

    assert [b["type"] for b in messages[0]["content"]] == ["text", "tool_use"]


def test_tool_result_is_paired_by_tool_use_id_inside_a_user_turn():
    _, messages = split_system([
        user("weather in Rome?"),
        ASSISTANT_CALL,
        {"role": "tool", "tool_call_id": "toolu_01abc", "content": '{"temp_c": 31}'},
    ])

    assert messages[2] == {"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_01abc",
        "content": '{"temp_c": 31}',
    }]}


def test_a_full_round_trip_alternates_user_assistant_user():
    _, messages = split_system([
        user("weather in Rome?"),
        ASSISTANT_CALL,
        {"role": "tool", "tool_call_id": "toolu_01abc", "content": '{"temp_c": 31}'},
        {"role": "assistant", "content": "It is 31°C."},
        user("and tomorrow?"),
    ])

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]


def test_parallel_tool_results_land_in_a_single_user_turn():
    """Splitting them across turns is what stops the model issuing parallel calls."""
    calls = {
        "role": "assistant",
        "tool_calls": [
            {"id": "toolu_a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "toolu_b", "type": "function", "function": {"name": "g", "arguments": "{}"}},
        ],
    }
    _, messages = split_system([
        user("go"),
        calls,
        {"role": "tool", "tool_call_id": "toolu_a", "content": "1"},
        {"role": "tool", "tool_call_id": "toolu_b", "content": "2"},
    ])

    assert len(messages) == 3
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["toolu_a", "toolu_b"]


def test_an_orphan_tool_result_degrades_to_text():
    """A tool_result with no matching tool_use is rejected upstream."""
    _, messages = split_system([
        {"role": "tool", "tool_call_id": "toolu_gone", "content": "sunny"},
    ])

    assert messages[0]["content"] == [{"type": "text", "text": "Tool result: sunny"}]


def test_a_tool_message_without_an_id_degrades_to_text():
    _, messages = split_system([{"role": "tool", "content": "sunny"}])

    assert messages[0]["content"][0]["type"] == "text"


@pytest.mark.parametrize("arguments,expected", [
    ('{"a": 1}', {"a": 1}),
    ("", {}),
    (None, {}),
    ("not json", {}),      # a truncated/garbled tool call must not raise
    ("[1, 2]", {}),        # input is an object upstream: a list is not usable
    ({"a": 1}, {"a": 1}),  # some clients already send it parsed
])
def test_tool_call_arguments_are_parsed_defensively(arguments, expected):
    _, messages = split_system([{"role": "assistant", "tool_calls": [
        {"id": "toolu_x", "function": {"name": "f", "arguments": arguments}},
    ]}])

    assert messages[0]["content"][0]["input"] == expected


def test_a_nameless_tool_call_is_skipped():
    _, messages = split_system([{"role": "assistant", "tool_calls": [{"id": "x", "function": {}}]}])
    assert messages == []


def test_a_tool_call_without_an_id_still_gets_one():
    """``tool_use.id`` is required, and a later tool_result has to match it."""
    _, messages = split_system([{"role": "assistant", "tool_calls": [
        {"function": {"name": "f", "arguments": "{}"}},
    ]}])

    assert messages[0]["content"][0]["id"]


# --- malformed input -------------------------------------------------------

def test_non_string_non_list_content_is_stringified_as_a_last_resort():
    _, messages = split_system([{"role": "user", "content": 42}])
    assert messages[0]["content"] == [{"type": "text", "text": "42"}]


def test_bare_strings_and_non_dicts_inside_a_block_list():
    _, messages = split_system([{"role": "user", "content": ["hi", "", None, 7]}])
    assert messages[0]["content"] == [{"type": "text", "text": "hi"}]


def test_an_empty_text_block_produces_no_turn():
    _, messages = split_system([{"role": "user", "content": [{"type": "text", "text": ""}]}])
    assert messages == []


def test_a_block_carrying_text_without_a_type_is_still_text():
    _, messages = split_system([{"role": "user", "content": [{"text": "hi"}]}])
    assert messages[0]["content"] == [{"type": "text", "text": "hi"}]


@pytest.mark.parametrize("url", ["", "data:image/png;base64,", "data:,"])
def test_unusable_image_urls_produce_no_block(url):
    _, messages = split_system([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": "hi"},
    ]}])

    assert messages[0]["content"] == [{"type": "text", "text": "hi"}]


def test_a_data_uri_without_a_media_type_falls_back_to_octet_stream():
    _, messages = split_system([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:;base64,QQ=="}},
    ]}])

    assert messages[0]["content"][0]["source"]["media_type"] == "application/octet-stream"


def test_a_tool_result_sent_as_blocks_keeps_its_text():
    _, messages = split_system([
        {"role": "assistant", "tool_calls": [
            {"id": "toolu_1", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": [
            {"type": "text", "text": "sun"}, {"type": "text", "text": "ny"},
        ]},
    ])

    assert messages[1]["content"][0]["content"] == "sunny"
