"""Restored context must not carry tool call ids from a previous session.

Replaying them makes the Realtime server reject the whole context with
invalid_tool_call_id, close the receive loop, and silently kill the turn --
the user asks a question and gets no answer at all.
"""

from app.context_restore import _strip_tool_plumbing


def test_drops_tool_results():
    messages = [
        {"role": "user", "content": "what is the bedroom temperature?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_abc123", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "call_abc123", "content": "21.4"},
        {"role": "assistant", "content": "21.4 degrees."},
    ]
    kept = _strip_tool_plumbing(messages)
    assert kept == [
        {"role": "user", "content": "what is the bedroom temperature?"},
        {"role": "assistant", "content": "21.4 degrees."},
    ]
    assert not any("call_abc123" in str(message) for message in kept)


def test_keeps_assistant_text_that_came_with_a_tool_call():
    messages = [
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [{"id": "call_x", "type": "function"}],
        }
    ]
    assert _strip_tool_plumbing(messages) == [
        {"role": "assistant", "content": "checking"}
    ]


def test_drops_legacy_function_call_shape():
    messages = [{"role": "assistant", "content": None, "function_call": {"name": "f"}}]
    assert _strip_tool_plumbing(messages) == []


def test_leaves_plain_conversation_untouched():
    messages = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert _strip_tool_plumbing(messages) == messages


def test_handles_empty_and_none():
    assert _strip_tool_plumbing(None) is None
    assert _strip_tool_plumbing([]) == []


def test_passes_through_non_dict_items():
    sentinel = object()
    assert _strip_tool_plumbing([sentinel]) == [sentinel]
