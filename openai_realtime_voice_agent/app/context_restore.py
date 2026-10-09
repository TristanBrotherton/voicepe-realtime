"""Helpers for reusing a conversation across Realtime sessions."""


def _strip_tool_plumbing(messages):
    """Drop tool calls and tool results from a restored conversation.

    Tool call IDs belong to the OpenAI Realtime *conversation* that produced
    them. Replaying them into a fresh session makes the server reject the
    whole context with:

        invalid_request_error / invalid_tool_call_id
        "Tool call ID 'call_...' not found in conversation."

    The receive loop then closes and the turn dies silently — the user asks a
    question and simply gets no answer. Trimming to the last N messages makes
    it worse, because it can also orphan a tool result from its call.

    What the user actually wants back is what was *said*, not the plumbing, so
    keep the plain turns and drop the rest.
    """
    if not messages:
        return messages
    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        # A tool result, or anything still pointing at a previous call id.
        if message.get("role") == "tool" or message.get("tool_call_id"):
            continue
        if message.get("tool_calls") or message.get("function_call"):
            # Keep whatever the assistant said out loud, drop the call itself.
            message = {
                key: value
                for key, value in message.items()
                if key not in ("tool_calls", "function_call")
            }
            if not message.get("content"):
                continue
        cleaned.append(message)
    return cleaned
