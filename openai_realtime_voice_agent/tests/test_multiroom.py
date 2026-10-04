"""Multi-room isolation: escalations, announcements and flags stay in their room."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import websockets

from app import openclaw_tool
from tests.test_two_clients import LiveServerTestCase


class FakeLLM:
    def __init__(self):
        self.handlers = {}

    def register_function(self, name, handler):
        self.handlers[name] = handler


class CapturingClient:
    payloads = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        CapturingClient.payloads.append(json)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"answer": "done"})


class TestEscalationRouting(unittest.IsolatedAsyncioTestCase):
    async def test_ask_names_the_requesting_device(self):
        CapturingClient.payloads = []
        llm = FakeLLM()
        with patch.dict("os.environ", {"OPENCLAW_URL": "http://agent.invalid/ask", "INSTANCE_NAME": "home"}), \
             patch.object(openclaw_tool.httpx, "AsyncClient", CapturingClient):
            openclaw_tool.register_openclaw_tool(llm, device_id="office")
            results = []

            async def cb(result):
                results.append(result)

            await llm.handlers["ask_openclaw"](SimpleNamespace(arguments={"question": "book a table"},
                                                               result_callback=cb))
        self.assertEqual(CapturingClient.payloads, [{"question": "book a table", "room": "home",
                                                     "device_id": "office"}])
        self.assertEqual(results, [{"answer": "done"}])

    def test_model_visible_descriptions_are_generic(self):
        text = json.dumps([openclaw_tool.get_openclaw_tool_definition(),
                           openclaw_tool.get_recall_tool_definition()])
        self.assertNotIn("tell the user you are checking", text)


class TestTwoRoomIsolation(LiveServerTestCase):
    """Announcement/timer audio reaches only the named device (flags: test_handler_wiring)."""

    async def connect(self, device):
        ws = await websockets.connect(self.url(f"?device_id={device}"))
        await asyncio.wait_for(ws.recv(), 5)  # hello
        return ws

    async def test_audio_stays_on_its_device(self):
        kitchen = await self.connect("kitchen")
        office = await self.connect("office")
        try:
            await self.wait_for_ids(["kitchen", "office"])
            self.assertTrue(await self.handler.send_bytes_to(b"\x01\x00" * 8, "office"))
            got = await asyncio.wait_for(office.recv(), 5)
            self.assertEqual(got, b"\x01\x00" * 8)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(kitchen.recv(), 0.3)
            self.assertFalse(await self.handler.send_bytes_to(b"\x00\x00", "bedroom"),
                             "unknown room must not fall back to another device")
        finally:
            await kitchen.close()
            await office.close()


if __name__ == "__main__":
    unittest.main()
