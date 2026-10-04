"""Timer expiry, listing and ringing stay scoped to the originating device."""
import asyncio
import os
import time
import unittest
from unittest.mock import patch

import app.timers as timers
from app.timers import TimerRegistry


def _timer(device_id, label, owner=""):
    return {
        "owner": owner,
        "device_id": device_id,
        "label": label,
        "ends": time.monotonic(),
        "wall": time.time(),
        "task": asyncio.current_task(),
    }


class TestRingEntityMapping(unittest.TestCase):
    def test_mapping_and_legacy_fallback(self):
        env = {
            "TIMER_RING_ENTITY": "switch.legacy_timer",
            "TIMER_RING_ENTITIES": "kitchen=switch.kitchen_timer,office=switch.office_timer",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(timers._ring_entity("office"), "switch.office_timer")
            self.assertEqual(timers._ring_entity("bedroom"), "switch.legacy_timer")
            self.assertEqual(timers._ring_entity("bedroom", allow_legacy=False), "")


class TestTimerTargeting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.grace = patch.object(timers, "ANNOUNCE_GRACE_S", 0)
        self.auto_off = patch.object(timers, "RING_AUTO_OFF_S", 0)
        self.grace.start()
        self.auto_off.start()
        self.registry = TimerRegistry()

    async def asyncTearDown(self):
        for task in [t["task"] for t in self.registry._timers.values()]:
            if task is not asyncio.current_task():
                task.cancel()
        self.grace.stop()
        self.auto_off.stop()

    async def test_announcement_goes_to_originating_device(self):
        calls = []

        async def announce(text, device_id):
            calls.append(("announce", text, device_id))
            return True

        def last_wake(device_id):
            calls.append(("wake", device_id))
            return time.monotonic()

        self.registry.announcer = announce
        self.registry.last_wake = last_wake
        self.registry._timers[1] = _timer("kitchen", "pasta")
        await self.registry._fire(1)
        self.assertEqual(calls[0], ("announce", "Your pasta timer is done.", "kitchen"))
        self.assertEqual(calls[1], ("wake", "kitchen"))
        self.assertEqual(self.registry._timers, {})

    async def test_tools_cannot_see_or_cancel_other_rooms(self):
        first = self.registry.set_timer(60, "tea", device_id="kitchen")
        second = self.registry.set_timer(60, "coffee", device_id="office")
        kitchen_timers = self.registry.list_timers("kitchen")["timers"]
        self.assertEqual([t["id"] for t in kitchen_timers], [first["id"]])
        self.assertEqual(self.registry.cancel(None, "kitchen")["cancelled"], first["id"])
        self.assertEqual(
            self.registry.cancel(second["id"], "kitchen"), {"error": f"no timer {second['id']}"}
        )
        self.assertEqual(self.registry.list_timers("office")["timers"][0]["id"], second["id"])
        self.registry.cancel(second["id"], "office")

    async def test_ring_uses_the_timers_own_device_switch(self):
        ring_calls = []

        async def set_ring(on, device_id, allow_legacy=True):
            if not allow_legacy:
                return False
            ring_calls.append((on, device_id))
            return True

        with patch.object(timers, "_set_ring", set_ring):
            self.registry._timers[3] = _timer("office", "tea")
            await self.registry._fire(3)
            self.assertEqual(ring_calls, [(True, "office"), (False, "office")])

            # With several devices the legacy single switch is disabled.
            self.registry.allow_legacy_ring = lambda _device_id: False
            self.registry._timers[4] = _timer("bedroom", "bread")
            await self.registry._fire(4)
            self.assertEqual(ring_calls, [(True, "office"), (False, "office")])


if __name__ == "__main__":
    unittest.main()
