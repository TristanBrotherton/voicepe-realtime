"""GPT-Live output audio: format, alignment, continuity, and what must survive.

The canary produced an unintelligible reply from a clean 24 kHz PCM16 stream.
These tests replay the protocol-faithful delta sequence from ``live_fixtures``
(shapes and durations transcribed from real captures) and assert the only
acceptable outcome: the device receives a byte-exact, in-order, 16-bit-aligned
subsequence of what the server sent, with every audible sample intact.

The defect that is specifically pinned here is the energy gate the canary
shipped: it deleted frames whose RMS fell below 120, and real replies contain
speech frames with peaks as low as 44. ``test_quiet_speech_survives`` fails if
anything like it comes back.
"""
import array
import base64
import math
import unittest

from app.live_audio import (
    MAX_SILENCE_MS,
    SILENCE_PEAK,
    LiveOutputAudio,
    OutputAudioCapture,
    pcm16_stats,
)

from tests import live_fixtures as fx


class FakeClock:
    """A monotonic clock advanced explicitly, so no test waits on wall time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def replay(deltas, assembler=None, clock=None, advance=0.1):
    """Feed deltas to an assembler at their real cadence; return the chunks."""
    clock = clock or FakeClock()
    assembler = assembler or LiveOutputAudio(clock=clock)
    chunks = []
    for delta in deltas:
        chunks.append(assembler.feed(delta.get("delta"), delta.get("start_ms"),
                                     delta.get("end_ms")))
        clock.advance(advance)
    return assembler, chunks


class TestPcmStats(unittest.TestCase):
    def test_level_measurement_matches_the_waveform(self):
        stats = pcm16_stats(fx.tone(100, amplitude=6000))
        self.assertEqual(stats.samples, 2400)
        self.assertAlmostEqual(stats.duration_ms, 100.0, places=6)
        self.assertLessEqual(stats.peak, 6000)
        self.assertGreater(stats.peak, 5900)
        # RMS of a full sine is amplitude / sqrt(2).
        self.assertAlmostEqual(stats.rms, 6000 / math.sqrt(2), delta=60)
        self.assertFalse(stats.inaudible())

    def test_idle_frames_are_inaudible_and_speech_frames_are_not(self):
        self.assertTrue(pcm16_stats(fx.silence(100)).inaudible())
        self.assertTrue(pcm16_stats(fx.silence(100, peak=fx.IDLE_PEAK)).inaudible())
        quiet = pcm16_stats(fx.tone(100, amplitude=fx.QUIET_SPEECH_PEAK))
        self.assertFalse(quiet.inaudible(), "measured in-reply speech peaks at 44-456")
        self.assertGreater(quiet.peak, SILENCE_PEAK)

    def test_odd_trailing_byte_is_ignored_not_misread(self):
        stats = pcm16_stats(fx.tone(10) + b"\x7f")
        self.assertEqual(stats.samples, 240)


class TestFormatAndAlignment(unittest.TestCase):
    def test_every_forwarded_chunk_is_whole_16_bit_samples(self):
        _, chunks = replay(fx.output_audio_deltas())
        forwarded = [c for c in chunks if c.pcm]
        self.assertTrue(forwarded)
        for chunk in forwarded:
            self.assertEqual(len(chunk.pcm) % 2, 0)

    def test_an_odd_length_delta_carries_its_byte_instead_of_shifting_the_stream(self):
        """A dropped byte shifts every later sample and sounds like loud noise."""
        pcm = fx.tone(200, amplitude=6000)
        first, second = pcm[:4801], pcm[4801:]  # a deliberate odd split
        deltas = [
            {"type": "session.output_audio.delta", "delta": base64.b64encode(first).decode()},
            {"type": "session.output_audio.delta", "delta": base64.b64encode(second).decode()},
        ]
        assembler, chunks = replay(deltas)
        out = b"".join(c.pcm for c in chunks)
        self.assertEqual(out, pcm, "the stream must be reassembled byte-for-byte")
        self.assertEqual(assembler.metrics.odd_length, 1)
        self.assertEqual(assembler.metrics.carried_bytes, 1)
        self.assertIn("odd byte length", "; ".join(assembler.failure_reasons()))

    def test_undecodable_and_missing_deltas_are_counted_not_forwarded(self):
        assembler, chunks = replay([
            {"delta": "not base64 !!!"},
            {"delta": None},
            {"delta": ""},
        ])
        self.assertEqual([c.pcm for c in chunks], [b"", b"", b""])
        self.assertEqual(assembler.metrics.undecodable, 3)
        self.assertEqual(assembler.metrics.deltas, 0)
        self.assertIn("could not be decoded", "; ".join(assembler.failure_reasons()))


class TestContinuity(unittest.TestCase):
    def test_the_device_receives_a_byte_exact_in_order_subsequence(self):
        assembler, chunks = replay(fx.output_audio_deltas())
        out = b"".join(c.pcm for c in chunks)
        self.assertEqual(out, fx.expected_forwarded(),
                         "forwarded audio must be the source bytes, unmodified")
        source = fx.reply_pcm()
        self.assertEqual(len(out) % 2, 0)
        self.assertLess(len(out), len(source), "the idle tail is withheld")
        self.assertEqual(out, source[:len(out)],
                         "nothing is re-ordered, duplicated or re-sampled")
        self.assertEqual(assembler.metrics.bytes_out, len(out))
        self.assertEqual(assembler.metrics.bytes_in, len(source))

    def test_each_speech_segment_decodes_to_its_exact_waveform(self):
        """A splice, a dropped frame or a phase jump would break this."""
        assembler, chunks = replay(fx.output_audio_deltas())
        out = b"".join(c.pcm for c in chunks)
        for offset, expected in fx.speech_spans():
            got = out[offset:offset + len(expected)]
            self.assertEqual(got, expected, f"speech at byte {offset} was altered")
        samples = array.array("h")
        samples.frombytes(out)
        # A clean 220 Hz sine at 24 kHz steps by at most ~2*pi*220/24000 of its
        # amplitude between samples. A splice or a one-byte shift produces a
        # far larger step. Checked inside the loud segment only, where the
        # bound is meaningful.
        start, loud = fx.speech_spans()[0]
        begin = start // 2
        end = begin + len(loud) // 2
        bound = 6000 * 2 * math.pi * 220 / 24000 * 1.2
        worst = max(abs(samples[i] - samples[i - 1]) for i in range(begin + 1, end))
        self.assertLess(worst, bound, "waveform discontinuity inside a speech segment")

    def test_chunk_order_and_count_are_preserved(self):
        deltas = fx.output_audio_deltas()
        assembler, chunks = replay(deltas)
        forwarded = [c for c in chunks if c.pcm]
        self.assertEqual(assembler.metrics.deltas, len(deltas))
        # A held lead-in travels inside the chunk for the first audible delta,
        # so chunks are fewer than forwarded deltas by exactly the number
        # released out of the hold — and every byte is still accounted for.
        self.assertEqual(
            assembler.metrics.forwarded_deltas,
            len(forwarded) + assembler.metrics.silence_released_from_hold_deltas,
        )
        self.assertEqual(assembler.metrics.silence_released_from_hold_deltas, 3,
                         "the 300 ms lead-in is three deltas")
        self.assertEqual(assembler.metrics.bytes_out,
                         sum(len(c.pcm) for c in forwarded))
        # Rebuilding from the per-chunk payloads in order equals the stream.
        self.assertEqual(b"".join(c.pcm for c in forwarded),
                         b"".join(c.pcm for c in chunks))

    def test_a_short_final_delta_is_forwarded_whole(self):
        """Real sessions end a reply with a partial delta (1,920 bytes seen)."""
        pcm = fx.tone(140, amplitude=6000)
        assembler, chunks = replay(fx.output_audio_deltas(pcm))
        self.assertEqual(len(fx.chunk(pcm)), 2)
        self.assertEqual(b"".join(c.pcm for c in chunks), pcm)


class TestQuietSpeechAndSilence(unittest.TestCase):
    def test_quiet_speech_survives(self):
        """The canary's regression: an RMS gate deletes real speech.

        The 600 ms quiet segment peaks at 120, so its RMS (~85) is below the
        120.0 threshold the shipped gate used. It is speech and must arrive.
        """
        assembler, chunks = replay(fx.output_audio_deltas())
        out = b"".join(c.pcm for c in chunks)
        offset, quiet = fx.speech_spans()[1]
        self.assertEqual(pcm16_stats(quiet).peak, fx.QUIET_SPEECH_PEAK)
        self.assertLess(pcm16_stats(quiet).rms, 120.0, "below the old gate threshold")
        self.assertEqual(out[offset:offset + len(quiet)], quiet,
                         "quiet speech was deleted — the canary's static defect")

    def test_an_intra_reply_pause_is_passed_through_untouched(self):
        assembler, chunks = replay(fx.output_audio_deltas())
        out = b"".join(c.pcm for c in chunks)
        # The 600 ms inter-sentence pause begins after 300+2000+600 ms.
        offset = fx.SAMPLE_RATE * 2 * 2900 // 1000
        pause = fx.silence(600, peak=fx.IDLE_PEAK)
        self.assertEqual(out[offset:offset + len(pause)], pause)
        # Nothing is suppressed until a run exceeds max_silence_ms, so the
        # pause arrives in full and the reply is not cut in two.
        self.assertEqual(len(out), offset + len(pause) + fx.SAMPLE_RATE * 2 * (1500 + 800) // 1000)

    def test_an_idle_run_is_suppressed_so_the_reply_can_end(self):
        """There is no output-audio-done event; an idle stream must stop."""
        assembler, chunks = replay(fx.output_audio_deltas())
        suppressed = [c for c in chunks if not c.pcm and "idle run" in c.reason]
        # 3,000 ms of idle at the END of a reply: the first 800 ms keeps the
        # device's chain fed through what might still be a pause, and the
        # remaining 2,200 ms is cut so the reply can finish.
        self.assertEqual(len(suppressed), 22)
        self.assertEqual(assembler.metrics.silence_suppressed_deltas, 22)
        self.assertAlmostEqual(assembler.metrics.silence_suppressed_ms, 2200.0, places=3)
        self.assertEqual(assembler.metrics.idle_head_discarded_deltas, 0,
                         "this run began inside a reply, so its head is kept")
        self.assertEqual([], assembler.failure_reasons(),
                         "withholding idle silence is correct, not a malformed stream")

    def test_audible_audio_after_suppression_reopens_the_stream(self):
        clock = FakeClock()
        assembler = LiveOutputAudio(clock=clock)
        replay(fx.output_audio_deltas(fx.silence(3000)), assembler, clock)
        self.assertGreater(assembler.metrics.silence_suppressed_deltas, 0)
        _, chunks = replay(fx.output_audio_deltas(fx.tone(200)), assembler, clock)
        self.assertEqual(b"".join(c.pcm for c in chunks), fx.tone(200))
        self.assertTrue(any(c.started_segment for c in chunks))


class TestSegmentsAndSpeakingState(unittest.TestCase):
    def test_one_reply_is_one_segment(self):
        assembler, chunks = replay(fx.output_audio_deltas())
        starts = [index for index, c in enumerate(chunks) if c.started_segment]
        self.assertEqual(len(starts), 1, "a reply must not be split into segments")
        # The first audible delta is at 300 ms, i.e. delta index 3.
        self.assertEqual(starts, [3])
        self.assertEqual(assembler.segments, 1)

    def test_speaking_is_true_during_a_reply_and_false_after_it(self):
        clock = FakeClock()
        assembler = LiveOutputAudio(clock=clock)
        assembler.feed(base64.b64encode(fx.tone(100)).decode())
        self.assertTrue(assembler.speaking)
        clock.advance(assembler.speech_hangover_s + 0.01)
        self.assertFalse(assembler.speaking)

    def test_reset_clears_assembly_state_but_keeps_the_evidence(self):
        assembler, _ = replay(fx.output_audio_deltas())
        deltas, digest = assembler.metrics.deltas, assembler.digest
        assembler.reset()
        self.assertFalse(assembler.speaking)
        self.assertEqual(assembler.metrics.deltas, deltas)
        self.assertEqual(assembler.digest, digest)


class TestTimedDeltas(unittest.TestCase):
    def test_the_real_endpoint_sends_no_timing_fields(self):
        assembler, _ = replay(fx.output_audio_deltas())
        self.assertEqual(assembler.metrics.untimed_deltas, assembler.metrics.deltas)
        self.assertEqual(assembler.metrics.timeline_gaps, 0)

    def test_the_documented_timed_variant_is_measured_not_acted_on(self):
        assembler, chunks = replay(fx.timed_output_audio_deltas())
        self.assertEqual(assembler.metrics.untimed_deltas, 0)
        self.assertEqual(assembler.metrics.timeline_overlaps, 0)
        self.assertEqual(assembler.metrics.timeline_gaps, 0,
                         "a contiguous timeline has no omitted silence")
        self.assertEqual(b"".join(c.pcm for c in chunks), fx.expected_forwarded(),
                         "timing fields must not change which bytes are forwarded")

    def test_a_timeline_gap_is_recorded_without_inserting_silence(self):
        deltas = fx.timed_output_audio_deltas(fx.tone(200))
        deltas[1]["start_ms"] += 500
        deltas[1]["end_ms"] += 500
        assembler, chunks = replay(deltas)
        self.assertEqual(assembler.metrics.timeline_gaps, 1)
        self.assertEqual(assembler.metrics.timeline_gap_ms_max, 500)
        self.assertEqual(b"".join(c.pcm for c in chunks), fx.tone(200),
                         "a gap is never filled with synthesised audio")

    def test_an_overlapping_range_is_reported_as_malformed(self):
        deltas = fx.timed_output_audio_deltas(fx.tone(300))
        deltas[2]["start_ms"] -= 50
        assembler, _ = replay(deltas)
        self.assertEqual(assembler.metrics.timeline_overlaps, 1)
        self.assertIn("overlapped", "; ".join(assembler.failure_reasons()))


class TestObservability(unittest.TestCase):
    def test_the_snapshot_carries_diagnosis_and_no_audio(self):
        assembler, _ = replay(fx.output_audio_deltas())
        snapshot = assembler.snapshot()
        for key in ("deltas", "forwarded_deltas", "bytes_in", "bytes_out", "segments",
                    "peak", "silent_deltas", "silence_suppressed_deltas", "odd_length",
                    "undecodable", "untimed_deltas", "realtime_ratio", "digest"):
            self.assertIn(key, snapshot)
        self.assertEqual(len(snapshot["digest"]), 16)
        blob = repr(snapshot)
        self.assertNotIn("\\x", blob, "a snapshot must never contain PCM")
        self.assertLess(len(blob), 4000, "the snapshot is bounded")
        self.assertLessEqual(len(snapshot["recent_deltas"]), 12)
        for record in snapshot["recent_deltas"]:
            self.assertEqual(sorted(record), ["bytes", "end_ms", "peak", "rms",
                                              "start_ms", "t", "zero"])

    def test_the_digest_identifies_the_stream_without_storing_it(self):
        first, _ = replay(fx.output_audio_deltas())
        second, _ = replay(fx.output_audio_deltas())
        third, _ = replay(fx.output_audio_deltas(fx.tone(500, freq=440.0)))
        self.assertEqual(first.digest, second.digest)
        self.assertNotEqual(first.digest, third.digest)

    def test_the_delivery_pace_is_measured(self):
        """A ~1.0x ratio is why the device needs a relay-side playout lead."""
        clock = FakeClock()
        assembler, _ = replay(fx.output_audio_deltas(fx.tone(2000)), clock=clock,
                              advance=0.1)
        self.assertIsNotNone(assembler.metrics.realtime_ratio)
        self.assertAlmostEqual(assembler.metrics.realtime_ratio, 1.05, delta=0.1)
        self.assertEqual(assembler.failure_reasons(), [],
                         "real-time pacing is normal for Live, not a malformed stream")

    def test_a_turn_report_covers_one_reply_not_the_session(self):
        """Session totals cannot answer "was *that* reply broken?"."""
        clock = FakeClock()
        assembler = LiveOutputAudio(clock=clock)
        replay(fx.output_audio_deltas(fx.tone(500)), assembler, clock)
        first = assembler.take_turn_report()
        self.assertEqual(first["deltas"], 5)
        self.assertEqual(first["bytes_out"], len(fx.tone(500)))

        replay(fx.output_audio_deltas(fx.tone(200)), assembler, clock)
        second = assembler.take_turn_report()
        self.assertEqual(second["deltas"], 2, "the second reply only")
        self.assertEqual(second["bytes_out"], len(fx.tone(200)))
        self.assertEqual(assembler.metrics.deltas, 7, "the session total still accrues")

        quiet = assembler.take_turn_report()
        self.assertEqual(quiet["deltas"], 0, "nothing new to report")
        self.assertEqual(quiet["peak"], assembler.metrics.peak,
                         "levels are absolute, not differential")

    def test_the_delta_history_is_bounded(self):
        assembler, _ = replay(fx.output_audio_deltas(fx.silence(60000)))
        self.assertLessEqual(len(assembler.history), 240)


class TestOptInCapture(unittest.TestCase):
    def test_capture_is_off_by_default(self):
        assembler = LiveOutputAudio(clock=FakeClock())
        self.assertIsNone(assembler.capture)
        self.assertNotIn("capture", assembler.snapshot())

    def test_capture_is_bounded_and_decodable(self):
        capture = OutputAudioCapture(limit_bytes=fx.SAMPLE_RATE * 2 * 200 // 1000)
        clock = FakeClock()
        assembler = LiveOutputAudio(clock=clock, capture=capture)
        replay(fx.output_audio_deltas(fx.tone(1000)), assembler, clock)
        self.assertEqual(len(capture.pcm), capture.limit_bytes)
        self.assertTrue(capture.truncated)
        self.assertEqual(bytes(capture.pcm), fx.tone(1000)[:capture.limit_bytes])
        wav = capture.wav()
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(wav[22:24], (1).to_bytes(2, "little"), "mono")
        self.assertEqual(wav[24:28], (24000).to_bytes(4, "little"), "24 kHz")
        self.assertEqual(wav[34:36], (16).to_bytes(2, "little"), "16-bit")
        self.assertEqual(len(wav), 44 + capture.limit_bytes)
        self.assertEqual(assembler.snapshot()["capture"]["held_bytes"], capture.limit_bytes)


class TestDefaults(unittest.TestCase):
    def test_thresholds_match_what_the_captures_measured(self):
        self.assertEqual(SILENCE_PEAK, 4)
        self.assertLess(SILENCE_PEAK, 44, "the quietest measured in-reply speech peak")
        self.assertGreaterEqual(SILENCE_PEAK, fx.IDLE_PEAK)
        self.assertEqual(MAX_SILENCE_MS, 800)
        self.assertGreater(MAX_SILENCE_MS, 600, "the measured inter-sentence pause")


if __name__ == "__main__":
    unittest.main()
