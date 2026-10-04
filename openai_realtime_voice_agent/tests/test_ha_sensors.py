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
