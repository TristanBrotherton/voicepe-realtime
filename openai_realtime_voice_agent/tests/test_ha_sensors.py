"""Home Assistant sensor publishing never blocks callers; counters persist."""
import asyncio
import json
import os
import tempfile
import time
import unittest

from app.ha_sensors import DailyCounters, SensorPublisher, StatePoster


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class SlowClient:
    """Stands in for httpx.AsyncClient against a slow or failing HA."""

    def __init__(self, delay=0.0, status=200):
        self.delay = delay
        self.status = status
        self.posts = []

    async def post(self, url, headers=None, json=None):
        await asyncio.sleep(self.delay)
        self.posts.append((url, json))
        return FakeResponse(self.status)

    async def aclose(self):
        pass


class TestStatePoster(unittest.IsolatedAsyncioTestCase):
    async def test_post_nowait_returns_immediately_even_if_ha_is_slow(self):
        client = SlowClient(delay=5.0)
        poster = StatePoster(token_getter=lambda: "t", client_factory=lambda: client)
        t0 = time.monotonic()
        for i in range(50):
            poster.post_nowait("sensor.x", i, {})
        self.assertLess(time.monotonic() - t0, 0.05)
        await poster.close()

    async def test_bursts_coalesce_to_latest_state(self):
        client = SlowClient()
        poster = StatePoster(token_getter=lambda: "t", client_factory=lambda: client)
        for i in range(10):
            poster.post_nowait("sensor.counter", i, {"n": i})
        poster.post_nowait("sensor.other", "x", {})
        await poster.flush()
        await asyncio.sleep(0.01)
        by_entity = {}
        for url, body in client.posts:
            by_entity.setdefault(url.rsplit("/", 1)[1], []).append(body["state"])
        self.assertEqual(by_entity["sensor.counter"][-1], "9")
        self.assertLessEqual(len(by_entity["sensor.counter"]), 2, "a burst must not fan out")
        self.assertEqual(by_entity["sensor.other"], ["x"])
        await poster.close()

    async def test_failures_are_counted_not_raised(self):
        client = SlowClient(status=500)
        poster = StatePoster(token_getter=lambda: "t", client_factory=lambda: client)
        poster.post_nowait("sensor.x", 1, {})
        await poster.flush()
        await asyncio.sleep(0.01)
        self.assertEqual(poster.failed, 1)
        await poster.close()

    async def test_no_token_means_no_posts(self):
        client = SlowClient()
        poster = StatePoster(token_getter=lambda: "", client_factory=lambda: client)
        poster.post_nowait("sensor.x", 1, {})
        await asyncio.sleep(0.01)
        self.assertEqual(client.posts, [])


class TestDailyCounters(unittest.TestCase):
    def test_counts_persist_across_restarts_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            counters = DailyCounters(path, today=lambda: "2026-10-04")
            counters.increment("wakes", "kitchen")
            counters.increment("wakes", "office")
            counters.increment("false_wakes", "kitchen")
            restarted = DailyCounters(path, today=lambda: "2026-10-04")
            self.assertEqual(restarted.get("wakes"), 2)
            self.assertEqual(restarted.device_counts("wakes"), {"kitchen": 1, "office": 1})
            self.assertEqual(restarted.get("false_wakes"), 1)

    def test_rollover_resets_without_new_events(self):
        day = {"v": "2026-10-04"}
        with tempfile.TemporaryDirectory() as tmp:
            counters = DailyCounters(os.path.join(tmp, "c.json"), today=lambda: day["v"])
            counters.increment("wakes")
            self.assertFalse(counters.roll())
            day["v"] = "2026-10-05"
            self.assertTrue(counters.roll())
            self.assertEqual(counters.get("wakes"), 0)
            with open(os.path.join(tmp, "c.json")) as f:
                self.assertEqual(json.load(f)["day"], "2026-10-05")

    def test_stale_file_from_another_day_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as f:
                json.dump({"day": "2026-10-01", "counts": {"wakes": 9}}, f)
            self.assertEqual(DailyCounters(path, today=lambda: "2026-10-04").get("wakes"), 0)


