"""End to end, in real time: the real GPT-Live service and pipeline as the
handler builds them with `speaker_owner` on, the real serializer and client,
and the fake device (tests/fake_device.py) on the other end of the socket,
its acks and interrupts going back through the serializer as JSON.

Short scenarios, both framings, checked with the same properties as the
owner-level harness (P1 whole or nothing, P2 no interleaving and the owner's
order, P5 one acknowledgement per turn, P7 outcomes equal the device's)."""
import asyncio
import json
import logging
import unittest
from unittest.mock import patch

import app.live_service as live_service_module
from app import speaker_owner as so
from app.device_config import DeviceDirectory
from app.device_registry import DeviceConnection
from app.live_service import OpenAILiveLLMService
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.turn_timeline import TurnTimeline
from app.voice_runtime import LiveConfig
from app.websocket_handler import WebSocketHandler

from tests import fake_device as fd
from tests import live_fixtures as fx
from tests.test_live_device_audio import DeviceSocket, ModelSocket

_OWNER_LOG = logging.getLogger("app.speaker_owner")
_OWNER_LOG_LEVEL = _OWNER_LOG.level


def setUpModule():
    _OWNER_LOG.setLevel(logging.WARNING)


def tearDownModule():
    _OWNER_LOG.setLevel(_OWNER_LOG_LEVEL)


class WiredDevice(fd.FakeDevice):
    """The fake device behind a socket: acks go back as JSON text frames."""

    def __init__(self, mode, serializer):
        super().__init__(mode, latency_s=0.002, jitter_s=0.002)
        self.serializer = serializer

    def _deliver(self, msg):
        if self._link_up:
            self.loop.create_task(self.serializer.deserialize(json.dumps(msg)))


class WiredSocket(DeviceSocket):
    def __init__(self, device):
        super().__init__()
        self.device = device

    async def send_bytes(self, data):
        await super().send_bytes(data)
        await self.device.send_bytes(data)

    async def send_text(self, data):
        await super().send_text(data)
        await self.device.send_text(data)


