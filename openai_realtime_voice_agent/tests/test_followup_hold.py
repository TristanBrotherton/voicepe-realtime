"""The follow-up window stays open while the user is still talking.

Under GPT-Live the add-on's only speech signal is the input transcript, and it
arrives when the utterance ENDS (lab, 140 turns: the device's ``listening``
landed 2.4-3.3 s after the wake for phrases spoken over 1.8-3.1 s). The
device's follow-up window closes on a fixed timer that only a ``listening``
phase cancels, so a follow-up begun late in the window was cut mid-sentence,
flushed, and the cut-off closed the paid session before its transcript came.

``app/followup_hold.py`` hears the mic itself: an energy onset in the window
sends ``listening`` early and keeps the session under its idle bounds, bounded
by a release so noise cannot keep a session billed. Four layers are pinned
here: the pure detector, the coordinator, the phase emitter and gate hooks,
and the production wiring through ``WebSocketHandler.build_pipeline``.
"""
import asyncio
import math
import struct
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.processors.frame_processor import FrameProcessor

import app.live_session_gate as gate_module
from app.followup_hold import (
    BLOCK_MS,
    MIN_RMS,
    FollowUpHold,
    SpeechOnsetDetector,
)
from app.live_session_gate import CLOSED, OPEN, LiveSessionPolicy
from app.phase_emitter import PhaseEmitter

from tests.test_live_gate_wiring import GateWiringCase
from tests.test_live_session_gate import GateCase


# ---------------------------------------------------------------------------
# synthetic microphone audio (16 kHz PCM16 mono)
# ---------------------------------------------------------------------------

def pcm(ms: int, amplitude: int, hz: float = 220.0, sample_rate: int = 16000) -> bytes:
    """A tone of the given peak amplitude (RMS ~ amplitude / sqrt 2)."""
    n = sample_rate * ms // 1000
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * hz * i / sample_rate))
                                    for i in range(n)))


SILENCE = pcm(1000, 0)
ROOM = pcm(1000, 400)         # RMS ~283: a quiet room on the Voice PE's mic
SPEECH = pcm(400, 2500)       # RMS ~1770: someone talking near the device
HUM = pcm(3000, 3000)         # RMS ~2121: a loud fan, above the absolute floor


class TestSpeechOnsetDetector(unittest.TestCase):
    def test_silence_and_a_quiet_room_never_fire(self):
        detector = SpeechOnsetDetector()
        self.assertFalse(detector.feed(SILENCE))
        self.assertFalse(detector.feed(ROOM))
        self.assertFalse(detector.fired)

    def test_speech_fires_once_after_the_onset_time(self):
        detector = SpeechOnsetDetector()
        detector.observe(ROOM)
        self.assertFalse(detector.feed(pcm(100, 2500)), "100 ms is a click, not speech")
        detector.reset()
        self.assertTrue(detector.feed(SPEECH))
        self.assertTrue(detector.fired)
        self.assertLessEqual(detector.fed_ms, 400)
        self.assertFalse(detector.feed(SPEECH), "fires once per arming")
        detector.reset()
        self.assertTrue(detector.feed(SPEECH), "armed again after a reset")

    def test_frames_of_any_size_add_up(self):
        detector = SpeechOnsetDetector()
        fired = False
        for i in range(0, len(SPEECH), 333):  # not a multiple of a block
            fired = detector.feed(SPEECH[i:i + 333]) or fired
        self.assertTrue(fired)

    def test_a_short_dip_inside_a_run_is_tolerated_a_long_one_is_not(self):
        detector = SpeechOnsetDetector()
        burst = pcm(160, 2500)
        self.assertFalse(detector.feed(burst))
        self.assertFalse(detector.feed(pcm(BLOCK_MS, 0)))
        self.assertTrue(detector.feed(burst), "a 20 ms stop consonant does not reset the run")
        detector.reset()
        self.assertFalse(detector.feed(burst))
        self.assertFalse(detector.feed(pcm(BLOCK_MS * 3, 0)))
        self.assertFalse(detector.feed(pcm(160, 2500)), "a 60 ms gap starts a new run")

    def test_a_steady_hum_becomes_the_floor_and_speech_must_rise_above_it(self):
        detector = SpeechOnsetDetector()
        detector.observe(HUM)  # the fan was there during the previous turn
        self.assertGreater(detector.noise, MIN_RMS)
        detector.reset()
        self.assertFalse(detector.feed(HUM), "the hum alone is not speech")
        self.assertFalse(detector.feed(pcm(400, 8000)), "not six times the hum")
        self.assertTrue(detector.feed(pcm(400, 20000)))

    def test_speech_with_a_known_floor_is_judged_against_that_floor(self):
        detector = SpeechOnsetDetector()
        detector.observe(ROOM)
        self.assertGreater(detector.threshold, MIN_RMS, "six times the room, not the absolute floor")
        self.assertFalse(detector.feed(pcm(400, 2200)), "RMS ~1556 is above the floor but not above the room")
        self.assertTrue(detector.feed(pcm(400, 2600)), "RMS ~1838 is")


