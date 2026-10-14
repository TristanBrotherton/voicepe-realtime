"""GPT-Live wire protocol helpers: payloads, delegation ledger, history.

Output audio lives in ``app/live_audio.py`` and is covered by
``test_live_audio.py``; the energy gate this module used to carry was removed
after real captures showed it deleting speech (see that module's docstring).
"""
import unittest

from app.live_protocol import (
    DelegationLedger,
    backend_usage,
    build_session_start,
    chunk_text,
    context_append,
    estimated_tokens,
    function_call_output,
    history_to_session_input,
    input_audio_append,
    input_audio_mute,
    parse_server_event,
    response_create,
    responses_function_tool,
    session_close,
)

TOOL = {"type": "function", "name": "set_timer", "description": "Set a timer",
        "parameters": {"type": "object", "properties": {"seconds": {"type": "integer"}},
                       "required": ["seconds"]}}


class TestClientEvents(unittest.TestCase):
    def test_session_start_matches_the_documented_shape(self):
        evt = build_session_start(
            model="gpt-live-1", instructions="Be brief.", backend_model="gpt-6-luna",
            backend_instructions="Use tools.", tools=[TOOL], voice="marin",
            reasoning_effort="low", service_tier="priority", max_output_tokens=8,
            history=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            event_id="event_start",
        )
        self.assertEqual(evt["type"], "session.start")
        self.assertEqual(evt["event_id"], "event_start")
        session = evt["session"]
        self.assertEqual(session["model"], "gpt-live-1")
        self.assertEqual(session["instructions"], "Be brief.")
        self.assertEqual(session["audio"], {"format": {"type": "audio/pcm", "rate": 24000},
                                            "output": {"voice": "marin"}})
        responses = session["delegation"]["responses"]
        self.assertEqual(session["delegation"]["type"], "responses")
        self.assertEqual(responses["model"], "gpt-6-luna")
        self.assertEqual(responses["instructions"], "Use tools.")
        self.assertEqual(responses["tool_choice"], "auto")
        self.assertFalse(responses["parallel_tool_calls"])
        self.assertEqual(responses["reasoning"], {"effort": "low"})
        self.assertEqual(responses["service_tier"], "priority")
        self.assertEqual(responses["max_output_tokens"], 16)
        self.assertEqual(responses["tools"], [responses_function_tool(TOOL)])
        self.assertEqual(len(session["input"]), 1)

    def test_session_start_omits_optional_fields(self):
        evt = build_session_start(model="m", instructions="i", backend_model="b",
                                  backend_instructions="bi", tools=[])
        session = evt["session"]
        self.assertNotIn("output", session["audio"])
        self.assertNotIn("input", session)
        self.assertNotIn("reasoning", session["delegation"]["responses"])
        self.assertNotIn("service_tier", session["delegation"]["responses"])

    def test_the_audio_format_can_be_omitted_for_a_strict_session_config(self):
        """The session configuration rejects unknown fields.

        A real capture (2026-10-07) shows the endpoint accepting
        ``audio.format`` and echoing it back, so it is sent by default; the
        published references disagree about whether the field exists at all,
        and this is the fallback that cannot be rejected.
        """
        evt = build_session_start(model="m", instructions="i", backend_model="b",
                                  backend_instructions="bi", tools=[],
                                  include_audio_format=False)
        self.assertNotIn("audio", evt["session"])
        with_voice = build_session_start(model="m", instructions="i", backend_model="b",
                                         backend_instructions="bi", tools=[],
                                         voice="marin", include_audio_format=False)
        self.assertEqual(with_voice["session"]["audio"], {"output": {"voice": "marin"}})

    def test_responses_function_tool_keeps_only_function_fields(self):
        tool = dict(TOOL, extra="realtime-only")
        out = responses_function_tool(tool)
        self.assertEqual(set(out), {"type", "name", "description", "parameters"})

    def test_result_and_continue_events(self):
        out = function_call_output("call_1", {"status": "done"}, event_id="r1")
        self.assertEqual(out, {"type": "response.item.create", "event_id": "r1",
                               "item": {"type": "function_call_output", "call_id": "call_1",
                                        "output": '{"status": "done"}'}})
        self.assertEqual(function_call_output("c", "plain")["item"]["output"], "plain")
        self.assertEqual(response_create("c1"), {"type": "response.create", "event_id": "c1"})

    def test_context_append_always_carries_delegation_id(self):
        evt = context_append("thinking", "fact", None, event_id="t1")
        self.assertEqual(evt, {"type": "session.thinking.append", "event_id": "t1",
                               "delegation_id": None, "content": "fact"})
        self.assertEqual(context_append("commentary", "say", "item_1")["delegation_id"], "item_1")
        self.assertEqual(context_append("instructions", "x")["type"], "session.instructions.append")

    def test_audio_mute_and_close(self):
        self.assertEqual(input_audio_append("QUJD"), {"type": "session.input_audio.append", "audio": "QUJD"})
        self.assertEqual(input_audio_mute(True, "m")["type"], "session.input_audio.mute")
        self.assertEqual(input_audio_mute(False, "m")["type"], "session.input_audio.unmute")
        self.assertEqual(session_close("x"), {"type": "session.close", "event_id": "x"})


