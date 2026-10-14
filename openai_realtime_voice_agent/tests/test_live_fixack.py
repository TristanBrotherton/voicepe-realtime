"""candidate-v35 stream fixack: the voice acknowledges, and the fallback clip
counts only audio the device actually heard.

v33 hand-back: while the voice streamed speech the pre-result gate was
holding (a guess at the result) or a phrase that never completed ("Sure,"),
the fallback clip treated those unheard bytes as "the voice is speaking" and
never fired, so a lookup sat in about 5 s of silence. The clip now depends
only on audio sent to the device (or a complete acknowledgement due within
ACK_LAG_S); a complete acknowledgement already heard still suppresses it.

The voice prompt (router mode) tells the voice to acknowledge a request it
cannot answer from context at once, in phrases the acknowledgement grammar
lets through, and never to guess the result.
"""
import asyncio
import re
import unittest
from unittest.mock import patch

import app.live_service as live_service_module
from app.live_router import is_acknowledgement
from app.main import LIVE_ROUTER_POLICY, live_router_instructions

from tests import live_fixtures as fx
from tests.test_live_router import FakeBackend, RouterHarness, b64, call, message, response


class _Fast(unittest.IsolatedAsyncioTestCase):
    ack_lag_s = 0.05

    def setUp(self):
        self._patches = [patch.object(live_service_module, "USER_TURN_GAP_S", 0.05),
                         patch.object(live_service_module, "ACK_LAG_S", self.ack_lag_s),
                         patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 0.2)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    async def _routed(self, h, fired):
        async def clip(device_id):
            fired.append(device_id)
            return True
        h.service.on_fallback_ack = clip
        await h.user_says("What's the weather tomorrow?")
        await asyncio.sleep(0.08)  # the user's turn closes; the route starts

    @staticmethod
    async def _stream(h, seconds, step_ms=100):
        """The voice keeps streaming audio (no transcript) for ``seconds``."""
        for _ in range(int(seconds * 1000 / step_ms)):
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(step_ms))})
            await asyncio.sleep(step_ms / 1000)


