"""Baseline measurements for the speaker-owner work: the model's pace per
reply (not per connection) and where each turn's time to audio went."""
import asyncio
import unittest

from pipecat.frames.frames import OutputAudioRawFrame

from app.live_audio import LiveOutputAudio
from app.output_lead_buffer import OutputLeadBuffer
from app.turn_timeline import LIVE_META_KEYS, TurnTimeline
from tests import live_fixtures as fx
from tests.test_live_audio import FakeClock, replay


class TestReplyPace(unittest.TestCase):
    def test_the_ratio_is_per_reply_not_per_connection(self):
        """Idle time between replies used to sit in the denominator."""
        clock = FakeClock()
        assembler = LiveOutputAudio(clock=clock)
        replay(fx.output_audio_deltas(fx.tone(2000)), assembler, clock, advance=0.1)
        replay(fx.output_audio_deltas(fx.silence(1000)), assembler, clock, advance=0.1)
        self.assertEqual(assembler.metrics.replies_measured, 1)
        first = assembler.metrics.reply_realtime_ratio
        self.assertAlmostEqual(first, 1.05, delta=0.1)
        clock.advance(3600.0)  # an idle hour
        replay(fx.output_audio_deltas(fx.tone(2000)), assembler, clock, advance=0.1)
        self.assertAlmostEqual(assembler.metrics.reply_realtime_ratio, 1.05, delta=0.1)
        self.assertLess(assembler.metrics.realtime_ratio, 0.01,
                        "the lifetime ratio is the misleading number")
        report = assembler.take_turn_report()
        self.assertIn("reply_realtime_ratio", report)


class TestWaits(unittest.TestCase):
    def test_waits_add_up_per_turn_and_land_in_the_summary(self):
        timeline = TurnTimeline("dev")
        timeline.begin("t1")
        timeline.add_wait("ack_lag", 120.4, first_only=True)
        timeline.add_wait("ack_lag", 999, first_only=True)
        timeline.add_wait("clip", 300)
        timeline.add_wait("clip", 200)
        timeline.add_wait("not_a_wait", 5)
        meta = timeline.current.meta
        self.assertEqual(meta["wait_ack_lag_ms"], 120)
        self.assertEqual(meta["wait_clip_ms"], 500)
        self.assertNotIn("wait_not_a_wait_ms", meta)
        for key in ("wait_ack_lag_ms", "wait_guess_quiet_ms", "wait_clip_ms",
                    "wait_lead_hold_ms", "wait_owner_queue_ms", "wait_device_prime_ms"):
            self.assertIn(key, LIVE_META_KEYS)
        summary = timeline.summary(timeline.current)
        self.assertEqual(summary["live"]["wait_clip_ms"], 500)

    def test_the_lead_hold_is_reported(self):
        holds = []

        async def go():
            buffer = OutputLeadBuffer(lead_ms=200, on_hold=holds.append)
            pushed = []

            async def push(frame, direction=None):
                pushed.append(frame)
            buffer.push_frame = push
            frame = OutputAudioRawFrame(audio=b"\x01\x00" * 2400, sample_rate=24000, num_channels=1)
            for _ in range(3):
                await buffer.process_frame(frame, None)
                await asyncio.sleep(0.01)
            return pushed

        pushed = asyncio.run(go())
        self.assertEqual(len(holds), 1)
        self.assertGreaterEqual(holds[0], 0)
        self.assertEqual(len(pushed), 3)


if __name__ == "__main__":
    unittest.main()
