"""Real build_pipeline wiring: device messages reach the timeline and label store.

Builds the production pipeline for two fake device connections (a FrameProcessor
stands in for the OpenAI service; nothing is run or contacted) and drives the
serializer exactly as device frames would.
"""
import asyncio
import json
import tempfile
import unittest

from pipecat.processors.frame_processor import FrameProcessor

from app.device_registry import DeviceConnection
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.turn_timeline import TurnTimeline
from app.wake_events import CaptureConfig, WakeAudioCapture, WakeEventStore
from app.websocket_handler import WebSocketHandler


class FakeService(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._register_event_handler("on_conversation_item_created")
        self.events = []
        self._current_assistant_response = None
        self.turn_timeline = None
        self.resets = 0

        class Socket:
            async def ping(self_inner):
                loop = asyncio.get_running_loop()
                waiter = loop.create_future()
                waiter.set_result(0.0)
                return waiter

        self._websocket = Socket()

    async def send_client_event(self, event):
        self.events.append(type(event).__name__)

    async def reset_conversation(self):
        self.resets += 1


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class FakeTransport:
    def __init__(self):
        self.client = FakeClient()
        self._input = FrameProcessor()
        self._output = FrameProcessor()

    def input(self):
        return self._input

    def output(self):
        return self._output


def build(handler, device_id):
    serializer = RawAudioSerializer(device_id)
    connection = DeviceConnection(device_id=device_id, websocket=object(), serializer=serializer)
    connection.transport = FakeTransport()
    connection.turn_timeline = TurnTimeline(device_id)
    connection.wake_capture = WakeAudioCapture(handler.wake_events)
    serializer.add_audio_tap(connection.wake_capture)
    connection.turn_liveness = TurnLiveness()
    connection.openai_service = FakeService()
    handler.build_pipeline(connection)
    return connection


class TestHandlerWiring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WakeEventStore(CaptureConfig(mode="metadata", probe_dir=self.tmp.name), instance="w")
        self.handler = WebSocketHandler(wake_events=self.store)
        self.handler.WEDGE_TIMEOUT_S = 60  # keep the silent-wake check out of the way
        self.kitchen = build(self.handler, "kitchen")
        self.office = build(self.handler, "office")

    async def asyncTearDown(self):
        for task in list(self.handler._background):
            task.cancel()
        for connection in (self.kitchen, self.office):
            await self.handler._teardown(connection)
        self.tmp.cleanup()

    async def send(self, connection, obj):
        await connection.serializer.deserialize(json.dumps(obj))

    async def test_wake_opens_a_turn_with_metadata_and_records_the_event(self):
        await self.send(self.kitchen, {"type": "wake", "v": 2, "turn": "k1", "model": "hey_leonard",
                                       "cutoff_uint8": 163, "window": 3, "src": "wake_word"})
        turn = self.kitchen.turn_timeline.current
        self.assertEqual(turn.turn_id, "k1")
        self.assertEqual(turn.meta["window"], 3)
        event = self.store.find("kitchen", "k1")
        self.assertEqual((event.model, event.window), ("hey_leonard", 3))
        self.assertAlmostEqual(event.cutoff, 0.639, places=3)

    async def test_first_mic_frame_is_acked_and_stamped(self):
        await self.send(self.kitchen, {"type": "wake", "turn": "k2"})
        await self.kitchen.serializer.deserialize(b"\x00\x00" * 160)
        self.assertIn('{"type":"ack"}', self.kitchen.transport.client.sent)
        self.assertIn("first_audio_frame", self.kitchen.turn_timeline.current.stamps)

    async def test_double_press_labels_only_this_rooms_wake(self):
        await self.send(self.kitchen, {"type": "wake", "turn": "k3"})
        await self.send(self.office, {"type": "wake", "turn": "o3"})
        await self.send(self.kitchen, {"type": "false_flag", "turn": "k3"})
        self.assertEqual(self.store.find("kitchen", "k3").label, "false_wake")
        self.assertEqual(self.store.find("office", "o3").label, "")
        self.assertEqual(self.kitchen.turn_timeline.find("k3").outcome, "false_wake")

    async def test_double_press_without_turn_is_bounded_to_this_device(self):
        await self.send(self.office, {"type": "wake", "turn": "o4"})
        await self.send(self.kitchen, {"type": "false_flag"})
        self.assertEqual(self.store.find("office", "o4").label, "", "kitchen flag must not hit office")

    async def test_device_turn_metrics_merge_into_the_timeline(self):
        await self.send(self.kitchen, {"type": "wake", "turn": "k5"})
        self.kitchen.turn_timeline.finish("replied")
        await self.send(self.kitchen, {"type": "turn_metrics", "turn": "k5", "fire_to_mic_ms": 1290,
                                       "first_audio_to_audible_ms": 205})
        self.assertEqual(self.kitchen.turn_timeline.find("k5").device["fire_to_mic_ms"], 1290)

    async def test_interrupt_before_speech_finishes_turn_as_no_speech(self):
        await self.send(self.kitchen, {"type": "wake", "turn": "k6"})
        await self.send(self.kitchen, {"type": "interrupt"})
        self.assertEqual(self.kitchen.turn_timeline.find("k6").outcome, "no_speech")
        self.assertIn("InputAudioBufferClearEvent", self.kitchen.openai_service.events)

    async def test_live_socket_is_not_reconnected_at_wake(self):
        await self.send(self.kitchen, {"type": "wake", "turn": "k7"})
        await asyncio.sleep(0.05)
        self.assertEqual(self.kitchen.openai_service.resets, 0)

    async def test_hello_advertises_protocol_and_capture_consent(self):
        hello = self.handler.hello_payload()
        self.assertEqual(hello["proto"], 2)
        self.assertEqual(hello["trigger_capture"], 0, "metadata mode never asks for trigger audio")


if __name__ == "__main__":
    unittest.main()
