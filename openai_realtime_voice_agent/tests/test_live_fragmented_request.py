"""Regressions for a fragmented household request and the false success it bought.

Both reproduce retained 2026-10-11 turns, with no provider, device or Home
Assistant.

**Retained Workshop canary turn c0ffee02-38 (candidate-v26).** The device heard
"Turn off Voice PE Live Test Light." but GPT-Live delivered the input
transcript as two fragments 1.5 s apart ("Turn off", then "PE Live Test
Light"). The second fragment opened a fresh user turn, which reset the
``action`` requirement the first fragment had raised; the model called no
tool and said "Done."; admission played it; the light stayed on.

**Retained Kitchen follow-up turn c0ffee01-23.f2.** 2.4 s of speech, then a
four-character fragment 0.6 s later. The silent-turn rescue judged only the
four characters, found no request, closed the turn without a follow-up, and
the conversation stopped.

Two rules pin the repair:

1. a user transcript fragment that opens within ``USER_FRAGMENT_MERGE_S`` of
   the previous fragment's close, while nothing has answered that fragment, is
   the same request: one utterance sequence, one transcript, one requirement;
2. a complete reply to an action-, clock- or query-shaped request with no tool
   result this turn and no backend work in flight is withheld by name and the
   full request goes through the existing silent-rescue path once.
"""
import asyncio
import unittest
from unittest.mock import patch