class Rig:
    """The handler-built pipeline for one device with the owner on."""

    def __init__(self, mode):
        self.mode = mode

    async def __aenter__(self):
        self.sockets = []

        async def fake_connect(uri, additional_headers=None, **_kwargs):
            socket = ModelSocket()
            self.sockets.append(socket)
            return socket
        self._patch = patch.object(live_service_module, "websocket_connect", fake_connect)
        self._patch.start()
        handler = self.handler = WebSocketHandler(output_lead_buffer_ms=600)
        handler.device_directory = DeviceDirectory.from_json(
            json.dumps([{"name": "testdevice", "speaker_owner": True}]))
        self.serializer = RawAudioSerializer("testdevice")
        self.device = WiredDevice(self.mode, self.serializer)
        socket = WiredSocket(self.device)
        connection = self.connection = DeviceConnection(
            device_id="testdevice", websocket=socket, serializer=self.serializer,
            profile=handler.device_directory.get("testdevice"))
        connection.transport = handler.create_transport(socket, self.serializer)
        connection.turn_timeline = TurnTimeline("testdevice")
        connection.speaker_owner = handler._build_speaker_owner(connection)
        self.owner = connection.speaker_owner
        self.device.owner = self.owner
        self.device._ticker = self.device.loop.call_soon(self.device._tick)
        service = self.service = OpenAILiveLLMService(
            api_key="sk-test", config=LiveConfig(voice="marin"), instructions="Be brief.",
            backend_instructions="Use tools.", tools=[], fill_silence=False,
            admit_complete_replies=False)
        service.turn_liveness = TurnLiveness()
        service.device_id = "testdevice"
        connection.openai_service = service
        handler.devices._devices["testdevice"] = connection
        pipeline, runner, self.task = handler.build_pipeline(connection)
        self.run = asyncio.create_task(runner.run(self.task))
        for _ in range(500):
            if self.sockets and self.sockets[-1].started.is_set():
                break
            await asyncio.sleep(0.01)
        self.sockets[-1].push(fx.session_started())
        for _ in range(500):
            if service._session_started:
                break
            await asyncio.sleep(0.01)
        caps = ["ireason1", "utt1"] if self.mode == so.UTT else []
        await self.serializer.deserialize(json.dumps({"type": "start", "caps": caps}))
        self.keys = {}
        return self

    async def speak(self, key, ms, cadence=0.095):
        """Model speech with real-time cadence, then the idle run that ends it."""
        pcm = fd.tagged_pcm(key, ms)
        for delta in fx.output_audio_deltas(pcm):
            self.sockets[-1].push(delta)
            await asyncio.sleep(cadence)
        for delta in fx.output_audio_deltas(fx.silence(1000)):
            self.sockets[-1].push(delta)
            await asyncio.sleep(cadence)
        return pcm

    async def __aexit__(self, *exc):
        try:
            self.device.stop()
            await asyncio.wait_for(self.task.cancel(), 5)
            await asyncio.wait_for(self.run, 5)
            await self.owner.shutdown()
        finally:
            self._patch.stop()


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def _timer_during_reply(self, mode):
        async with Rig(mode) as rig:
            reply = asyncio.create_task(rig.speak(20, 1500))
            await asyncio.sleep(0.5)
            announce_pcm = fd.tagged_pcm(21, 1200)
            announced = asyncio.create_task(rig.handler.play_owned(
                announce_pcm, "testdevice", so.ANNOUNCE, wait="finished", keep=True))
            await reply
            second = await rig.speak(22, 800)
            self.assertTrue(await asyncio.wait_for(announced, 10))
            await asyncio.sleep(2.5)
            heard, runs, span = fd.heard_by_key(SimpleResult(rig))
            order = []
            for key, _t, _n in runs:
                if not order or order[-1] != key:
                    order.append(key)
            self.assertEqual(order, [20, 21, 22], "the timer line waits for the boundary")
            self.assertEqual(heard[20], fd.tagged_pcm(20, 1500), "P1: the reply whole")
            self.assertEqual(heard[21], announce_pcm, "P1: the announcement whole")
            self.assertEqual(heard[22], second, "P1: the next reply whole")
            if mode == so.UTT:
                for utt in rig.owner.history():
                    rec = rig.device.records.get(utt.id)
                    self.assertIsNotNone(rec)
                    if rec.state in ("queued", "playing"):
                        continue
                    self.assertEqual((utt.state, utt.fed), (rec.state, rec.fed), "P7")

    async def test_timer_during_reply_utt1(self):
        await self._timer_during_reply(so.UTT)

    async def test_timer_during_reply_legacy(self):
        await self._timer_during_reply(so.LEGACY)

    async def test_stop_mid_reply_utt1(self):
        async with Rig(so.UTT) as rig:
            reply = asyncio.create_task(rig.speak(30, 3000))
            await asyncio.sleep(1.2)
            rig.device.interrupt("stop")
            await reply
            await asyncio.sleep(0.5)
            utt = rig.owner.history()[0]
            rec = rig.device.records[utt.id]
            self.assertEqual((utt.state, utt.reason), (so.CUT, "stop"))
            self.assertEqual(utt.fed, rec.fed, "P7: the add-on knows exactly what played")
            heard, _runs, _span = fd.heard_by_key(SimpleResult(rig))
            self.assertEqual(len(heard[30]), rec.fed, "P1: a prefix ending at the stop")
            self.assertIn({"type": "interrupt_ack", "seq": 1}, rig.device.received_text)
            # The device accepts audio again after the ack. (Model speech
            # stays suppressed until the user speaks, as before the owner.)
            again = fd.tagged_pcm(31, 600)
            self.assertTrue(await rig.handler.play_owned(again, "testdevice", so.ANNOUNCE,
                                                         wait="finished"))
            heard, _runs, _span = fd.heard_by_key(SimpleResult(rig))
            self.assertEqual(heard.get(31), again)

    async def test_one_acknowledgement_per_turn(self):
        async with Rig(so.UTT) as rig:
            rig.connection.turn_timeline.begin("t1")
            reply = asyncio.create_task(rig.speak(40, 1200))
            await asyncio.sleep(0.3)
            clip = await rig.handler.play_owned(fd.tagged_pcm(41, 800), "testdevice", so.CLIP)
            await reply
            await asyncio.sleep(1.5)
            self.assertFalse(clip, "a clip cannot start inside a reply: dropped, never on top")
            heard, _runs, _span = fd.heard_by_key(SimpleResult(rig))
            self.assertNotIn(41, heard)


class SimpleResult:
    def __init__(self, rig):
        self.device = rig.device


if __name__ == "__main__":
    unittest.main()