# ---------------------------------------------------------------------------
# the coordinator
# ---------------------------------------------------------------------------

class HoldCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.listening = 0
        self.idles = 0
        self.onsets = 0
        self.releases = 0
        self.listening_ok = True

        async def hold_listening():
            self.listening += 1
            return self.listening_ok

        async def release_idle():
            self.idles += 1

        self.hold = FollowUpHold(
            hold_listening=hold_listening, release_idle=release_idle,
            may_hold=lambda: self.may_hold,
            on_onset=lambda: setattr(self, "onsets", self.onsets + 1),
            on_release=lambda: setattr(self, "releases", self.releases + 1),
            hold_max_s=0.1,
        )
        self.may_hold = True

    async def asyncTearDown(self):
        await self.hold.close()

    def after_reply(self):
        self.hold.note_phase("replying", {})
        self.hold.note_phase("idle", {})

    async def speak(self):
        self.hold.feed(SPEECH)
        await asyncio.sleep(0.01)


class TestArming(HoldCase):
    async def test_the_idle_after_a_reply_arms_the_hold(self):
        self.after_reply()
        self.assertTrue(self.hold.armed)

    async def test_an_idle_without_a_reply_does_not_arm(self):
        self.hold.note_phase("idle", {})  # the connect-time idle
        self.assertFalse(self.hold.armed)

    async def test_a_silent_turn_opens_no_window_so_nothing_is_armed(self):
        self.hold.note_phase("replying", {})
        self.hold.note_phase("idle", {"followup": False})
        self.assertFalse(self.hold.armed)

    async def test_speech_outside_a_window_only_teaches_the_floor(self):
        await self.speak()
        self.assertFalse(self.hold.held)
        self.assertEqual(self.listening, 0)
        self.assertGreater(self.hold.detector.fed_ms, 0)

    async def test_a_turn_phase_disarms(self):
        self.after_reply()
        self.hold.note_phase("thinking", {})
        self.assertFalse(self.hold.armed)


class TestHolding(HoldCase):
    async def test_speech_in_the_window_sends_listening_once_and_notes_the_onset(self):
        self.after_reply()
        await self.speak()
        self.assertTrue(self.hold.held)
        self.assertEqual((self.listening, self.onsets), (1, 1))
        await self.speak()
        self.assertEqual(self.listening, 1, "one hold per window")
        self.assertTrue(self.hold.speech_heard_within(1.0))

    async def test_its_own_listening_does_not_end_the_hold(self):
        self.after_reply()
        await self.speak()
        self.hold.note_phase("listening", {})  # the emitter echoing our own hold
        self.assertTrue(self.hold.held)

    async def test_the_real_turn_ends_the_hold_without_a_release(self):
        self.after_reply()
        await self.speak()
        self.hold.note_phase("listening", {})  # ours
        self.hold.note_phase("listening", {})  # the transcript's UserStartedSpeaking
        self.assertFalse(self.hold.held)
        await asyncio.sleep(0.2)
        self.assertEqual((self.idles, self.releases), (0, 0))

    async def test_thinking_during_the_hold_ends_it_too(self):
        self.after_reply()
        await self.speak()
        self.hold.note_phase("thinking", {})
        await asyncio.sleep(0.2)
        self.assertEqual(self.idles, 0)

    async def test_no_transcript_within_the_bound_releases_the_mic(self):
        self.after_reply()
        await self.speak()
        await asyncio.sleep(0.2)
        self.assertFalse(self.hold.held)
        self.assertEqual((self.idles, self.releases), (1, 1))
        # The release's own idle must not arm a second window on a closed mic.
        self.hold.note_phase("idle", {})
        self.assertFalse(self.hold.armed)
        await self.speak()
        self.assertEqual(self.listening, 1)

    async def test_no_session_to_keep_means_no_hold(self):
        self.may_hold = False
        self.after_reply()
        await self.speak()
        self.assertFalse(self.hold.held)
        self.assertEqual(self.listening, 0)

    async def test_a_listening_that_could_not_be_sent_leaves_nothing_held(self):
        self.listening_ok = False
        self.after_reply()
        await self.speak()
        self.assertFalse(self.hold.held)
        await asyncio.sleep(0.2)
        self.assertEqual(self.idles, 0)


