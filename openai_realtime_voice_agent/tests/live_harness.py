"""A running GPT-Live service in a real pipecat pipeline, on a fake socket.

Shared by the Live test modules so they all exercise the same wiring: the real
``OpenAILiveLLMService``, the real pipeline task and runner, and server events
pushed in as the endpoint sends them.
"""
import asyncio
import json
from unittest.mock import patch

from pipecat.frames.frames import StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameProcessor

import app.live_service as live_service_module
from app.live_service import OpenAILiveLLMService
from app.phase_emitter import TurnLiveness
from app.turn_timeline import TurnTimeline
from app.voice_runtime import LiveConfig

from tests import live_fixtures as fx


class FakeSocket:
    """Enough of a websockets client connection for the service."""

    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False
        self.started = asyncio.Event()

    async def send(self, data):
        payload = json.loads(data)
        self.sent.append(payload)
        if payload["type"] == "session.start":
            self.started.set()

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)

    async def ping(self):
        waiter = asyncio.get_running_loop().create_future()
        waiter.set_result(0.0)
        return waiter

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def push(self, evt):
        self.incoming.put_nowait(json.dumps(evt))

    def push_all(self, events):
        for event in events:
            self.push(event)

    def types(self):
        return [m["type"] for m in self.sent]

    def of(self, kind):
        return [m for m in self.sent if m["type"] == kind]

    async def wait_for_sent(self, kind, count=1, timeout=2.0):
        async def _wait():
            while len(self.of(kind)) < count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(_wait(), timeout)


class Tap(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames = []
        self.started = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append((frame, direction))
        if isinstance(frame, StartFrame):
            self.started.set()
        await self.push_frame(frame, direction)

    def of(self, frame_cls, direction=None):
        return [f for f, d in self.frames
                if isinstance(f, frame_cls) and (direction is None or d == direction)]

    async def wait_for(self, frame_cls, count=1, direction=None, timeout=2.0):
        async def _wait():
            while len(self.of(frame_cls, direction)) < count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(_wait(), timeout)


class LiveHarness:
    """A running pipeline [source Tap, service, sink Tap] on a fake socket."""

    def __init__(self, test, **service_kwargs):
        self.test = test
        self.sockets = []
        self.service_kwargs = service_kwargs

    async def __aenter__(self):
        async def fake_connect(uri, additional_headers=None, **_kwargs):
            socket = FakeSocket()
            self.sockets.append(socket)
            return socket

        self._patch = patch.object(live_service_module, "websocket_connect", fake_connect)
        self._patch.start()
        kwargs = dict(api_key="sk-test", config=LiveConfig(voice="marin"),
                      instructions="Be brief.", backend_instructions="Use tools.",
                      tools=[], fill_silence=False)
        kwargs.update(self.service_kwargs)
        self.service = OpenAILiveLLMService(**kwargs)
        self.service.turn_timeline = TurnTimeline("kitchen")
        self.service.turn_liveness = TurnLiveness()
        self.service.device_id = "kitchen"
        self.source, self.sink = Tap(), Tap()
        self.pipeline = Pipeline([self.source, self.service, self.sink])
        self.task = PipelineTask(self.pipeline, idle_timeout_secs=None,
                                 cancel_on_idle_timeout=False)
        self.runner = PipelineRunner(handle_sigint=False)
        self.run = asyncio.create_task(self.runner.run(self.task))
        await asyncio.wait_for(self.sink.started.wait(), 5)
        await asyncio.wait_for(self._socket_ready(), 5)
        return self

    async def _socket_ready(self):
        while not self.sockets:
            await asyncio.sleep(0.005)
        await self.sockets[-1].started.wait()

    @property
    def socket(self) -> FakeSocket:
        return self.sockets[-1]

    async def start_session(self, session_id="sess_1", audio=None):
        self.socket.push(fx.session_started(session_id, audio))

        async def _wait():
            while not self.service._session_started:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(_wait(), 2)

    async def __aexit__(self, *exc):
        try:
            await asyncio.wait_for(self.task.cancel(), 5)
            await asyncio.wait_for(self.run, 5)
        finally:
            self._patch.stop()
