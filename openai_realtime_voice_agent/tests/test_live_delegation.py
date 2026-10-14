"""Delegated Home Assistant actions, replayed from real captured events.

The canary reported two actions that "failed and did not change state". The
protocol itself is not at fault — ``app.live_probe`` showed GPT-Live creating a
delegation on its own initiative from spoken input, emitting ``HassTurnOff``
with the right arguments, accepting our ``response.item.create`` result and
continuing on ``response.create`` — so these tests pin the whole local half of
that path with the captured event shapes (``tests/live_fixtures.py``) and the
production wiring: the shared tool guards, the consequential-action gate and
the ``confirm_action`` handler ``main.py`` builds.

What is asserted end to end, over the socket:

* the handler is invoked once, with the backend's arguments unchanged;
* its result is submitted under the right ``call_id`` and the backend response
  is continued exactly once;
* events that carry a partial call (``response.output_item.added``, which has
  no arguments, and ``response.function_call_arguments.done``, which has no
  name or call id) never dispatch anything;
* an unlock does **not** reach Home Assistant before the user confirms, and
  reaches it exactly once after they do;
* a call with no registered handler is answered rather than left hanging, and
  the lifecycle log says which of those happened.
"""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

import app.live_service as live_service_module
from app.action_gate import ActionGate, EntityDirectory, get_confirm_tool_definition
from app.main import Application

from tests import live_fixtures as fx
from tests.live_harness import LiveHarness

STATES = [
    {"entity_id": "lock.kitchen_door", "attributes": {"friendly_name": "Entry Lock"}},
    {"entity_id": "light.kitchen_main", "attributes": {"friendly_name": "Atrium Lamp"}},
]


async def fetch_states():
    return STATES


def action_gate(**kwargs):
    return ActionGate(directory=EntityDirectory(fetch_states), **kwargs)


class LiveTools(LiveHarness):
    """The harness plus the production tool wiring for one device."""

    async def __aenter__(self):
        await super().__aenter__()
        self.gate = action_gate()
        self.service.action_gate = self.gate
        self.service.spoken_prompts = None
        self.service.turn_timeline.begin("w1")
        # Most fixtures begin after the user's transcript is already known.
        # Individual ordering regressions override this explicitly.
        self.service._user_transcript_seen_for_response = True
        self.executed = []
        self.service.register_function("HassTurnOff", self._record("HassTurnOff"))
        self.service.register_function("GetLiveContext", self._record("GetLiveContext"))
        # Exactly the handler main.py registers, bound to this service.
        self.service.register_function(
            "confirm_action",
            Application._create_confirm_handler(self._app(), self.service),
        )
        await self.start_session()
        return self

    def _app(self):
        app = object.__new__(Application)
        app.action_gate = self.gate
        return app

    def _record(self, name):
        async def handler(params):
            self.executed.append((name, dict(params.arguments or {})))
            await params.result_callback({"status": "done", "tool": name})
        return handler

    def results(self):
        return [m["item"] for m in self.socket.of("response.item.create")]

    def speak_confirmation_prompt(self, confirm_id: str) -> None:
        self.service._input_audio_ms_sent = max(
            self.service._input_audio_ms_sent, 1000.0
        )
        self.gate.fence_after_prompt(
            confirm_id, self.service.turn_timeline.user_turn_seq,
            self.service._input_audio_ms_sent,
        )

    def new_user_turn(self, text: str = "yes") -> None:
        """What a fresh utterance does to the confirmation bookkeeping."""
        self.service.turn_timeline.note_speech_stopped(None, None, None)
        self.service._input_audio_ms_sent += 500.0
        self.service._last_user_text = text
        self.service._last_user_text_seq = self.service.turn_timeline.user_turn_seq
        self.service._last_user_start_ms = self.service._input_audio_ms_sent