class TestDeviceEvents(HoldCase):
    async def test_a_wake_during_the_hold_cancels_the_release(self):
        self.after_reply()
        await self.speak()
        self.hold.note_wake()
        await asyncio.sleep(0.2)
        self.assertEqual(self.idles, 0, "a release would close the wake turn's mic")

    async def test_a_stop_during_the_hold_cancels_the_release(self):
        self.after_reply()
        await self.speak()
        self.hold.disarm("stop")
        await asyncio.sleep(0.2)
        self.assertEqual(self.idles, 0)

    async def test_a_flush_after_speech_reports_it_and_ends_the_hold(self):
        self.after_reply()
        await self.speak()
        self.assertTrue(self.hold.note_flush())
        self.assertFalse(self.hold.held)
        await asyncio.sleep(0.2)
        self.assertEqual(self.idles, 0)

    async def test_a_flush_of_a_quiet_window_reports_nothing(self):
        self.after_reply()
        self.assertFalse(self.hold.note_flush())


# ---------------------------------------------------------------------------
# the phase emitter hooks
# ---------------------------------------------------------------------------

class TestPhaseEmitterHooks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = patch.object(FrameProcessor, "process_frame", new=AsyncMock())
        self.base.start()
        self.phases = []

        async def send(value, **extra):
            self.phases.append((value, extra))

        self.emitter = PhaseEmitter(send, idle_debounce_s=0.01)
        self.emitter.push_frame = AsyncMock()
        self.seen = []
        self.emitter.on_phase_sent = lambda value, extra: self.seen.append((value, extra))

    async def asyncTearDown(self):
        await self.emitter.close()
        self.base.stop()

    async def test_every_sent_phase_is_observed(self):
        await self.emitter._emit("replying")
        await self.emitter._emit("idle", extra={"followup": False})
        self.assertEqual(self.seen, [("replying", {}), ("idle", {"followup": False})])

    async def test_hold_listening_only_from_idle(self):
        await self.emitter._emit("idle")
        self.assertTrue(await self.emitter.hold_listening())
        self.assertEqual(self.phases[-1][0], "listening")
        self.assertFalse(await self.emitter.hold_listening(), "a turn is already under way")
        await self.emitter._emit("replying")
        self.assertFalse(await self.emitter.hold_listening())

    async def test_release_hold_only_undoes_a_current_listening(self):
        await self.emitter._emit("idle")
        self.assertFalse(await self.emitter.release_hold())
        await self.emitter.hold_listening()
        self.assertTrue(await self.emitter.release_hold())
        self.assertEqual([p[0] for p in self.phases], ["idle", "listening", "idle"])
        await self.emitter._emit("thinking")
        self.assertFalse(await self.emitter.release_hold(), "the turn moved on; leave it")

    async def test_the_hold_does_not_change_the_dangling_vad_guard(self):
        self.emitter.note_wake()
        await self.emitter._emit("idle")
        await self.emitter.hold_listening()
        self.assertFalse(self.emitter._speech_since_wake)


# ---------------------------------------------------------------------------
# the session gate hooks
# ---------------------------------------------------------------------------

class TestGateHooks(GateCase):
    POLICY = LiveSessionPolicy(idle_s=10.0, followup_s=4.0, max_silent_s=45.0,
                               max_session_s=300.0, open_timeout_s=10.0, tick_s=0.005)

    async def test_an_onset_keeps_the_session_past_the_follow_up_window(self):
        gate = self.make(policy=self.POLICY)
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()
        await self.advance(3, step=1)
        gate.note_speech_onset()  # the user started a long follow-up at 3 s
        for _ in range(6):  # the mic streams while they talk
            await self.advance(1, step=1)
            gate.note_user_audio()
        self.assertEqual(gate.state, OPEN, "the transcript comes when they stop")
        gate.note_user_utterance()
        await self.advance(2, step=1)
        self.assertEqual(gate.state, OPEN)

    async def test_without_an_onset_the_window_still_closes_as_before(self):
        gate = self.make(policy=self.POLICY)
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()
        for _ in range(6):
            await self.advance(1, step=1)
            gate.note_user_audio()  # mic transport activity is not speech
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["follow-up quiet"])

    async def test_a_released_hold_closes_within_the_release_quiet(self):
        gate = self.make(policy=self.POLICY)
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()
        gate.note_speech_onset()
        await self.advance(2, step=1)
        gate.note_hold_released()
        await self.advance(gate_module.HOLD_RELEASE_QUIET_S + 0.5, step=0.5)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["follow-up quiet"])

    async def test_a_release_never_shortens_a_turn_in_flight(self):
        gate = self.make(policy=self.POLICY)
        await gate.open("wake")
        gate.note_user_utterance()
        gate.note_hold_released()
        await self.advance(gate_module.HOLD_RELEASE_QUIET_S + 1, step=0.5)
        self.assertEqual(gate.state, OPEN)


