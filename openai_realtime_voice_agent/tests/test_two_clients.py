"""Two devices stay connected at once (multi-device acceptance test).

Runs the real WebSocketHandler.serve_connection over a real uvicorn server
with real WebSocket clients, stubbing only the OpenAI service and the
pipeline runner — connection lifecycle, registry, framing and routing are
genuine. Under the old single-client transport the second connect closed the
first socket.
"""
import asyncio
import contextlib
import json
import unittest

import uvicorn
import websockets

from app.main import Application
from app.websocket_handler import WebSocketHandler


class FakeOpenAIService:
    """Stands in for SafeRealtimeLLMService; records which device made it."""

    def __init__(self, device_id, registry):
        self.device_id = device_id
        self.disconnected = False
        registry.append(self)

    def event_handler(self, _name):
        def decorator(fn):
            return fn
        return decorator

    async def send_client_event(self, _evt):
        return None

    async def disconnect(self):
        self.disconnected = True


class LiveServerTestCase(unittest.IsolatedAsyncioTestCase):
    """A real uvicorn server running the add-on's FastAPI app on a free port."""

    handler_kwargs: dict = {}

    async def asyncSetUp(self):
        self.services = []
        self.handler = WebSocketHandler(host="127.0.0.1", port=0, follow_up_ms=1234, **self.handler_kwargs)

        async def factory(connection):
            return FakeOpenAIService(connection.device_id, self.services)

        self.handler.openai_service_factory = factory

        # Replace the pipeline with a stub that still runs the transport's
        # real read loop, so control frames and binary/text multiplexing are
        # genuinely exercised.
        def fake_build(connection, activity_callback=None):
            class Runner:
                async def run(self, _task):
                    async for message in connection.transport.client.receive():
                        await connection.serializer.deserialize(message)

            class Task:
                async def cancel(self):
                    return None

            return object(), Runner(), Task()

        self.handler.build_pipeline = fake_build
        app = Application()
        app.websocket_handler = self.handler
        app.session_manager = None
        self.configure_app(app)
        config = uvicorn.Config(
            app.build_web_app(), host="127.0.0.1", port=0, log_level="error", lifespan="off"
        )
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started, "uvicorn did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def configure_app(self, app):
        """Hook for subclasses to adjust the Application before serving."""

    async def asyncTearDown(self):
        self.server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self.server_task, 5)

    def url(self, query="", path="/"):
        return f"ws://127.0.0.1:{self.port}{path}{query}"

    async def wait_for_ids(self, expected, timeout=3.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.handler.devices.ids() == expected:
                return
            await asyncio.sleep(0.02)
        self.assertEqual(self.handler.devices.ids(), expected)


class TestTwoClients(LiveServerTestCase):
    async def test_two_devices_coexist_with_isolated_sessions(self):
        async with websockets.connect(self.url("?device_id=kitchen")) as a:
            hello_a = json.loads(await asyncio.wait_for(a.recv(), 5))
            self.assertEqual(hello_a["type"], "hello")
            self.assertEqual(hello_a["follow_up_ms"], 1234)

            async with websockets.connect(self.url("?device_id=office")) as b:
                self.assertEqual(json.loads(await asyncio.wait_for(b.recv(), 5))["type"], "hello")
                await self.wait_for_ids(["kitchen", "office"])

                # The first socket must still be alive and usable.
                await a.send(json.dumps({"type": "ping"}))
                self.assertEqual(json.loads(await asyncio.wait_for(a.recv(), 5)), {"type": "pong"})
                await b.send(json.dumps({"type": "ping"}))
                self.assertEqual(json.loads(await asyncio.wait_for(b.recv(), 5)), {"type": "pong"})

                # Independent sessions, not one shared session.
                self.assertEqual(sorted(s.device_id for s in self.services), ["kitchen", "office"])

                # Phases are unicast, not broadcast.
                await self.handler.devices.get("kitchen").send_phase("listening")
                self.assertEqual(
                    json.loads(await asyncio.wait_for(a.recv(), 5)),
                    {"type": "phase", "value": "listening"},
                )
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(b.recv(), 0.4)

                # Explicit ids resolve; unknown ids refuse to guess.
                self.assertEqual(self.handler.resolve_device("office").device_id, "office")
                self.assertIsNone(self.handler.resolve_device("bedroom"))

            await self.wait_for_ids(["kitchen"])
        await self.wait_for_ids([])
        self.assertTrue(all(s.disconnected for s in self.services), "sessions leaked")

    async def test_legacy_firmware_without_device_id_uses_client_ip(self):
        async with websockets.connect(self.url()) as legacy:
            self.assertEqual(json.loads(await asyncio.wait_for(legacy.recv(), 5))["type"], "hello")
            await self.wait_for_ids(["127.0.0.1"])
            await legacy.send(json.dumps({"type": "ping"}))
            self.assertEqual(json.loads(await asyncio.wait_for(legacy.recv(), 5)), {"type": "pong"})

    async def test_non_root_paths_stay_accepted(self):
        async with websockets.connect(self.url("?device_id=hall", path="/voice/pe")) as client:
            self.assertEqual(json.loads(await asyncio.wait_for(client.recv(), 5))["type"], "hello")
            await self.wait_for_ids(["hall"])


if __name__ == "__main__":
    unittest.main()