from pipecat.frames.frames import (
    TTSAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

import app.live_service as live_service_module
from tests import live_fixtures as fx
from tests.live_harness import LiveHarness


def fragment(text, start_ms, end_ms):
    return {"type": "session.input_transcript.delta", "delta": text,
            "start_ms": start_ms, "end_ms": end_ms}


class FragmentMerge(unittest.IsolatedAsyncioTestCase):
    """Rule 1: a prompt second fragment of an unanswered request is the same request."""

    def setUp(self):
        patcher = patch.object(live_service_module, "USER_TURN_GAP_S", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_non_contiguous_fragment_of_an_unanswered_request_is_merged(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            s = h.service
            s.turn_timeline.begin("t-fragments")
            # 1.5 s between fragments on the session clock: far outside the
            # 250 ms timestamp-continuation window, as the retained turn was.
            h.socket.push(fragment("Turn off", 0, 400))
            await h.sink.wait_for(UserStoppedSpeakingFrame)
            self.assertEqual(s._evidence.requirement, "action")
            with self.assertLogs("app.live_service", level="INFO") as logs:
                h.socket.push(fragment(" the test light", 1900, 2300))
                await h.sink.wait_for(UserStartedSpeakingFrame, count=2)
                await h.source.wait_for(TranscriptionFrame, count=2,
                                        direction=FrameDirection.UPSTREAM)
            self.assertEqual(s.turn_timeline.user_turn_seq, 1,
                             "a fragment of one request is not a second utterance")
            self.assertEqual(h.source.of(TranscriptionFrame)[-1].text,
                             "Turn off the test light")
            self.assertEqual(s._last_user_text, "Turn off the test light")
            self.assertEqual(s._evidence.requirement, "action",
                             "the first fragment's requirement survives the second")
            self.assertTrue(s._requires_backend_for_reply)
            merged = [m for m in logs.output if "merged" in m]
            self.assertEqual(len(merged), 1)
            self.assertNotIn("test light", merged[0], "lengths only, never the words")

    async def test_a_fragment_after_the_request_was_answered_is_a_new_utterance(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            s = h.service
            s.turn_timeline.begin("t-answered")
            h.socket.push(fragment("Turn off the lamp", 0, 400))
            await h.sink.wait_for(UserStoppedSpeakingFrame)
            # Reply audio reached the device: the request has been answered.
            h.socket.push({"type": "session.output_audio.delta",
                           "delta": fx._encode(fx.tone(100))})
            await h.sink.wait_for(TTSAudioRawFrame)
            h.socket.push(fragment("And the fan", 1900, 2300))
            await h.sink.wait_for(UserStartedSpeakingFrame, count=2)
            await h.source.wait_for(TranscriptionFrame, count=2,
                                    direction=FrameDirection.UPSTREAM)
            self.assertEqual(s.turn_timeline.user_turn_seq, 2)
            self.assertEqual(h.source.of(TranscriptionFrame)[-1].text, "And the fan")

    async def test_a_fragment_outside_the_merge_window_is_a_new_utterance(self):
        with patch.object(live_service_module, "USER_FRAGMENT_MERGE_S", 0.05):
            async with LiveHarness(self) as h:
                await h.start_session()
                s = h.service
                s.turn_timeline.begin("t-late")
                h.socket.push(fragment("Turn off", 0, 400))
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                await asyncio.sleep(0.12)
                h.socket.push(fragment("the lamp", 1900, 2300))
                await h.sink.wait_for(UserStartedSpeakingFrame, count=2)
                await h.source.wait_for(TranscriptionFrame, count=2,
                                        direction=FrameDirection.UPSTREAM)
                self.assertEqual(s.turn_timeline.user_turn_seq, 2)
                self.assertEqual(h.source.of(TranscriptionFrame)[-1].text, "the lamp")

    async def test_the_kitchen_follow_up_rescues_the_whole_request(self):
        """c0ffee01-23.f2: the rescue must judge both fragments, not the last four characters."""
        with patch.object(live_service_module, "SILENT_RESPONSE_S", 0.05):
            async with LiveHarness(self) as h:
                await h.start_session()
                s = h.service
                s.turn_timeline.begin("t-followup")
                h.socket.push(fragment("Turn on the hallway lights", 0, 2400))
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                h.socket.push(fragment(" okay", 3000, 3200))
                await h.sink.wait_for(UserStoppedSpeakingFrame, count=2)
                await h.socket.wait_for_sent("response.item.create")
                await h.socket.wait_for_sent("response.create")
                message = h.socket.of("response.item.create")[-1]["item"]
                self.assertEqual(message["content"][0]["text"],
                                 "Turn on the hallway lights okay")
                self.assertEqual(len(h.socket.of("response.create")), 1)


class FalseSuccessWithheld(unittest.IsolatedAsyncioTestCase):
    """Rule 2: "Done." with no tool is never played for an action-shaped request."""

    def setUp(self):
        patcher = patch.object(live_service_module, "USER_TURN_GAP_S", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _request(self, h, text="Turn off the lamp"):
        s = h.service
        s.spoken_prompts = None
        s.turn_timeline.begin("t-false-success")
        h.socket.push(fragment(text, 0, 800))
        await h.sink.wait_for(UserStoppedSpeakingFrame)
        self.assertEqual(s._evidence.requirement, "action")
        return s

    def _model_says_done(self, h):
        # Timestamped inside the 1.3 s of PCM that follows, as the retained
        # admission fixtures are, so alignment never decides these cases.
        h.socket.push({"type": "session.output_transcript.delta", "delta": "Done.",
                       "start_ms": 100, "end_ms": 400})
        h.socket.push_all(fx.output_audio_deltas(fx.tone(400) + fx.silence(900)))

    async def test_done_with_no_tool_is_withheld_and_the_request_is_rescued(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s = await self._request(h)
            with self.assertLogs("app.live_service", level="WARNING") as logs:
                self._model_says_done(h)
                await h.socket.wait_for_sent("response.item.create")
                await h.socket.wait_for_sent("response.create")
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [],
                             "a success claim with no tool never reaches the device")
            message = h.socket.of("response.item.create")[-1]["item"]
            self.assertEqual(message["content"][0]["text"], "Turn off the lamp")
            withheld = [m for m in logs.output if "withheld" in m]
            self.assertEqual(len(withheld), 1)
            self.assertIn("no tool", withheld[0])
            self.assertIn("action", withheld[0])
            self.assertNotIn("Done", withheld[0], "never the words")
            self.assertFalse(s._heard_audio_this_turn)

    async def test_a_second_false_success_ends_the_turn_truthfully(self):
        played = []

        class Prompts:
            enabled = True

            async def say(self, key, device_id):
                played.append((key, device_id))
                return True

        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s = await self._request(h)
            s.spoken_prompts = Prompts()
            silent = []
            s.on_silent_response = lambda: silent.append(1)
            self._model_says_done(h)
            await h.socket.wait_for_sent("response.create")
            # The backend answers the rescue with nothing usable (no tool),
            # and the model claims again.
            h.socket.push(fx.response_created(""))
            h.socket.push(fx.response_completed(""))
            await asyncio.sleep(0.03)
            self._model_says_done(h)
            await asyncio.sleep(0.3)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            self.assertEqual(len(h.socket.of("response.create")), 1,
                             "the rescue is sent once per request")
            self.assertEqual(played, [("error_not_done", "kitchen")])
            self.assertEqual(silent, [1], "the turn ends instead of hanging")

    async def test_a_claim_before_the_transcript_closes_is_rescued_not_refused(self):
        """Workshop canary turn c0ffee02-54 (candidate-v30): the model's
        "Done." landed while the user's transcript turn was still open. The
        request had never been rescued, yet the turn was ended with a refusal
        and the light was switched only by the later silent-turn watch."""
        played = []

        class Prompts:
            enabled = True

            async def say(self, key, device_id):
                played.append(key)
                return True

        with patch.object(live_service_module, "USER_TURN_GAP_S", 5.0):
            async with LiveHarness(self, admit_complete_replies=True) as h:
                await h.start_session()
                s = h.service
                s.spoken_prompts = Prompts()
                s.turn_timeline.begin("t-claim-before-close")
                h.socket.push(fragment("Turn off the lamp", 0, 800))
                await h.sink.wait_for(UserStartedSpeakingFrame)
                await asyncio.sleep(0.05)
                self.assertTrue(s._user_turn.open, "the transcript turn is still open")
                with self.assertLogs("app.live_service", level="WARNING") as logs:
                    self._model_says_done(h)
                    await h.socket.wait_for_sent("response.create")
                self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
                message = h.socket.of("response.item.create")[-1]["item"]
                self.assertEqual(message["content"][0]["text"], "Turn off the lamp",
                                 "the open request itself is rescued")
                self.assertFalse(any("withheld again" in m for m in logs.output))
                self.assertEqual(played, [], "nothing is refused before the request was tried")

    async def test_a_tool_backed_reply_is_still_played(self):
        """Narrowness: the same words after a real tool result are a reply."""
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s = await self._request(h)
            executed = []

            async def handler(params):
                executed.append(dict(params.arguments or {}))
                await params.result_callback({"success": True})

            s.register_function("intent__HassTurnOff", handler)
            h.socket.push_all(fx.function_call_sequence(
                fx.TURN_OFF_CALL_ID, "intent__HassTurnOff", fx.TURN_OFF_ARGUMENTS, "d1"))
            await h.socket.wait_for_sent("response.create")
            self.assertEqual(len(executed), 1)
            h.socket.push(fx.response_created(""))
            h.socket.push(fx.response_completed(""))
            await asyncio.sleep(0.03)
            self._model_says_done(h)
            await h.sink.wait_for(TTSAudioRawFrame, timeout=3)
            self.assertEqual(len(h.socket.of("response.item.create")), 1,
                             "the tool result, not a rescue")


if __name__ == "__main__":
    unittest.main()