class TestServerEvents(unittest.TestCase):
    def test_parse_is_lenient(self):
        self.assertEqual(parse_server_event('{"type":"session.started","session":{"id":"s"}}')["type"],
                         "session.started")
        self.assertEqual(parse_server_event(b'{"type":"error"}')["type"], "error")
        self.assertIsNone(parse_server_event("not json"))
        self.assertIsNone(parse_server_event('{"no":"type"}'))
        self.assertIsNone(parse_server_event("[1,2]"))

    def test_backend_usage_from_nested_completion(self):
        evt = {"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.completed", "response": {"usage": {
                "input_tokens": 120, "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 5}}}}}
        self.assertEqual(backend_usage(evt), {"in_text": 120, "cached": 100, "out_text": 30, "reasoning": 5})
        self.assertIsNone(backend_usage({"type": "response.event", "event": {"type": "response.created"}}))


def envelope(inner, delegation_id="item_d1"):
    return {"type": "response.event", "event_id": "e", "delegation_id": delegation_id, "event": inner}


def function_item(call_id="call_1", name="set_timer", arguments='{"seconds": 60}', status="completed"):
    return envelope({"type": "response.output_item.done", "output_index": 0,
                     "item": {"type": "function_call", "status": status, "call_id": call_id,
                              "name": name, "arguments": arguments}})


class TestDelegationLedger(unittest.TestCase):
    def test_collects_completed_function_calls_and_continues_after_results(self):
        ledger = DelegationLedger()
        self.assertEqual(ledger.observe(envelope({"type": "response.created"})), (None, False))
        call, cont = ledger.observe(function_item())
        self.assertEqual((call.call_id, call.name, call.arguments), ("call_1", "set_timer", {"seconds": 60}))
        self.assertFalse(cont)
        self.assertTrue(ledger.busy)
        # The lifecycle snapshot has output: [] — the collected call is what counts.
        _, cont = ledger.observe(envelope({"type": "response.completed",
                                           "response": {"output": [], "usage": {}}}))
        self.assertFalse(cont, "a result is still outstanding")
        self.assertTrue(ledger.complete("call_1"), "all results in → response.create is due")
        self.assertFalse(ledger.busy)

    def test_result_before_completion_waits_for_the_response_to_finish(self):
        ledger = DelegationLedger()
        ledger.observe(envelope({"type": "response.created"}))
        ledger.observe(function_item())
        self.assertFalse(ledger.complete("call_1"), "response has not finished emitting items")
        _, cont = ledger.observe(envelope({"type": "response.completed", "response": {"output": []}}))
        self.assertTrue(cont)

    def test_response_without_calls_never_continues(self):
        ledger = DelegationLedger()
        ledger.observe(envelope({"type": "response.created"}))
        _, cont = ledger.observe(envelope({"type": "response.completed", "response": {"output": []}}))
        self.assertFalse(cont)
        self.assertFalse(ledger.busy)

    def test_arguments_done_alone_and_incomplete_items_are_ignored(self):
        ledger = DelegationLedger()
        self.assertEqual(ledger.observe(envelope({"type": "response.function_call_arguments.done",
                                                  "arguments": "{}"})), (None, False))
        self.assertEqual(ledger.observe(function_item(status="in_progress")), (None, False))
        self.assertEqual(ledger.observe(envelope({"type": "response.output_item.done",
                                                  "item": {"type": "message"}})), (None, False))

    def test_duplicate_call_ids_and_bad_arguments(self):
        ledger = DelegationLedger()
        call, _ = ledger.observe(function_item(arguments="not json"))
        self.assertEqual(call.arguments, {"_invalid_arguments": "not json"})
        self.assertEqual(ledger.observe(function_item(arguments="{}")), (None, False), "already open")
        self.assertFalse(ledger.complete("unknown"))

    def test_uncorrelated_envelopes_share_one_key(self):
        ledger = DelegationLedger()
        ledger.observe(envelope({"type": "response.created"}, delegation_id=None))
        ledger.observe(function_item(), )
        self.assertEqual(ledger.open_calls, 1)
        ledger.reset()
        self.assertEqual(ledger.open_calls, 0)
        self.assertFalse(ledger.busy)

    def test_two_responses_in_one_delegation_do_not_cross_continue(self):
        ledger = DelegationLedger()

        def event(kind, response_id, **extra):
            inner = {"type": kind, **extra}
            if kind in ("response.created", "response.completed"):
                inner["response"] = {"id": response_id}
            else:
                inner["response_id"] = response_id
            return envelope(inner, delegation_id="shared")

        ledger.observe(event("response.created", "r1"))
        call1, _ = ledger.observe(event("response.output_item.done", "r1", item={
            "type": "function_call", "status": "completed", "call_id": "c1",
            "name": "set_timer", "arguments": "{}",
        }))
        ledger.observe(event("response.created", "r2"))
        call2, _ = ledger.observe(event("response.output_item.done", "r2", item={
            "type": "function_call", "status": "completed", "call_id": "c2",
            "name": "set_timer", "arguments": "{}",
        }))
        self.assertEqual((call1.response_id, call2.response_id), ("r1", "r2"))
        ledger.observe(event("response.completed", "r1"))
        self.assertTrue(ledger.complete("c1"))
        self.assertFalse(ledger.complete("c2"), "r2 is not complete yet")
        _, cont = ledger.observe(event("response.completed", "r2"))
        self.assertTrue(cont)


class TestHistorySeeding(unittest.TestCase):
    def test_text_messages_become_input_items_and_tool_traffic_is_skipped(self):
        messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "turn the lamp on"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}], "content": None},
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
            {"role": "assistant", "content": [{"type": "text", "text": "The lamp is on."}]},
            {"role": "user", "content": [{"type": "input_text", "text": "thanks"}]},
            {"role": "assistant", "content": "IN_PROGRESS"},
        ]
        items = history_to_session_input(messages)
        self.assertEqual([i["role"] for i in items], ["user", "assistant", "user"])
        self.assertEqual(items[1]["content"], [{"type": "output_text", "text": "The lamp is on."}])
        self.assertTrue(all(i["type"] == "message" for i in items))

    def test_caps_keep_the_newest(self):
        messages = [{"role": "user", "content": f"m{i}"} for i in range(200)]
        items = history_to_session_input(messages)
        self.assertEqual(len(items), 128)
        self.assertEqual(items[-1]["content"][0]["text"], "m199")
        big = [{"role": "user", "content": "x" * 10000} for _ in range(5)]
        self.assertEqual(len(history_to_session_input(big)), 2)
        self.assertEqual(history_to_session_input(None), [])


class TestChunking(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_text("  hello  "), ["hello"])
        self.assertEqual(chunk_text(""), [])

    def test_long_text_splits_on_sentences_within_budget(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(300))
        chunks = chunk_text(text, token_limit=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(estimated_tokens(c) <= 50 for c in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_a_sentence_longer_than_the_budget_is_split_at_spaces(self):
        text = " ".join(["word"] * 400)
        chunks = chunk_text(text, token_limit=20)
        self.assertTrue(all(estimated_tokens(c) <= 20 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())


if __name__ == "__main__":
    unittest.main()