# ---------------------------------------------------------------------------
# the production wiring
# ---------------------------------------------------------------------------

class TestWiring(GateWiringCase):
    POLICY = LiveSessionPolicy(idle_s=2.0, followup_s=0.3, max_silent_s=30.0,
                               max_session_s=600.0, tick_s=0.01)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.handler.follow_up_ms = 8000
        patcher = patch.object(gate_module, "HOLD_RELEASE_QUIET_S", 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def reply_then_idle(self, connection):
        emitter = connection.phase_emitter
        self.phases = []

        async def capture(value, **extra):
            self.phases.append((value, extra))

        emitter._send_phase = capture
        await emitter._emit("replying")
        connection.session_gate.note_audible_output()
        await emitter._emit("idle")
        connection.session_gate.note_reply_finished()

    async def test_the_live_runtime_gets_a_hold_on_the_mic_tap(self):
        connection = self.build()
        hold = connection.followup_hold
        self.assertIsNotNone(hold)
        self.assertIn(hold, connection.serializer._audio_taps)
        self.assertEqual(connection.phase_emitter.on_phase_sent, hold.note_phase)

    async def test_off_or_no_window_means_no_hold(self):
        self.handler.follow_up_hold = False
        self.assertIsNone(self.build("a").followup_hold)
        self.handler.follow_up_hold = True
        self.handler.follow_up_ms = 0
        self.assertIsNone(self.build("b").followup_hold)

    async def test_the_realtime_runtime_gets_no_hold(self):
        connection = self.build(on_demand=False)
        self.assertIsNone(connection.followup_hold)

    async def test_a_late_follow_up_holds_the_mic_and_the_session(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.reply_then_idle(connection)
        connection.followup_hold.hold_max_s = 5.0
        # The user starts talking late in the window; the mic streams speech.
        for i in range(0, len(SPEECH), 3200):
            await connection.serializer.deserialize(SPEECH[i:i + 3200])
        await self.settle(lambda: any(p[0] == "listening" for p in self.phases))
        self.assertTrue(connection.followup_hold.held)
        # Past the follow-up-quiet bound the session is still there, because
        # the transcript of what is being said has not come yet.
        await asyncio.sleep(0.6)
        self.assertEqual(connection.session_gate.state, OPEN)
        self.assertEqual(connection.openai_service.closes, [])

    async def test_a_cut_off_after_speech_keeps_the_session_for_the_transcript(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.reply_then_idle(connection)
        connection.followup_hold.hold_max_s = 5.0
        await connection.serializer.deserialize(SPEECH)
        await self.settle(lambda: connection.followup_hold.held)
        await self.send(connection, {"type": "flush"})  # the device timer won the race
        self.assertEqual(connection.session_gate.state, OPEN)
        self.assertEqual(connection.openai_service.closes, [])
        self.assertFalse(connection.followup_hold.held)

    async def test_a_quiet_cut_off_still_closes_the_session(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.reply_then_idle(connection)
        await connection.serializer.deserialize(ROOM)
        await self.send(connection, {"type": "flush"})
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.closes, ["follow-up cut-off"])

    async def test_a_hold_with_no_transcript_releases_the_mic_and_closes(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.reply_then_idle(connection)
        connection.followup_hold.hold_max_s = 0.1
        await connection.serializer.deserialize(SPEECH)
        await self.settle(lambda: connection.followup_hold.held)
        await self.settle(lambda: self.phases[-1][0] == "idle" and len(self.phases) >= 3)
        self.assertEqual([p[0] for p in self.phases], ["replying", "idle", "listening", "idle"])
        await self.settle(lambda: connection.session_gate.state == CLOSED)
        self.assertEqual(connection.openai_service.closes, ["follow-up quiet"])

    async def test_a_wake_inside_the_hold_is_an_ordinary_wake(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.reply_then_idle(connection)
        connection.followup_hold.hold_max_s = 0.1
        await connection.serializer.deserialize(SPEECH)
        await self.settle(lambda: connection.followup_hold.held)
        await self.send(connection, {"type": "wake", "turn": "k2", "src": "wake_word"})
        self.assertFalse(connection.followup_hold.held)
        await asyncio.sleep(0.25)
        self.assertEqual([p[0] for p in self.phases], ["replying", "idle", "listening"],
                         "no release idle may cut the wake turn's mic")
        self.assertEqual(connection.session_gate.state, OPEN)


if __name__ == "__main__":
    unittest.main()
