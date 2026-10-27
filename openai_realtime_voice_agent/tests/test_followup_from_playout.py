"""The follow-up window runs from the end of PLAYBACK, not of the transcript.

Kitchen 2026-10-26 19:49: the reply's transcript ended at :42.99 and the
window started there; the device finished playing at :46.2 and the session
closed "follow-up quiet" at :51.2, so the follow-up question ("what is the
weather tomorrow") went to a closed session and got no answer.
"""
import time
import unittest

from app.live_session_gate import CLOSED, OPEN, LiveSessionPolicy
import app.live_session_gate as gate_module
from app.live_service import DEVICE_PLAYOUT_TAIL_S, OpenAILiveLLMService
from tests.test_live_session_gate import FakeLiveService, GateCase

POLICY = LiveSessionPolicy(idle_s=10.0, followup_s=8.0, max_silent_s=45.0,
                           max_session_s=300.0, open_timeout_s=10.0, tick_s=0.005)


class PlayingService(FakeLiveService):
    def __init__(self, remaining):
        super().__init__()
        self.remaining = remaining

    def playout_remaining_s(self):
        return self.remaining


class TestWindowFromPlayback(GateCase):
    async def test_the_window_waits_for_the_audio_still_playing(self):
        gate = self.make(policy=POLICY, service=PlayingService(3.2))
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()       # transcript end; 3.2 s still to play
        await self.advance(10.5, step=0.5)  # 3.2 s playout + 7.3 s of the window
        self.assertEqual(gate.state, OPEN, "the user had less than 8 s after hearing the end")
        await self.advance(1.5, step=0.5)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["follow-up quiet"])

    async def test_the_device_report_restarts_the_window(self):
        gate = self.make(policy=POLICY)  # no estimate at all
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()
        await self.advance(4, step=0.5)
        gate.note_playback_ended()       # the device finished playing 4 s later
        await self.advance(7.5, step=0.5)
        self.assertEqual(gate.state, OPEN)
        await self.advance(1.0, step=0.5)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["follow-up quiet"])

    async def test_a_report_never_shortens_the_window(self):
        gate = self.make(policy=POLICY, service=PlayingService(5.0))
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()
        gate.note_playback_ended()       # early report: keep the later deadline
        await self.advance(12.5, step=0.5)
        self.assertEqual(gate.state, OPEN)

    async def test_a_report_outside_a_follow_up_window_does_nothing(self):
        gate = self.make(policy=POLICY)
        await gate.open("wake")
        gate.note_user_utterance()       # a turn in flight
        before = gate._idle_deadline
        gate.note_playback_ended()
        self.assertEqual(gate._idle_deadline, before)

    async def test_a_wild_estimate_is_bounded(self):
        gate = self.make(policy=POLICY, service=PlayingService(600.0))
        await gate.open("wake")
        gate.note_audible_output()
        gate.note_reply_finished()
        await self.advance(gate_module.PLAYOUT_WAIT_MAX_S + POLICY.followup_s + 1, step=0.5)
        await self._settle(lambda: gate.state == CLOSED)


class TestPlayoutEstimate(unittest.TestCase):
    def test_real_time_paced_audio_plays_out_after_the_last_push(self):
        svc = OpenAILiveLLMService.__new__(OpenAILiveLLMService)
        self.assertEqual(svc.playout_remaining_s(), 0.0)
        for _ in range(10):              # 2 s of 24 kHz mono PCM16, in a burst
            svc._note_audio_pushed(9600)
        remaining = svc.playout_remaining_s()
        self.assertAlmostEqual(remaining, 2.0 + DEVICE_PLAYOUT_TAIL_S, delta=0.1)
        svc._playout_end_mono = time.monotonic() - 5
        self.assertEqual(svc.playout_remaining_s(), 0.0)


if __name__ == "__main__":
    unittest.main()
