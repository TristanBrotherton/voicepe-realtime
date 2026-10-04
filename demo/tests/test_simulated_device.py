"""The demo script: protocol, timing, failure reporting and honesty labels.

Runs entirely against the built-in simulated add-on: no network, no API key.
"""
import asyncio
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))

import simulated_device as demo  # noqa: E402
from simulated_device import Step  # noqa: E402


def run(steps, **kwargs):
    return asyncio.run(demo.run_demo(steps, dry_run=True, pause_s=0.05, out=io.StringIO(), **kwargs))


class TestDryRun(unittest.TestCase):
    def setUp(self):
        self._prompt = demo.DRY_RUN_PROMPT_S
        demo.DRY_RUN_PROMPT_S = 0.3

    def tearDown(self):
        demo.DRY_RUN_PROMPT_S = self._prompt

    def test_fast_slow_and_stop_paths(self):
        report = run([
            Step("fast", "Turn on the kitchen lights.", "fast"),
            Step("slow", "What's the weather tomorrow?", "slow", timeout_s=10),
            Step("stop", "Tell me a long story.", "fast", interrupt_after_s=0.5, timeout_s=10),
        ])
        self.assertTrue(report["dry_run"])
        self.assertIn("not measurements", report["note"])
        fast, slow, stop = report["runs"][0]
        self.assertEqual([r["outcome"] for r in (fast, slow, stop)], ["ok", "ok", "ok"])
        self.assertEqual(fast["metrics"]["audio_bursts"], 1)
        self.assertGreaterEqual(slow["metrics"]["audio_bursts"], 2, "ack then answer")
        self.assertIn("interrupt_to_silence_ms", stop["metrics"])
        self.assertLess(stop["metrics"]["reply_audio_s"], 6.0, "the reply was cut off")
        for result in (fast, slow, stop):
            self.assertGreater(result["metrics"]["end_of_speech_to_first_audio_ms"], 0)

    def test_failures_report_outcomes_not_numbers(self):
        report = run([
            Step("broken", "Do something.", "fast", behavior="fail"),
            Step("nothing", "Hello?", "fast", behavior="silent", timeout_s=1.0),
        ])
        broken, nothing = report["runs"][0]
        self.assertEqual(broken["outcome"], "error")
        self.assertIn("simulated failure", broken["detail"])
        self.assertEqual(nothing["outcome"], "timeout")
        self.assertEqual(broken["metrics"], {})
        self.assertEqual(nothing["metrics"], {})
        summary = report["summary"]["fast"]
        self.assertEqual((summary["ok"], summary["success_rate"]), (0, 0.0))
        self.assertIsNone(summary["end_of_speech_to_first_audio_ms"]["p50"])

    def test_follow_up_step_sends_no_wake(self):
        report = run([
            Step("ask", "Unlock the front door.", "fast"),
            Step("yes", "Yes.", "fast", followup=True),
        ])
        self.assertEqual([r["outcome"] for r in report["runs"][0]], ["ok", "ok"])

    def test_unreachable_add_on_is_a_reported_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with wave.open(str(Path(tmp) / "x.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(demo.synthetic_prompt(0.3))
            report = asyncio.run(demo.run_demo([Step("x", "Hi.")], url="ws://127.0.0.1:9/",
                                               audio_dir=Path(tmp), out=io.StringIO()))
        (result,) = report["runs"][0]
        self.assertEqual((result["id"], result["outcome"]), ("connect", "error"))
        self.assertFalse(report["dry_run"])


class TestScenarios(unittest.TestCase):
    def test_shipped_scenarios(self):
        steps = demo.load_steps(DEMO / "scenarios.json")
        ids = [s.id for s in steps]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(s.text.strip() and s.path in ("fast", "slow") for s in steps))
        self.assertTrue(any(s.path == "slow" for s in steps), "a tool path with an acknowledgement")
        self.assertTrue(any(s.interrupt_after_s for s in steps), "a stop")
        self.assertTrue(any(s.followup for s in steps), "a follow-up without the wake word")
        self.assertFalse(any(s.behavior for s in steps), "failure behaviours are test-only")

    def test_trailing_silence_is_not_speech(self):
        voiced = struct.pack("<h", 3000) * (demo.FRAME_BYTES // 2)
        frames = demo.split_frames(voiced * 3 + demo.SILENCE * 5)
        self.assertEqual(len(frames), 3)

    def test_live_runs_need_a_url(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            demo.main(["--scenarios", str(DEMO / "scenarios.json")])

    def test_results_schema(self):
        demo.DRY_RUN_PROMPT_S, saved = 0.3, demo.DRY_RUN_PROMPT_S
        try:
            report = run([Step("fast", "Hi.", "fast")])
        finally:
            demo.DRY_RUN_PROMPT_S = saved
        json.dumps(report)
        self.assertEqual(report["schema"], "voicepe.demo.results/1")
        self.assertEqual(set(report["summary"]), {"fast"})


if __name__ == "__main__":
    unittest.main()
