"""False-wake labels: device/turn scoping, bounded windows, label-safe retention."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app.wake_events import (
    CaptureConfig,
    ProbeArchive,
    WakeAudioCapture,
    WakeEventStore,
    weekly_report,
)

PCM_1S = b"\x01\x00" * 16000


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class StoreTestCase(unittest.TestCase):
    mode = "audio"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.clock = Clock()
        self.wall = Clock(1_790_000_000.0)
        self.config = CaptureConfig(mode=self.mode, probe_dir=self.dir)
        self.store = WakeEventStore(self.config, instance="test", clock=self.clock, wall=self.wall)

    def tearDown(self):
        self.tmp.cleanup()

    def wavs(self):
        return sorted(n for n in os.listdir(self.dir) if n.endswith(".wav"))


class TestCaptureConfig(unittest.TestCase):
    def test_auto_follows_legacy_enable_recording(self):
        with patch.dict(os.environ, {"WAKE_CAPTURE": "auto", "ENABLE_RECORDING": "true"}):
            self.assertEqual(CaptureConfig.from_env().mode, "audio")
        with patch.dict(os.environ, {"WAKE_CAPTURE": "auto", "ENABLE_RECORDING": "false"}):
            self.assertEqual(CaptureConfig.from_env().mode, "metadata")

    def test_explicit_modes_and_unknown_value(self):
        for mode in ("off", "metadata", "audio"):
            with patch.dict(os.environ, {"WAKE_CAPTURE": mode}):
                self.assertEqual(CaptureConfig.from_env().mode, mode)
        with patch.dict(os.environ, {"WAKE_CAPTURE": "everything"}):
            self.assertEqual(CaptureConfig.from_env().mode, "metadata")


class TestLabeling(StoreTestCase):
    def test_flag_labels_this_devices_newest_wake_only(self):
        kitchen = self.store.record_wake("kitchen", "k1", model="hey_leonard", cutoff=0.64, window=3)
        self.store.save_probe(kitchen, PCM_1S)
        self.clock.t += 2
        office = self.store.record_wake("office", "o1")
        self.store.save_probe(office, PCM_1S)
        # The kitchen flags its own false wake, even though office woke later.
        labeled = self.store.flag_false_wake("kitchen", "double_press")
        self.assertIs(labeled, kitchen)
        self.assertEqual(office.label, "")
        names = self.wavs()
        self.assertTrue(any(n.startswith("falsewake_") and "_kitchen_k1" in n for n in names))
        self.assertTrue(any(n.startswith("probe_") and "_office_o1" in n for n in names))

    def test_flag_outside_window_is_ignored(self):
        self.store.record_wake("kitchen", "k1")
        self.clock.t += self.config.flag_window_s + 1
        self.assertIsNone(self.store.flag_false_wake("kitchen", "double_press"))

    def test_flag_with_turn_id_reaches_an_older_wake(self):
        first = self.store.record_wake("kitchen", "k1")
        self.clock.t += 5
        self.store.record_wake("kitchen", "k2")
        self.assertIs(self.store.flag_false_wake("kitchen", "queued", turn_id="k1"), first)

    def test_queued_flag_respects_its_own_window(self):
        self.store.record_wake("kitchen", "k1")
        self.clock.t += self.config.queued_flag_window_s + 5
        self.assertIsNone(self.store.flag_false_wake("kitchen", "queued", turn_id="k1"))

    def test_flag_before_capture_saves_directly_as_label(self):
        event = self.store.record_wake("kitchen", "k1")
        self.store.flag_false_wake("kitchen", "button")
        self.store.save_probe(event, PCM_1S)
        self.assertTrue(self.wavs()[0].startswith("falsewake_"))
        sidecar = os.path.join(self.dir, "meta", f"{event.stem}.json")
        with open(sidecar) as f:
            meta = json.load(f)
        self.assertEqual(meta["label_method"], "button")
        self.assertNotIn("probe_path", meta)

    def test_sidecars_stay_out_of_the_trainers_harvest_glob(self):
        event = self.store.record_wake("kitchen", "k1")
        self.store.save_probe(event, PCM_1S)
        self.store.flag_false_wake("kitchen", "voice")
        top_level = [n for n in os.listdir(self.dir) if n.startswith("falsewake_")]
        self.assertTrue(all(n.endswith(".wav") for n in top_level))

    def test_trigger_snippet_is_labeled_with_its_wake(self):
        event = self.store.record_wake("kitchen", "k1")
        self.store.save_trigger(event, PCM_1S)
        self.store.flag_false_wake("kitchen", "double_press")
        self.assertTrue(any(n.startswith("falsewake_") and n.endswith("_trigger.wav") for n in self.wavs()))

    def test_candidates_never_become_training_negatives(self):
        event = self.store.record_wake("kitchen", "k1")
        self.store.save_probe(event, PCM_1S)
        self.store.mark_candidate("kitchen", "k1", "admission_silence")
        names = self.wavs()
        self.assertTrue(names[0].startswith("candidate_"))
        self.assertFalse(any(n.startswith("falsewake_") for n in names))

    def test_hooks_fire_once_per_label(self):
        seen = []
        self.store.false_wake_hooks.append(lambda e: seen.append(e.turn_id))
        self.store.record_wake("kitchen", "k1")
        self.store.flag_false_wake("kitchen", "voice")
        self.store.flag_false_wake("kitchen", "voice")
        self.assertEqual(seen, ["k1"])

    def test_turn_ids_and_device_ids_are_sanitized(self):
        event = self.store.record_wake("kit/../chen", "a b;c")
        self.assertNotIn("/", event.stem)
        self.assertNotIn(" ", event.stem)


class TestRetention(StoreTestCase):
    def test_labels_survive_a_full_archive(self):
        # 600 captures with 20 labels: the 500-file unlabeled cap must not
        # delete a single label (the old sorted() prune deleted labels first).
        self.config.max_unlabeled = 500
        for i in range(600):
            event = self.store.record_wake("kitchen", f"t{i}")
            self.wall.t += 1
            self.store.save_probe(event, b"\x00\x00" * 160)
            if i % 30 == 0:
                self.store.flag_false_wake("kitchen", "double_press")
            self.clock.t += 31
        names = self.wavs()
        labeled = [n for n in names if n.startswith("falsewake_")]
        unlabeled = [n for n in names if not n.startswith("falsewake_")]
        self.assertEqual(len(labeled), 20)
        self.assertEqual(len(unlabeled), 500)

    def test_ttl_prunes_old_unlabeled_and_old_labels_separately(self):
        archive = ProbeArchive(self.config, now=lambda: 1_000_000.0)
        for name, age_days in (("probe_old.wav", 40), ("probe_new.wav", 1),
                               ("falsewake_old.wav", 200), ("falsewake_mid.wav", 90)):
            path = os.path.join(self.dir, name)
            with open(path, "wb") as f:
                f.write(b"x")
            os.utime(path, (1_000_000.0 - age_days * 86400,) * 2)
        archive.prune()
        self.assertEqual(self.wavs(), ["falsewake_mid.wav", "probe_new.wav"])

    def test_purge_keeps_labels_unless_asked(self):
        for name in ("probe_a.wav", "falsewake_b.wav"):
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(b"x")
        archive = ProbeArchive(self.config)
        self.assertEqual(archive.purge(), 1)
        self.assertEqual(self.wavs(), ["falsewake_b.wav"])
        self.assertEqual(archive.purge(include_labeled=True), 1)
        self.assertEqual(self.wavs(), [])


class TestMetadataOnlyAndGuestMode(StoreTestCase):
    mode = "metadata"

    def test_metadata_mode_never_writes_audio(self):
        event = self.store.record_wake("kitchen", "k1")
        self.assertEqual(self.store.save_probe(event, PCM_1S), "")
        self.store.flag_false_wake("kitchen", "voice")
        self.assertEqual(self.wavs(), [])
        log = os.path.join(self.dir, "meta", "events-test.jsonl")
        with open(log) as f:
            kinds = [json.loads(line)["kind"] for line in f]
        self.assertEqual(kinds, ["wake", "label"])

    def test_guest_mode_stores_nothing(self):
        store = WakeEventStore(CaptureConfig(mode="audio", probe_dir=self.dir), instance="g",
                               guest_mode=lambda: True, clock=self.clock, wall=self.wall)
        event = store.record_wake("kitchen", "k1")
        store.save_probe(event, PCM_1S)
        store.flag_false_wake("kitchen", "voice")
        self.assertEqual(os.listdir(self.dir), [])

    def test_off_mode_stores_nothing(self):
        store = WakeEventStore(CaptureConfig(mode="off", probe_dir=self.dir), instance="o",
                               clock=self.clock, wall=self.wall)
        store.record_wake("kitchen", "k1")
        self.assertIsNotNone(store.flag_false_wake("kitchen", "voice"), "labels still count")
        self.assertEqual(os.listdir(self.dir), [])


class TestWeeklyReport(StoreTestCase):
    mode = "metadata"

    def test_report_aggregates_without_audio_or_text(self):
        for i in range(4):
            self.store.record_wake("kitchen", f"k{i}", model="hey_leonard", cutoff=0.64, window=3)
            self.clock.t += 1
        self.store.flag_false_wake("kitchen", "double_press")
        self.store.record_wake("office", "o1", model="hey_leonard", cutoff=0.64, window=3)
        self.store.mark_candidate("office", "o1", "admission_silence")
        log = os.path.join(self.dir, "meta", "events-test.jsonl")
        report = weekly_report([log], now=self.wall.t + 10)
        self.assertEqual(report["devices"]["kitchen"]["wakes"], 4)
        self.assertEqual(report["devices"]["kitchen"]["false_wake_flags"], 1)
        self.assertEqual(report["devices"]["office"]["unconfirmed_candidates"], 1)
        self.assertEqual(report["operating_points"]["hey_leonard@0.64/3"], 5)
        self.assertEqual(report["totals"]["flag_rate"], round(1 / 5, 4))


class TestWakeAudioCapture(unittest.IsolatedAsyncioTestCase):
    async def test_capture_saves_off_the_event_loop_and_respects_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WakeEventStore(CaptureConfig(mode="audio", probe_dir=tmp), instance="c")
            capture = WakeAudioCapture(store, capture_seconds=1.0)
            event = store.record_wake("kitchen", "k1")
            capture.start(event)
            capture.feed(PCM_1S[:16000])
            capture.feed(PCM_1S[16000:])  # completes 1 s -> save scheduled
            await asyncio.gather(*capture.pending)
            names = [n for n in os.listdir(tmp) if n.endswith(".wav")]
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].startswith("probe_"))

    async def test_short_partial_capture_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WakeEventStore(CaptureConfig(mode="audio", probe_dir=tmp), instance="c")
            capture = WakeAudioCapture(store, capture_seconds=5.0, min_seconds=1.0)
            capture.start(store.record_wake("kitchen", "k1"))
            capture.feed(b"\x00\x00" * 4000)  # 0.25 s
            capture.finalize()
            self.assertEqual([n for n in os.listdir(tmp) if n.endswith(".wav")], [])


if __name__ == "__main__":
    unittest.main()