class TestSensorPublisher(unittest.IsolatedAsyncioTestCase):
    async def test_wake_publish_does_not_await_network(self):
        client = SlowClient(delay=8.0)
        poster = StatePoster(token_getter=lambda: "t", client_factory=lambda: client)
        publisher = SensorPublisher(poster=poster, counters=DailyCounters(None))
        t0 = time.monotonic()
        await publisher.wake("kitchen")
        await publisher.false_wake("kitchen")
        self.assertLess(time.monotonic() - t0, 0.05)
        self.assertEqual(publisher.counters.get("wakes"), 1)
        self.assertEqual(publisher.counters.get("false_wakes"), 1)
        await poster.close()

    async def test_latency_sensor_carries_numbers_only(self):
        client = SlowClient()
        poster = StatePoster(token_getter=lambda: "t", client_factory=lambda: client)
        publisher = SensorPublisher(poster=poster, counters=DailyCounters(None))
        publisher.latency(
            {"speech_end_to_first_audio_sent_ms": 812, "device_id": "kitchen", "turn_id": "a1",
             "outcome": "replied", "intervals": {"speech_end_to_first_audio_sent_ms": 812},
             "device": {"fire_to_mic_ms": 1300}, "tools": [{"name": "HassTurnOn", "ms": 210, "ok": True}]},
            {"speech_end_to_first_audio_sent_ms": {"n": 1, "p50": 812.0, "p90": 812.0}},
        )
        await poster.flush()
        await asyncio.sleep(0.01)
        url, body = client.posts[-1]
        self.assertTrue(url.endswith("_latency"))
        self.assertEqual(body["state"], "812")
        self.assertNotIn("transcript", json.dumps(body))
        await poster.close()


if __name__ == "__main__":
    unittest.main()


class RecordingPoster:
    def __init__(self):
        self.posts = {}

    def post_nowait(self, entity, state, attrs):
        self.posts[entity] = (state, attrs)


class TestPerDeviceSensors(unittest.IsolatedAsyncioTestCase):
    """A configured device keeps its own sensor.voicepe_<name>_* names on a
    shared instance; the instance sensors keep the totals."""

    def publisher(self):
        import app.ha_sensors as hs
        poster = RecordingPoster()
        publisher = SensorPublisher(poster=poster, counters=DailyCounters(None))
        publisher.device_names = {"kitchen"}
        return publisher, poster, hs._INST

    async def test_a_configured_device_publishes_under_its_own_name(self):
        publisher, poster, inst = self.publisher()
        await publisher.wake("Kitchen")
        await publisher.wake("Kitchen")
        await publisher.wake("192.0.2.9")
        self.assertEqual(poster.posts["sensor.voicepe_kitchen_wakes_today"][0], 2)
        self.assertEqual(poster.posts[f"sensor.voicepe_{inst}_wakes_today"][0], 3, "the instance keeps the total")
        publisher.wake_model("kitchen", {"model": "m"})
        self.assertIn("sensor.voicepe_kitchen_wake_word", poster.posts)
        publisher.latency({"device_id": "kitchen", "speech_end_to_first_audio_sent_ms": 900}, {})
        self.assertEqual(poster.posts["sensor.voicepe_kitchen_latency"][0], 900)
        await publisher.speaker("male", "A", 0.5, "pitch", "kitchen")
        self.assertIn("sensor.voicepe_kitchen_speaker", poster.posts)

    async def test_an_unconfigured_device_uses_the_instance_name(self):
        publisher, poster, inst = self.publisher()
        publisher.latency({"device_id": "192.0.2.9", "speech_end_to_first_audio_sent_ms": 1}, {})
        self.assertEqual(list(poster.posts), [f"sensor.voicepe_{inst}_latency"])

    def test_each_cost_sensor_shows_one_ledger(self):
        publisher, poster, inst = self.publisher()
        own = {"spend": {"usd": 0.2}}
        publisher.live_session_cost(own, device_id="kitchen", shared=False)
        self.assertEqual(list(poster.posts), ["sensor.voicepe_kitchen_live_session_cost_today"],
                         "a device's own ledger never lands on the instance sensor")
        shared = {"spend": {"usd": 2.4}}
        publisher.live_session_cost(shared, device_id="192.0.2.9", shared=True)
        self.assertEqual(poster.posts[f"sensor.voicepe_{inst}_live_session_cost_today"][0], 2.4)
        self.assertEqual(poster.posts["sensor.voicepe_kitchen_live_session_cost_today"][0], 0.2)

    async def test_timers_per_device(self):
        publisher, poster, inst = self.publisher()

        class Registry:
            def list_timers(self, device_id=None):
                timers = [{"label": "a", "seconds_left": 5, "dev": "kitchen"},
                          {"label": "b", "seconds_left": 9, "dev": "x"}]
                return {"timers": [t for t in timers if device_id is None or t["dev"] == device_id]}

        await publisher.timers(Registry(), "kitchen")
        self.assertEqual(poster.posts["sensor.voicepe_kitchen_active_timers"][0], 1)
        self.assertEqual(poster.posts[f"sensor.voicepe_{inst}_active_timers"][0], 2)

    def test_entity_ids_stay_valid_and_a_same_named_device_still_publishes(self):
        import app.ha_sensors as hs
        publisher, poster, inst = self.publisher()
        publisher.device_names = {"living-room", inst}
        publisher.latency({"device_id": "LIVING-ROOM", "speech_end_to_first_audio_sent_ms": 5}, {})
        self.assertIn("sensor.voicepe_living_room_latency", poster.posts)
        publisher.live_session_cost({"spend": {"usd": 0.3}}, device_id=inst, shared=False)
        self.assertEqual(poster.posts[f"sensor.voicepe_{inst}_live_session_cost_today"][0], 0.3)
