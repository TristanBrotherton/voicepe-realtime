"""DeviceRegistry targeting, identity, lifecycle and frame formatting."""
import asyncio
import unittest

from app.device_registry import (
    DeviceConnection,
    DeviceRegistry,
    device_id_from_websocket,
    sanitize_device_id,
)
from app.session_manager import SessionManager


class FakeURL:
    def __init__(self, query):
        self.query = query


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeWS:
    def __init__(self, query="", host=None):
        self.url = FakeURL(query)
        self.client = FakeClient(host) if host else None
        self.sent = []

    async def send_text(self, data):
        self.sent.append(data)


class TestIdentity(unittest.TestCase):
    def test_query_param_wins_over_ip(self):
        self.assertEqual(device_id_from_websocket(FakeWS("device_id=kitchen", "192.0.2.9")), "kitchen")

    def test_ip_fallback_and_unknown(self):
        self.assertEqual(device_id_from_websocket(FakeWS("", "192.0.2.9")), "192.0.2.9")
        self.assertEqual(device_id_from_websocket(FakeWS("", None)), "unknown")

    def test_hostile_ids_are_sanitized(self):
        self.assertEqual(sanitize_device_id('kit chen"; drop'), "kitchendrop")
        self.assertEqual(len(sanitize_device_id("x" * 500)), 64)


class TestTargeting(unittest.IsolatedAsyncioTestCase):
    async def test_default_target_follows_activity(self):
        reg = DeviceRegistry()
        kitchen = DeviceConnection("kitchen", FakeWS())
        await reg.add(kitchen)
        self.assertIs(reg.resolve(), kitchen, "sole device must work before first activity")
        office = DeviceConnection("office", FakeWS())
        await reg.add(office)
        self.assertEqual(len(reg), 2)
        self.assertEqual(reg.ids(), ["kitchen", "office"])
        self.assertIsNone(reg.resolve(), "idle devices must not receive implicit sends")

        kitchen.touch()
        await asyncio.sleep(0.01)
        office.touch()
        self.assertIs(reg.resolve(), office)
        kitchen.touch()
        self.assertIs(reg.resolve(), kitchen)
        self.assertIs(reg.resolve("office"), office, "explicit id wins")
        self.assertIsNone(reg.resolve("bedroom"), "unknown explicit id must NOT fall back")

        reconnect = DeviceConnection("bedroom", FakeWS())
        await reg.add(reconnect)
        self.assertIs(reg.resolve(), kitchen, "idle reconnect stole the active target")
        await reg.remove(reconnect)

    async def test_reconnect_replaces_and_stale_disconnect_cannot_evict(self):
        reg = DeviceRegistry()
        kitchen = DeviceConnection("kitchen", FakeWS())
        office = DeviceConnection("office", FakeWS())
        await reg.add(kitchen)
        await reg.add(office)
        kitchen2 = DeviceConnection("kitchen", FakeWS())
        displaced = await reg.add(kitchen2)
        self.assertIs(displaced, kitchen)
        self.assertIs(reg.get("kitchen"), kitchen2)
        self.assertEqual(len(reg), 2)
        self.assertFalse(await reg.remove(kitchen))
        self.assertIs(reg.get("kitchen"), kitchen2)
        self.assertTrue(await reg.remove(kitchen2))
        self.assertIsNone(reg.get("kitchen"))

    def test_stale_session_cleanup_preserves_replacement(self):
        sessions = SessionManager()
        old_service, new_service = object(), object()
        sessions.set_current_service("kitchen", old_service)
        sessions.set_current_service("kitchen", new_service)
        sessions.handle_client_disconnect("kitchen", old_service)
        self.assertIs(sessions.get_current_service("kitchen"), new_service)
        sessions.handle_client_disconnect("kitchen", new_service)
        self.assertIsNone(sessions.get_current_service("kitchen"))


class TestFrames(unittest.IsolatedAsyncioTestCase):
    async def test_phase_frames_are_compact_json(self):
        ws = FakeWS()
        conn = DeviceConnection("kitchen", ws)
        await conn.send_phase("listening")
        # The firmware does a literal substring match on this exact form.
        self.assertEqual(ws.sent, ['{"type":"phase","value":"listening"}'])

    async def test_phase_is_unicast_and_broadcast_reaches_all(self):
        reg = DeviceRegistry()
        a, b = DeviceConnection("a", FakeWS()), DeviceConnection("b", FakeWS())
        await reg.add(a)
        await reg.add(b)
        await a.send_phase("replying")
        self.assertEqual(len(a.websocket.sent), 1)
        self.assertEqual(len(b.websocket.sent), 0, "phase leaked to the other device")
        self.assertEqual(await reg.broadcast_json({"type": "hello"}), 2)


if __name__ == "__main__":
    unittest.main()
