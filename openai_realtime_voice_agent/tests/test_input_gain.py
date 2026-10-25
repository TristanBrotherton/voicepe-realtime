"""A quiet voice is brought up to the level GPT-Live transcribes (app/input_gain.py)."""
import math
import struct
import unittest

import numpy as np

from app.input_gain import DEFAULT_MAX_GAIN, TARGET_RMS, SpeechLevelGain

RATE = 24000


def tone(ms: int, amplitude: int, hz: float = 220.0) -> bytes:
    n = RATE * ms // 1000
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * hz * i / RATE))
                                    for i in range(n)))


def rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


class TestSpeechLevelGain(unittest.TestCase):
    def test_quiet_speech_is_raised_toward_the_target(self):
        gain = SpeechLevelGain(sample_rate=RATE)
        quiet = tone(1000, 1500)  # RMS ~1060: a loudspeaker across the room
        out = gain.process(quiet)
        tail = out[len(out) // 2:]  # past the first block's ramp
        self.assertAlmostEqual(rms(tail), TARGET_RMS, delta=TARGET_RMS * 0.08)
        self.assertGreater(gain.max_gain_applied, 3.0)
        self.assertLessEqual(gain.max_gain_applied, DEFAULT_MAX_GAIN)

    def test_a_very_quiet_signal_gets_at_most_the_cap(self):
        gain = SpeechLevelGain(sample_rate=RATE, max_gain=4.0)
        out = gain.process(tone(1000, 400))  # RMS ~283
        self.assertAlmostEqual(rms(out[len(out) // 2:]), 283 * 4.0, delta=60)
        self.assertEqual(gain.max_gain_applied, 4.0)

    def test_a_voice_near_the_device_is_left_alone(self):
        gain = SpeechLevelGain(sample_rate=RATE)
        loud = tone(1000, 10000)  # RMS ~7071
        self.assertEqual(gain.process(loud), loud, "never attenuated, never changed")
        self.assertEqual(gain.boosted_blocks, 0)

    def test_a_loud_block_after_quiet_ones_is_not_boosted_into_clipping(self):
        gain = SpeechLevelGain(sample_rate=RATE)
        gain.process(tone(1000, 1500))
        self.assertGreater(gain.gain, 3.0)
        loud = tone(100, 12000)  # RMS ~8485, one breath later
        out = gain.process(loud)
        a = np.frombuffer(out, dtype=np.int16).astype(np.int32)
        self.assertLessEqual(int(a.max()), 32767)
        self.assertGreaterEqual(int(a.min()), -32768)
        # Instant attack: judged with this block included, so only the ramp of
        # the first 20 ms block carries any of the old gain.
        self.assertLess(rms(out[len(out) // 5:]), 8485 * 1.05)
        self.assertEqual(gain.gain, 1.0)

    def test_the_gain_returns_slowly_between_utterances(self):
        gain = SpeechLevelGain(sample_rate=RATE, half_life_s=0.5)
        gain.process(tone(500, 10000))
        self.assertEqual(gain.gain, 1.0)
        gain.process(tone(200, 0))
        self.assertLess(gain.gain, 2.0, "not yet: the level decays over the half-life")
        gain.process(tone(2000, 0))
        self.assertEqual(gain.gain, DEFAULT_MAX_GAIN, "quiet long enough: ready for a quiet voice")

    def test_frames_of_any_size_come_out_whole_and_in_order(self):
        gain = SpeechLevelGain(sample_rate=RATE)
        src = tone(1000, 1500)
        out = b""
        for i in range(0, len(src), 1234):  # not a multiple of a 20 ms block
            out += gain.process(src[i:i + 1234])
        self.assertEqual(len(out), len(src) - len(src) % (gain.block * 2))
        self.assertEqual(len(gain._carry), len(src) % (gain.block * 2))

    def test_off_is_a_passthrough(self):
        gain = SpeechLevelGain(sample_rate=RATE, max_gain=1.0)
        self.assertFalse(gain.enabled)
        odd = tone(1000, 1500)[:1001]
        self.assertEqual(gain.process(odd), odd)
        self.assertEqual(gain.process(b""), b"")


if __name__ == "__main__":
    unittest.main()
