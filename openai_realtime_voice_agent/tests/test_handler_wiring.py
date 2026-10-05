"""Real build_pipeline wiring: device messages reach the timeline and label store.

Builds the production pipeline for two fake device connections (a FrameProcessor
stands in for the OpenAI service; nothing is run or contacted) and drives the
serializer exactly as device frames would.
"""
import asyncio
import json
import os
import tempfile
import unittest

from loguru import logger as loguru_logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor

from app.audio_recording_service import AudioRecordingService
from app.device_registry import DeviceConnection
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.turn_timeline import TurnTimeline
from app.wake_events import CaptureConfig, WakeAudioCapture, WakeEventStore, weekly_report
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

    async def test_firmware_before_protocol_2_still_works(self):
        # Devices not yet reflashed send bare messages: no turn id, no model
        # metadata, no turn_metrics. Turns, labels and acks must still work.
        await self.send(self.kitchen, {"type": "wake"})
        turn = self.kitchen.turn_timeline.current
        self.assertTrue(turn.turn_id, "a generated turn id")
        self.assertEqual(turn.meta, {})
        await self.kitchen.serializer.deserialize(b"\x00\x00" * 160)
        self.assertIn('{"type":"ack"}', self.kitchen.transport.client.sent)
        await self.send(self.kitchen, {"type": "button_cancel"})
        self.assertEqual(self.store.find("kitchen", turn.turn_id).label, "false_wake")
        await self.send(self.kitchen, {"type": "wake"})
        second = self.kitchen.turn_timeline.current
        await self.send(self.kitchen, {"type": "false_flag"})
        self.assertEqual(self.store.find("kitchen", second.turn_id).label, "false_wake")
        self.assertEqual(self.store.find("kitchen", second.turn_id).label_method, "double_press")

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

    async def test_shadow_detection_is_metadata_only(self):
        await self.send(self.kitchen, {"type": "shadow_detection", "model": "hey_leonard_candidate"})
        log = os.path.join(self.tmp.name, "meta", "events-w.jsonl")
        report = weekly_report([log])
        self.assertEqual(report["devices"]["kitchen"]["shadow_detections"], {"hey_leonard_candidate": 1})
        self.assertEqual(report["devices"]["kitchen"]["wakes"], 0, "shadow fires are not wakes")
        self.assertIsNone(self.kitchen.turn_timeline.current, "no turn starts")

    async def test_hello_advertises_protocol_and_capture_consent(self):
        hello = self.handler.hello_payload()
        self.assertEqual(hello["proto"], 2)
        self.assertEqual(hello["trigger_capture"], 0, "metadata mode never asks for trigger audio")


class PassThrough(FrameProcessor):
    """Forwards every frame, like a real transport end would."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        await self.push_frame(frame, direction)


class ForwardingService(FakeService):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class RunnableTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self._input = PassThrough()
        self._output = PassThrough()


def build_runnable(handler, device_id):
    serializer = RawAudioSerializer(device_id)
    connection = DeviceConnection(device_id=device_id, websocket=object(), serializer=serializer)
    connection.transport = RunnableTransport()
    connection.turn_timeline = TurnTimeline(device_id)
    connection.wake_capture = WakeAudioCapture(handler.wake_events)
    connection.turn_liveness = TurnLiveness()
    connection.openai_service = ForwardingService()
    _pipeline, runner, task = handler.build_pipeline(connection)
    connection.runner, connection.task = runner, task
    return connection


class TestDisplacedSessionWithRecording(unittest.IsolatedAsyncioTestCase):
    """A device reconnecting over its own half-open session, recording on.

    Observed live after every OTA reboot: the replacement pipeline reused the
    recorder processors of the one being cancelled, so the old pipeline's
    frames and CancelFrame ran into processors that had not started (~490
    "_FrameProcessor__input_queue" errors, a RecursionError), and cancelling
    the old pipeline hung for pipecat's full 20 s cancel timeout.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.recording = AudioRecordingService(enable_recording=True, output_dir=self.tmp.name)
        store = WakeEventStore(CaptureConfig(mode="off", probe_dir=self.tmp.name), instance="d")
        self.handler = WebSocketHandler(wake_events=store, audio_recording_service=self.recording)
        self.handler.WEDGE_TIMEOUT_S = 60
        self.errors = []
        self.sink = loguru_logger.add(lambda message: self.errors.append(str(message)), level="ERROR")

    async def asyncTearDown(self):
        loguru_logger.remove(self.sink)
        for task in list(self.handler._background):
            task.cancel()
        self.recording.cleanup()
        self.tmp.cleanup()

    def test_each_pipeline_gets_its_own_recorders(self):
        self.assertIsNot(self.recording.get_input_recorder(), self.recording.get_input_recorder())
        self.assertIsNot(self.recording.get_output_recorder(), self.recording.get_output_recorder())

    async def test_displaced_pipeline_cancels_promptly_and_cleanly(self):
        old = build_runnable(self.handler, "kitchen")
        running = asyncio.create_task(old.runner.run(old.task))
        await asyncio.wait_for(old.transport.output().started.wait(), 5)
        frame = InputAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=16000, num_channels=1)
        await old.task.queue_frames([frame] * 10)
        await asyncio.sleep(0.1)

        # The same device reconnects before its old socket is noticed dead.
        new = build_runnable(self.handler, "kitchen")
        self.assertTrue(new.records_audio, "the reconnecting device keeps recording")

        await asyncio.wait_for(old.task.cancel(), 5)
        await asyncio.wait_for(running, 5)
        leaked = [e for e in self.errors if "_FrameProcessor__input_queue" in e or "recursion" in e]
        self.assertEqual(leaked, [], "old pipeline frames reached the replacement pipeline")
        self.assertFalse(new.transport.output().started.is_set(), "replacement pipeline was touched")


if __name__ == "__main__":
    unittest.main()
