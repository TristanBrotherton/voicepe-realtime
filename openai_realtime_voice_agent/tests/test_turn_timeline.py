"""Per-turn latency timeline: stage intervals, endpointing delay, device merge."""
import asyncio
import json
import unittest

from app.turn_timeline import LatencyStats, TurnTimeline, sanitize_turn_id


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


def run_turn(timeline, clock, turn_id="d1-1", tool_ms=None):
    timeline.begin(turn_id=turn_id, source="wake_word", meta={"model": "hey_leonard", "cutoff": 0.64, "window": 3})
    clock.advance(1300)
    timeline.mark("first_audio_frame")
    clock.advance(900)
    timeline.mark("speech_started")
    clock.advance(1500)
    # 2000 ms of audio streamed; OpenAI says speech ended at 1400 ms -> 600 ms endpointing.
    timeline.note_speech_stopped(audio_end_ms=1400, audio_start_ms=300, appended_audio_ms=2000)
    clock.advance(120)
    timeline.mark("response_created")
    if tool_ms:
        record = timeline.tool_started("web_search")
        clock.advance(tool_ms)
        timeline.tool_finished(record, ok=True)
    clock.advance(480)
    timeline.mark("first_model_audio")
    clock.advance(400)
    timeline.mark("first_audio_sent")
    timeline.mark("bot_started")
    clock.advance(2000)
    timeline.mark("bot_stopped", overwrite=True)
    return timeline.finish("replied")


class TestTurnTimeline(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.published = []
        self.timeline = TurnTimeline("kitchen", publish=lambda s, st: self.published.append((s, st)),
                                     clock=self.clock)

    def test_intervals_and_endpointing_delay(self):
        turn = run_turn(self.timeline, self.clock)
        summary = self.timeline.summary(turn)
        i = summary["intervals"]
        self.assertEqual(i["wake_to_first_frame_ms"], 1300)
        self.assertEqual(i["speech_ms"], 1500)
        self.assertEqual(i["speech_end_to_response_ms"], 120)
        self.assertEqual(i["speech_end_to_first_model_audio_ms"], 600)
        self.assertEqual(i["model_audio_to_sent_ms"], 400)
        self.assertEqual(i["speech_end_to_first_audio_sent_ms"], 1000)
        self.assertEqual(i["vad_endpoint_delay_ms"], 600)
        self.assertEqual(i["true_speech_end_to_first_audio_sent_ms"], 1600)
        self.assertEqual(i["speech_audio_ms"], 1100)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["wake"], {"model": "hey_leonard", "cutoff": 0.64, "window": 3})

    def test_tool_durations_are_recorded(self):
        turn = run_turn(self.timeline, self.clock, tool_ms=2500)
        summary = self.timeline.summary(turn)
        self.assertEqual(summary["tools"], [{"name": "web_search", "ms": 2500, "ok": True}])
        self.assertEqual(summary["intervals"]["tool_ms_total"], 2500)

    def test_summary_is_emitted_without_event_loop_and_counts_stats(self):
        run_turn(self.timeline, self.clock)
        self.assertEqual(len(self.published), 1)
        summary, stats = self.published[0]
        self.assertEqual(stats["speech_end_to_first_audio_sent_ms"]["p50"], 1000.0)
        # Privacy: numbers, ids, tool names, wake metadata — no text payloads.
        flat = json.dumps(summary)
        for forbidden in ("transcript", "text", "audio_b64"):
            self.assertNotIn(forbidden, flat)

    def test_device_metrics_merge_by_turn_and_seq(self):
        run_turn(self.timeline, self.clock, turn_id="d1-7")
        self.timeline.merge_device_metrics("d1-7", {"fire_to_mic_ms": 1250, "first_audio_to_audible_ms": 212,
                                                    "seq": 0, "note": "ignored"})
        turn = self.timeline.find("d1-7")
        self.assertEqual(turn.device, {"fire_to_mic_ms": 1250, "first_audio_to_audible_ms": 212, "seq": 0})

    def test_follow_up_turns_get_derived_ids(self):
        run_turn(self.timeline, self.clock, turn_id="d1-9")
        follow = self.timeline.ensure_active()
        self.assertEqual(follow.turn_id, "d1-9.f1")
        self.assertEqual(follow.source, "follow_up")
        self.timeline.finish("replied")
        self.assertEqual(self.timeline.ensure_active().turn_id, "d1-9.f2")

    def test_new_wake_supersedes_an_open_turn(self):
        self.timeline.begin("a")
        self.timeline.begin("b")
        self.assertEqual(self.timeline.find("a").outcome, "superseded")

    def test_late_outcome_change(self):
        run_turn(self.timeline, self.clock, turn_id="x1")
        self.timeline.set_outcome("x1", "false_wake")
        self.assertEqual(self.timeline.find("x1").outcome, "false_wake")

    def test_counters_for_confirmation_gate(self):
        self.timeline.begin("w1")
        self.timeline.note_speech_stopped(None, None, None)
        self.assertEqual((self.timeline.wake_seq, self.timeline.user_turn_seq), (1, 1))
        self.timeline.ensure_active()
        self.assertEqual(self.timeline.wake_seq, 1, "follow-ups are not wakes")

    def test_sanitize(self):
        self.assertEqual(sanitize_turn_id('ab"; rm -rf /'), "abrm-rf")
        self.assertEqual(len(sanitize_turn_id("x" * 100)), 40)


class TestEmitTiming(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_device_metrics_then_emits_once(self):
        clock = Clock()
        published = []
        timeline = TurnTimeline("kitchen", publish=lambda s, st: published.append(s), clock=clock)
        timeline.DEVICE_METRICS_GRACE_S = 5
        run_turn(timeline, clock, turn_id="t1")
        self.assertEqual(published, [], "held for device metrics")
        timeline.merge_device_metrics("t1", {"first_audio_to_audible_ms": 180})
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["device"]["first_audio_to_audible_ms"], 180)
        timeline.close()
        self.assertEqual(len(published), 1, "no duplicate on close")

    async def test_close_flushes_pending_summaries(self):
        clock = Clock()
        published = []
        timeline = TurnTimeline("kitchen", publish=lambda s, st: published.append(s), clock=clock)
        run_turn(timeline, clock, turn_id="t2")
        timeline.close()
        self.assertEqual([s["turn_id"] for s in published], ["t2"])


class TestLatencyStats(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        stats = LatencyStats()
        for v in range(1, 11):
            stats.add("k", v * 100)
        snap = stats.snapshot()["k"]
        self.assertEqual((snap["n"], snap["p50"], snap["p90"]), (10, 500.0, 900.0))
        self.assertEqual(stats.p50("k"), 500.0)
        self.assertIsNone(stats.p50("missing"))


if __name__ == "__main__":
    unittest.main()