class TestDelegatedCall(unittest.IsolatedAsyncioTestCase):
    async def test_a_captured_hass_call_runs_once_and_is_answered(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            self.assertEqual(h.executed, [(
                "HassTurnOff",
                {"name": "kitchen lights", "area": "kitchen", "domain": ["light"]},
            )], "the backend's arguments must reach the handler unchanged")
            items = h.results()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["type"], "function_call_output")
            self.assertEqual(items[0]["call_id"], fx.TURN_OFF_CALL_ID)
            self.assertEqual(json.loads(items[0]["output"]),
                             {"status": "done", "tool": "HassTurnOff"})
            self.assertEqual(h.socket.types().count("response.create"), 1,
                             "the response is continued exactly once")

    async def test_arguments_survive_the_streamed_fragments(self):
        """Arguments are rebuilt by the backend, not by us: use the done item."""
        async with LiveTools(self) as h:
            events = fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS)
            partial = [e for e in events
                       if (e.get("event") or {}).get("type") in (
                           "response.output_item.added",
                           "response.function_call_arguments.delta",
                           "response.function_call_arguments.done")]
            self.assertGreater(len(partial), 3, "the capture streams the arguments")
            h.socket.push_all(partial)
            await asyncio.sleep(0.1)
            self.assertEqual(h.executed, [],
                             "a partial call must never dispatch: added has no "
                             "arguments and arguments.done has no name or call_id")
            self.assertEqual(h.results(), [])
            self.assertEqual(h.service.calls.observed, 0)

    async def test_the_lifecycle_is_recorded(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            await asyncio.sleep(0.05)
            snapshot = h.service.calls.snapshot()
            self.assertEqual(snapshot["observed"], 1)
            self.assertEqual(snapshot["dispatched"], 1)
            self.assertEqual(snapshot["unregistered"], 0)
            self.assertEqual(snapshot["submitted"], 1)
            self.assertEqual(snapshot["continued"], 1)
            self.assertEqual(snapshot["abandoned"], 0)
            self.assertEqual(h.service.calls.unanswered(), [])
            line = snapshot["recent"][0]
            self.assertIn("HassTurnOff", line)
            self.assertIn("result submitted", line)
            self.assertNotIn("kitchen", line, "argument values are not recorded")
            self.assertIn("area,domain,name", line, "argument keys are")

    async def test_backend_usage_is_accumulated(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            self.assertEqual(h.service.backend_tokens["in_text"], 851)
            self.assertEqual(h.service.backend_tokens["out_text"], 38)

    async def test_a_call_with_no_handler_is_answered_not_left_hanging(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                "call_missing", "HassStartTeleport", "{}"))
            await h.socket.wait_for_sent("response.item.create")
            items = h.results()
            self.assertEqual(items[0]["call_id"], "call_missing")
            self.assertIn("not available", json.loads(items[0]["output"])["error"])
            self.assertEqual(h.service.calls.snapshot()["unregistered"], 1)
            self.assertFalse(h.service.ledger.busy,
                             "the delegation must not stay open forever")

    async def test_a_result_that_arrives_before_the_response_finishes_continues_later(self):
        async with LiveTools(self) as h:
            h.socket.push(fx.delegation_created())
            h.socket.push(fx.response_created())
            h.socket.push(fx.function_call_done(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.item.create")
            self.assertEqual(h.socket.of("response.create"), [],
                             "the backend response has not finished emitting items")
            h.socket.push(fx.response_completed())
            await h.socket.wait_for_sent("response.create")
            self.assertEqual(h.socket.types().count("response.create"), 1)
            await asyncio.sleep(0.05)
            self.assertEqual(h.service.calls.snapshot()["continued"], 1)

    async def test_a_failed_result_send_does_not_settle_or_continue_the_call(self):
        async with LiveTools(self) as h:
            original_send = h.socket.send

            async def fail_result(data):
                if json.loads(data)["type"] == "response.item.create":
                    raise ConnectionError("fixture socket dropped")
                await original_send(data)

            h.socket.send = fail_result
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            await asyncio.sleep(0.05)
            snapshot = h.service.calls.snapshot()
            self.assertEqual(snapshot["submitted"], 0)
            self.assertEqual(snapshot["continued"], 0)
            self.assertTrue(h.service.ledger.busy,
                            "a result that never reached the server remains unsettled")
            self.assertEqual(h.socket.of("response.create"), [])

    async def test_a_server_rejected_result_is_not_reported_as_submitted(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.item.create")
            event_id = h.socket.of("response.item.create")[0]["event_id"]
            self.assertEqual(h.service.calls.snapshot()["submitted"], 1)
            h.socket.push({"type": "error", "error": {
                "type": "invalid_request_error", "code": "bad_result",
                "message": "result rejected", "client_event_id": event_id,
            }})
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            snapshot = h.service.calls.snapshot()
            self.assertEqual(snapshot["submitted"], 0)
            self.assertEqual(snapshot["continued"], 0)
            self.assertIn("rejected", snapshot["recent"][-1])

    async def test_a_server_rejected_immediate_continuation_stays_unresolved(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            event_id = h.socket.of("response.create")[-1]["event_id"]
            h.socket.push({"type": "error", "error": {
                "type": "invalid_request_error", "code": "bad_continue",
                "message": "continuation rejected", "client_event_id": event_id,
            }})
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            self.assertEqual(h.service.calls.snapshot()["continued"], 0)
            self.assertTrue(h.service.response_active)

    async def test_a_server_rejected_delayed_continuation_stays_unresolved(self):
        async with LiveTools(self) as h:
            h.socket.push(fx.delegation_created())
            h.socket.push(fx.response_created())
            h.socket.push(fx.function_call_done(
                fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
            await h.socket.wait_for_sent("response.item.create")
            h.socket.push(fx.response_completed())
            await h.socket.wait_for_sent("response.create")
            event_id = h.socket.of("response.create")[-1]["event_id"]
            h.socket.push({"type": "error", "error": {
                "type": "invalid_request_error", "code": "bad_continue",
                "message": "continuation rejected", "client_event_id": event_id,
            }})
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            self.assertEqual(h.service.calls.snapshot()["continued"], 0)
            self.assertTrue(h.service.response_active)

    async def test_an_unanswered_call_is_named_at_disconnect(self):
        async with LiveTools(self) as h:
            blocked = asyncio.Event()

            async def never_returns(params):
                await blocked.wait()

            h.service.register_function("GetLiveContext", never_returns)
            h.socket.push_all(fx.function_call_sequence(
                "call_stuck", "GetLiveContext", "{}"))
            await asyncio.sleep(0.1)
            self.assertEqual(h.service.calls.unanswered()[0].call_id, "call_stuck")
            await h.service._disconnect()
            record = h.service.calls.records[-1]
            self.assertEqual(record.outcome, "abandoned at disconnect")
            self.assertFalse(record.submitted)
            blocked.set()


class TestConfirmationGate(unittest.IsolatedAsyncioTestCase):
    """An unlock must not reach Home Assistant before an explicit yes."""

    async def test_an_unlock_is_held_and_nothing_happens_until_confirmed(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            self.assertEqual(h.executed, [], "the lock was never touched")
            held = json.loads(h.results()[0]["output"])
            self.assertEqual(held["status"], "confirmation_required")
            self.assertIn("has NOT been done", held["instructions"])
            confirm_id = held["confirm_id"]
            self.assertIn("unlock entry lock", held["action"].lower())

            # The model asks; the user has not answered yet. A confirm_action
            # in the SAME turn must be refused.
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm_early", "confirm_action",
                json.dumps({"confirm_id": confirm_id}),
                delegation_id="item_early"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            early = json.loads(h.results()[1]["output"])
            self.assertIn("not been spoken", early["error"])
            self.assertEqual(h.executed, [], "still untouched")

            # The user speaks again, then the backend confirms.
            h.speak_confirmation_prompt(confirm_id)
            h.new_user_turn()
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm", "confirm_action",
                json.dumps({"confirm_id": confirm_id}),
                delegation_id="item_confirm"))
            await h.socket.wait_for_sent("response.item.create", count=3)
            self.assertEqual(h.executed, [(
                "HassTurnOff", {"name": "entry lock", "domain": ["lock"]},
            )], "the held arguments are replayed exactly once, after the yes")
            final = h.results()[2]
            self.assertEqual(final["call_id"], "call_confirm")
            self.assertEqual(json.loads(final["output"]),
                             {"status": "done", "tool": "HassTurnOff"})

    async def test_confirmation_prompt_arms_inside_an_existing_audio_segment(self):
        async with LiveTools(self) as h:
            audio = base64.b64encode(fx.tone(100)).decode("ascii")
            h.socket.push({"type": "session.output_audio.delta", "delta": audio})
            await h.sink.wait_for(TTSAudioRawFrame)

            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]

            # The question continues the acknowledgement's audio segment, so
            # started_segment is false for this accepted post-hold chunk.
            h.socket.push({"type": "session.output_audio.delta", "delta": audio})
            await h.sink.wait_for(TTSAudioRawFrame, count=2)
            self.assertIsNotNone(h.gate._pending[confirm_id].armed_user_turn_seq)

            h.socket.push({"type": "session.input_transcript.delta", "delta": "yes",
                           "start_ms": 100, "end_ms": 200})
            await asyncio.sleep(0.05)
            await h.service._end_turn("user")
            h.socket.push_all(fx.function_call_sequence(
                "call_yes_after_continuous_prompt", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_continuous"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertEqual(len(h.executed), 1)

    async def test_streamed_yes_prefix_cannot_confirm_before_later_negation(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]
            h.speak_confirmation_prompt(confirm_id)

            h.socket.push({"type": "session.input_transcript.delta", "delta": "yes",
                           "start_ms": 1500, "end_ms": 1550})
            await h.sink.wait_for(UserStartedSpeakingFrame)
            h.socket.push_all(fx.function_call_sequence(
                "call_prefix", "confirm_action", json.dumps({"confirm_id": confirm_id}),
                delegation_id="item_prefix"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertIn("still being transcribed",
                          json.loads(h.results()[1]["output"])["error"])
            self.assertEqual(h.executed, [])

            h.socket.push({"type": "session.input_transcript.delta", "delta": " don't",
                           "start_ms": 1550, "end_ms": 1700})
            await asyncio.sleep(0.05)
            await h.service._end_turn("user")
            h.socket.push_all(fx.function_call_sequence(
                "call_settled_negative", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_negative"))
            await h.socket.wait_for_sent("response.item.create", count=3)
            self.assertIn("not an explicit affirmative",
                          json.loads(h.results()[2]["output"])["error"])
            self.assertEqual(h.executed, [])

    async def test_settled_streamed_yes_confirms_once(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]
            h.speak_confirmation_prompt(confirm_id)

            h.socket.push({"type": "session.input_transcript.delta", "delta": "yes",
                           "start_ms": 1500, "end_ms": 1600})
            await h.sink.wait_for(UserStartedSpeakingFrame)
            await h.service._end_turn("user")
            h.socket.push_all(fx.function_call_sequence(
                "call_settled_yes", "confirm_action", json.dumps({"confirm_id": confirm_id}),
                delegation_id="item_settled_yes"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertEqual(len(h.executed), 1)

    async def test_post_hold_silence_does_not_arm_confirmation(self):
        async with LiveTools(self) as h:
            tone = base64.b64encode(fx.tone(100)).decode("ascii")
            silence = base64.b64encode(b"\x00\x00" * 2400).decode("ascii")
            h.socket.push({"type": "session.output_audio.delta", "delta": tone})
            await h.sink.wait_for(TTSAudioRawFrame)

            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]

            h.socket.push({"type": "session.output_audio.delta", "delta": silence})
            await h.sink.wait_for(TTSAudioRawFrame, count=2)
            self.assertIsNone(h.gate._pending[confirm_id].armed_user_turn_seq)

            h.socket.push({"type": "session.input_transcript.delta", "delta": "yes",
                           "start_ms": 100, "end_ms": 200})
            await asyncio.sleep(0.05)
            await h.service._end_turn("user")
            h.socket.push_all(fx.function_call_sequence(
                "call_yes_after_silence", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_silence"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertEqual(h.executed, [])
            self.assertIn("not been spoken", json.loads(h.results()[1]["output"])["error"])

    async def test_a_delayed_original_transcript_cannot_count_as_confirmation(self):
        async with LiveTools(self) as h:
            h.service._user_transcript_seen_for_response = False
            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]

            # Legal protocol ordering: delegation first, then the transcript
            # for the utterance which requested the unlock.
            h.new_user_turn("unlock the entry lock")
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm_too_early", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_late_input"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertIn("not been spoken", json.loads(h.results()[1]["output"])["error"])
            self.assertEqual(h.executed, [])

            h.speak_confirmation_prompt(confirm_id)
            h.new_user_turn("yes")
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm_after_yes", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_actual_yes"))
            await h.socket.wait_for_sent("response.item.create", count=3)
            self.assertEqual(len(h.executed), 1)

    async def test_second_request_pretranscript_delegation_cannot_self_confirm(self):
        async with LiveTools(self) as h:
            # A prior utterance makes the old lifetime boolean true.
            h.service.turn_timeline.user_turn_seq = 1
            h.service._user_transcript_seen_for_response = True
            h.service._last_user_text = "hello"
            h.service._last_user_text_seq = 1

            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]

            # The original unlock transcript arrives after its delegation.
            h.new_user_turn("unlock the entry lock")
            h.socket.push_all(fx.function_call_sequence(
                "call_self_confirm", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_self"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertEqual(h.executed, [])
            self.assertIn("not been spoken", json.loads(h.results()[1]["output"])["error"])

            h.speak_confirmation_prompt(confirm_id)
            h.new_user_turn("unlock the entry lock")
            h.socket.push_all(fx.function_call_sequence(
                "call_non_yes", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_non_yes"))
            await h.socket.wait_for_sent("response.item.create", count=3)
            self.assertEqual(h.executed, [])
            self.assertIn("not an explicit affirmative",
                          json.loads(h.results()[2]["output"])["error"])

            h.new_user_turn("yes")
            h.socket.push_all(fx.function_call_sequence(
                "call_real_yes", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_real_yes"))
            await h.socket.wait_for_sent("response.item.create", count=4)
            self.assertEqual(len(h.executed), 1)

    async def test_assistant_transcript_ending_before_hold_does_not_add_a_fence(self):
        async with LiveTools(self) as h:
            await h.service._open_turn("user")
            current = h.service.turn_timeline.user_turn_seq
            h.service._assistant_turn.open = True
            h.service._assistant_turn.text = "One moment."
            await h.service._end_turn("assistant")
            self.assertEqual(h.service.gate_request_context()[1], current)

    async def test_a_replayed_confirmation_cannot_unlock_twice(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.item.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]
            h.speak_confirmation_prompt(confirm_id)
            h.new_user_turn()
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm", "confirm_action", json.dumps({"confirm_id": confirm_id}),
                delegation_id="item_confirm"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertEqual(len(h.executed), 1)
            h.new_user_turn()
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm_again", "confirm_action",
                json.dumps({"confirm_id": confirm_id}), delegation_id="item_again"))
            await h.socket.wait_for_sent("response.item.create", count=3)
            self.assertEqual(len(h.executed), 1, "a confirmation is single use")
            self.assertIn("no such pending action",
                          json.loads(h.results()[2]["output"])["error"])

    async def test_an_unknown_confirm_id_is_refused(self):
        async with LiveTools(self) as h:
            h.new_user_turn()
            h.socket.push_all(fx.function_call_sequence(
                "call_bogus", "confirm_action", json.dumps({"confirm_id": "deadbe"})))
            await h.socket.wait_for_sent("response.item.create")
            self.assertEqual(h.executed, [])
            self.assertIn("no such pending action",
                          json.loads(h.results()[0]["output"])["error"])

    async def test_a_new_wake_cancels_a_pending_unlock(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.UNLOCK_CALL_ID, "HassTurnOff", fx.UNLOCK_ARGUMENTS))
            await h.socket.wait_for_sent("response.item.create")
            confirm_id = json.loads(h.results()[0]["output"])["confirm_id"]
            h.service.turn_timeline.begin("w2")  # a new conversation
            h.new_user_turn()
            h.socket.push_all(fx.function_call_sequence(
                "call_confirm", "confirm_action", json.dumps({"confirm_id": confirm_id}),
                delegation_id="item_confirm"))
            await h.socket.wait_for_sent("response.item.create", count=2)
            self.assertEqual(h.executed, [])
            self.assertIn("new conversation",
                          json.loads(h.results()[1]["output"])["error"])

    async def test_an_ordinary_light_is_not_gated(self):
        async with LiveTools(self) as h:
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff",
                json.dumps({"name": "Atrium Lamp", "domain": ["light"]})))
            await h.socket.wait_for_sent("response.create")
            self.assertEqual(len(h.executed), 1)
            self.assertEqual(json.loads(h.results()[0]["output"])["status"], "done")


class TestSpeakerGate(unittest.IsolatedAsyncioTestCase):
    async def test_a_speaker_gated_tool_fails_closed_below_the_model(self):
        from app.speaker_context import SpeakerProbe

        async with LiveTools(self) as h:
            h.service.speaker_probe = SpeakerProbe("alex", "sam")
            h.service.male_only_tools = {"HassTurnOff"}
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "HassTurnOff",
                json.dumps({"name": "Atrium Lamp", "domain": ["light"]})))
            await h.socket.wait_for_sent("response.item.create")
            self.assertEqual(h.executed, [])
            self.assertIn("error", json.loads(h.results()[0]["output"]))


class TestDelegationLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_backend_work_holds_the_thinking_watchdog_open(self):
        async with LiveTools(self) as h:
            release = asyncio.Event()

            async def slow(params):
                await release.wait()
                await params.result_callback({"status": "ok"})

            h.service.register_function("GetLiveContext", slow)
            h.socket.push_all(fx.function_call_sequence(
                "call_slow", "GetLiveContext", "{}"))
            await asyncio.sleep(0.1)
            # Two holds: the delegation itself (bounded by DELEGATION_TIMEOUT_S)
            # and the tool call inside it.
            self.assertEqual(h.service.turn_liveness.in_flight, 2)
            self.assertTrue(h.service.response_active)
            release.set()
            await h.socket.wait_for_sent("response.create")
            await asyncio.sleep(0.05)
            self.assertEqual(h.service.turn_liveness.in_flight, 0,
                             "the delegation hold is released with the last result, "
                             "not DELEGATION_TIMEOUT_S later")
            self.assertFalse(h.service.response_active)

    async def test_a_failed_delegated_response_surfaces_an_error_frame(self):
        from pipecat.frames.frames import ErrorFrame
        from pipecat.processors.frame_processor import FrameDirection

        async with LiveTools(self) as h:
            h.socket.push(fx.delegation_created())
            h.socket.push(fx.envelope({
                "type": "response.failed",
                "response": {"status": "failed", "error": {"message": "rate limited"}},
            }))
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            self.assertIn("rate limited", h.source.of(ErrorFrame)[0].error)
            await asyncio.sleep(0.02)
            self.assertEqual(h.service.turn_liveness.in_flight, 0)

    async def test_a_client_targeted_delegation_is_declined_explicitly(self):
        async with LiveTools(self) as h:
            h.socket.push(fx.delegation_created("item_client", target="client"))
            await h.socket.wait_for_sent("session.commentary.append")
            event = h.socket.of("session.commentary.append")[0]
            self.assertEqual(event["delegation_id"], "item_client")
            self.assertIn("No backend", event["content"])

    async def test_the_tool_definitions_sent_to_the_backend_keep_only_wire_fields(self):
        tools = [{"type": "function", "name": "HassTurnOff", "description": "Turns off.",
                  "parameters": {"type": "object", "properties": {}},
                  "x_internal": "must not be sent"}]
        async with LiveTools(self, tools=tools) as h:
            responses = h.socket.sent[0]["session"]["delegation"]["responses"]
            self.assertEqual(responses["tools"], [{
                "type": "function", "name": "HassTurnOff", "description": "Turns off.",
                "parameters": {"type": "object", "properties": {}},
            }])
            self.assertEqual(responses["tool_choice"], "auto")
            self.assertIs(responses["parallel_tool_calls"], False)

    async def test_every_reply_logs_its_audio_and_call_evidence(self):
        """The canary's logs were lost to a restart; this lands per reply."""
        import logging

        with self.assertLogs("app.live_service", level="INFO") as captured:
            async with LiveTools(self) as h:
                h.socket.push_all(fx.function_call_sequence(
                    fx.TURN_OFF_CALL_ID, "HassTurnOff", fx.TURN_OFF_ARGUMENTS))
                await h.socket.wait_for_sent("response.create")
                h.socket.push({"type": "session.output_audio.delta",
                               "delta": fx._encode(fx.tone(100))})
                await asyncio.sleep(0.05)
                with patch.object(live_service_module, "ASSISTANT_TURN_GAP_S", 0.05):
                    h.socket.push({"type": "session.output_transcript.delta",
                                   "delta": "Done.", "start_ms": 0, "end_ms": 200})
                    await asyncio.sleep(0.3)
        audio_lines = [m for m in captured.output if "live output audio [reply end" in m]
        self.assertEqual(len(audio_lines), 1, captured.output)
        line = audio_lines[0]
        for needle in ("deltas forwarded", "peak ", "odd-length 0", "undecodable 0",
                       "untimed 1", "sha256 ", "x real time"):
            self.assertIn(needle, line)
        self.assertNotIn("Done.", line, "no transcript in the diagnostics line")
        call_lines = [m for m in captured.output if "live function calls" in m]
        self.assertEqual(len(call_lines), 1)
        self.assertIn("'submitted': 1", call_lines[0])
        self.assertIn("HassTurnOff", call_lines[0])
        self.assertNotIn("kitchen lights", call_lines[0], "no argument values")
        self.assertEqual(logging.getLogger("app.live_service").level, logging.NOTSET)

    async def test_the_confirm_tool_is_shaped_for_the_responses_backend(self):
        definition = get_confirm_tool_definition()
        self.assertEqual(definition["name"], "confirm_action")
        self.assertIn("confirm_id", definition["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