class FallbackClipCountsOnlyHeardAudio(_Fast):
    async def test_held_speech_does_not_block_the_clip(self):
        """A guess at the result is held, never heard: the clip still plays."""
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            fired = []
            await self._routed(h, fired)
            h.socket.push({"type": "session.output_transcript.delta",
                           "delta": "Tomorrow will be sunny and warm with"})
            with self.assertLogs("app.live_service", level="WARNING") as logs:
                await self._stream(h, 0.6)
            self.assertEqual(h.heard(), b"", "the held guess was not heard")
            self.assertEqual(fired, ["kitchen"], "held bytes are not audio heard")
            self.assertTrue(any("fallback acknowledgement fired" in m for m in logs.output))
            backend.gate.set()
            await h.wait_commentary()

    async def test_an_acknowledgement_that_never_completes_does_not_block_the_clip(self):
        """"Sure," waits in the lag for words that never come: not heard."""
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            fired = []
            await self._routed(h, fired)
            h.socket.push({"type": "session.output_transcript.delta", "delta": "Sure,"})
            await self._stream(h, 0.6)
            self.assertEqual(h.heard(), b"")
            self.assertEqual(fired, ["kitchen"])
            backend.gate.set()
            await h.wait_commentary()

    async def test_a_heard_acknowledgement_still_suppresses_the_clip(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            fired = []
            await self._routed(h, fired)
            h.voice_says("One moment.", ms=300)
            await asyncio.sleep(0.6)
            self.assertNotEqual(h.heard(), b"", "the acknowledgement was heard")
            self.assertEqual(fired, [], "the voice acknowledged; no clip on top of it")
            backend.gate.set()
            await h.wait_commentary()


class TheVoicesOwnShortAcknowledgementPlays(_Fast):
    """The voice's own acknowledgement plays when it is a closed-grammar
    waiting phrase, with lead-ins only before it and a trailing name only when
    the runtime knows that name. Every other token holds the phrase until the
    verified result (fixack review: a denylist let "Hold on, it worked." and
    "One sec, unlocking the door." play before the result)."""

    HEARD = ("One moment, Jordan.", "Let me check.", "Sure, one sec.",
             "Let me pull that up.", "Checking live.")
    HELD = ("Done.", "It's on.", "Milk and eggs.", "It's 72 degrees",
            "I think the weather tomorrow will be sunny with a light breeze.",
            # fixack review: claims, progressive actions and yes/no answers
            "Hold on, it worked.", "One sec, I did it.", "checking, I did.",
            "One sec, unlocking the door.", "One sec, opening the garage.",
            "One sec, disarming the alarm.", "Let me check, all good.", "Hang on, sent.",
            "One sec, sure.", "Hold on, okay.", "One moment, affirmative.", "Hold on, will do.",
            # free content words, and a name the runtime does not know
            "checking, kitchen.", "Let me look, eggs.", "One sec, noon.", "Hang on, Morgan.")

    def setUp(self):
        super().setUp()
        from app import live_router
        live_router.set_known_names(["Jordan"])
        self.addCleanup(live_router.set_known_names, [])

    def test_the_grammar_accepts_waiting_phrases_and_a_known_name(self):
        for text in self.HEARD + ("Hold on, Jordan.", "Let me pull that up for you."):
            self.assertTrue(is_acknowledgement(text), text)

    def test_every_other_token_holds_the_phrase(self):
        for text in self.HELD:
            self.assertFalse(is_acknowledgement(text), text)

    async def test_one_moment_with_a_known_name_is_heard_and_no_clip_plays(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            fired = []
            await self._routed(h, fired)
            h.voice_says("One moment, Jordan.", ms=300)
            await asyncio.sleep(0.6)
            self.assertNotEqual(h.heard(), b"", "the voice's acknowledgement was heard")
            self.assertEqual(fired, [], "a heard model acknowledgement suppresses the clip")
            backend.gate.set()
            await h.wait_commentary()

    async def _before_result(self, text):
        """Bytes heard before the result, and after it went in."""
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.on_fallback_ack = None
            await h.user_says("What's on the list?")
            await asyncio.sleep(0.08)
            h.voice_says(text, ms=300)
            await asyncio.sleep(0.4)
            before = len(h.heard())
            backend.gate.set()
            await h.wait_commentary()
            await asyncio.sleep(0.1)
            return before, len(h.heard())

    async def test_short_acknowledgements_are_heard_before_the_result(self):
        for text in ("One moment, Jordan.", "Let me check.", "Sure, one sec."):
            before, _ = await self._before_result(text)
            self.assertGreater(before, 0, f"{text!r} was not heard")

    async def test_anything_else_before_the_result_is_never_heard(self):
        for text in self.HELD:
            before, after = await self._before_result(text)
            self.assertEqual(before, 0, f"{text!r} was heard before the result")
            self.assertEqual(after, 0, f"{text!r} was not dropped at the result")


class FallbackClipWaitsForAnImminentAcknowledgement(_Fast):
    ack_lag_s = 0.3

    async def test_a_complete_acknowledgement_in_the_lag_is_given_time_to_play(self):
        """At the deadline "Let me check." is complete but still ACK_LAG_S
        behind: it is heard within that grace, so no clip."""
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            fired = []
            await self._routed(h, fired)
            h.voice_says("Let me check.", ms=300)
            await asyncio.sleep(0.8)
            self.assertNotEqual(h.heard(), b"")
            self.assertEqual(fired, [])
            backend.gate.set()
            await h.wait_commentary()


class TheClipIsTheOnlyAcknowledgement(_Fast):
    """Kitchen, 0.18.33: the clip played, then the voice's own "One moment."
    was released at the result, so two acknowledgements were heard and the
    second was cut at its start. After the clip, the voice's pre-result
    speech is held like a guess and dropped at the result."""

    async def test_the_voices_acknowledgement_after_the_clip_is_not_heard(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Sunny.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            fired = []
            await self._routed(h, fired)
            await asyncio.sleep(0.4)  # silence past the deadline: the clip plays
            self.assertEqual(len(fired), 1)
            h.voice_says("One moment.", ms=300)
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"", "no second acknowledgement before the result")
            backend.gate.set()
            await h.wait_commentary()
            await asyncio.sleep(0.1)
            self.assertEqual(h.heard(), b"", "the held acknowledgement is dropped at the result")


class TheVoiceAcknowledgesOnItsOwn(unittest.TestCase):
    def _bullet(self):
        lines = [line for line in LIVE_ROUTER_POLICY.splitlines() if "acknowledge" in line.lower()]
        self.assertEqual(len(lines), 1, "one acknowledgement instruction")
        return lines[0]

    def test_the_prompt_tells_the_voice_to_acknowledge_at_once_and_not_guess(self):
        prompt = live_router_instructions("Base.", [{"name": "HassTurnOn"}])
        bullet = self._bullet()
        self.assertIn(bullet, prompt)
        self.assertIn("cannot answer from this conversation", bullet)
        self.assertIn("at once", bullet)
        self.assertIn('"One moment."', bullet)
        self.assertIn('"Let me check."', bullet)
        self.assertIn("two to four words", bullet)
        self.assertIn("wait for the result", bullet)
        self.assertIn("Do not guess the result while waiting", bullet)
        for forbidden in ("never say", "Silence is", "do not acknowledge"):
            self.assertNotIn(forbidden, prompt)

    def test_every_example_phrase_passes_the_acknowledgement_grammar(self):
        phrases = re.findall(r'"([^"]+)"', LIVE_ROUTER_POLICY)
        self.assertGreaterEqual(len(phrases), 3)
        for phrase in phrases:
            self.assertTrue(is_acknowledgement(phrase), f"{phrase!r} would be held by the gate")


class TheClipFiresOnSilenceAfterPromotion(_Fast):
    """v35 merge: longtask's promotion (_task_promote) replaced the armed
    progress watch, so fixack's heard-audio-only rule is carried there. A guess
    the voice streams across the promotion is dropped, never heard: the clip
    still plays. The voice speaking the promotion line counts as heard."""

    async def _promoted(self, h, fired):
        from app.live_tasks import TaskPolicy
        h.service.task_policy = TaskPolicy(promote_s=0.3)
        await self._routed(h, fired)

    async def test_dropped_speech_across_the_promotion_does_not_block_the_clip(self):
        backend = FakeBackend(response(message("Sunny.")))
        backend.gate.clear()
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 5.0), \
                patch.object(live_service_module, "PROGRESS_SILENT_FALLBACK_S", 0.3):
            async with RouterHarness(self, backend) as h:
                fired = []
                await self._promoted(h, fired)
                h.socket.push({"type": "session.output_transcript.delta",
                               "delta": "Tomorrow will be sunny and warm with"})
                with self.assertLogs("app.live_service", level="WARNING") as logs:
                    await self._stream(h, 1.0)
                    await asyncio.sleep(0.2)
                self.assertEqual(fired, ["kitchen"], "dropped bytes are not audio heard")
                self.assertTrue(any("for the promotion of" in m for m in logs.output), logs.output)
                backend.gate.set()

    async def test_a_spoken_promotion_line_suppresses_the_clip(self):
        backend = FakeBackend(response(message("Sunny.")))
        backend.gate.clear()
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 5.0), \
                patch.object(live_service_module, "PROGRESS_SILENT_FALLBACK_S", 0.4):
            async with RouterHarness(self, backend) as h:
                fired = []
                await self._promoted(h, fired)
                await asyncio.sleep(0.35)  # promoted; the gate is open
                h.socket.push({"type": "session.commentary.appended"})
                h.voice_says("Still working on it, I'll tell you when I have it.", ms=300)
                await asyncio.sleep(0.6)
                self.assertNotEqual(h.heard(), b"")
                self.assertEqual(fired, [], "the voice said it is still working: no clip")
                backend.gate.set()


if __name__ == "__main__":
    unittest.main()
