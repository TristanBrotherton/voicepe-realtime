"""The output recording holds what the device was actually sent.

voice-pe#22: under GPT-Live the output WAV did not match what the device
played. The recorder was a pipeline processor, so audio sent around the
pipeline (spoken prompts, the fallback acknowledgement clip, announcements)
never reached it, and a file read while recording claimed zero samples. Output
is now recorded at the device socket, for every binary frame actually sent.
"""
import asyncio
import os
import tempfile
import unittest
import wave

from starlette.websockets import WebSocketState

from app.audio_recorder import AudioRecorder
from app.audio_recording_service import AudioFrameRecorder, AudioRecordingService
from app.multi_client_transport import MixedFastAPIWebsocketClient
from app.wake_events import CaptureConfig, WakeEventStore
from app.websocket_handler import WebSocketHandler

from tests.test_handler_wiring import build_runnable
from tests.test_live_device_audio import DELTAS, EXPECTED, DevicePath


def _wav(path):
    with wave.open(path, "rb") as wav:
        return wav.getframerate(), wav.getnchannels(), wav.readframes(wav.getnframes())


def _only(directory, prefix):
    names = [n for n in os.listdir(directory) if n.startswith(prefix)]
    assert len(names) == 1, names
    return os.path.join(directory, names[0])


class TestRecordedAtTheSocket(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.recording = AudioRecordingService(enable_recording=True, output_dir=self.tmp.name)
        self.recording.start_new_session("kitchen")

    async def asyncTearDown(self):
        self.recording.cleanup()
        self.tmp.cleanup()

    async def test_reply_and_out_of_band_audio_are_both_recorded(self):
        """A Live reply through the real output transport, then a prompt sent
        straight to the socket (as _play_prompt and send_bytes_to do): the
        recording is exactly the bytes the device received, in order."""
        prompt = b"\x22\x01" * 2400  # 100 ms acknowledgement clip
        async with DevicePath() as path:
            path.transport.client.on_audio_sent = self.recording.record_output
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            await path.transport.client.send(prompt)
            sent = path.device.pcm
        self.assertEqual(sent, EXPECTED + prompt)
        output = _only(self.tmp.name, "output_kitchen_")
        # Read while still recording: the header already states the length.
        rate, channels, frames = _wav(output)
        self.assertEqual((rate, channels), (24000, 1))
        self.assertEqual(frames, sent, "the recording is what the device was sent")
        self.recording.stop_recording()
        self.assertEqual(_wav(output)[2], sent)

    async def test_text_and_unsent_frames_are_not_recorded(self):
        class Socket:
            client_state = WebSocketState.CONNECTED
            application_state = WebSocketState.CONNECTED

            def __init__(self):
                self.sent = []

            async def send_bytes(self, data):
                self.sent.append(data)

            async def send_text(self, data):
                self.sent.append(data)

        class Callbacks:
            async def on_client_disconnected(self, _websocket):
                pass

        socket = Socket()
        client = MixedFastAPIWebsocketClient(socket, Callbacks())
        recorded = []
        client.on_audio_sent = recorded.append
        await client.send('{"type":"phase"}')
        await client.send(b"\x01\x00" * 4)
        self.assertEqual(recorded, [b"\x01\x00" * 4], "only binary audio is recorded")
        socket.client_state = WebSocketState.DISCONNECTED
        await client.send(b"\x02\x00" * 4)
        self.assertEqual(recorded, [b"\x01\x00" * 4], "audio never sent is never recorded")

    async def test_a_failing_tap_never_costs_the_device_its_audio(self):
        class Socket:
            client_state = WebSocketState.CONNECTED
            application_state = WebSocketState.CONNECTED
            sent = []

            async def send_bytes(self, data):
                self.sent.append(data)

        class Callbacks:
            pass

        client = MixedFastAPIWebsocketClient(Socket(), Callbacks())

        def broken(_audio):
            raise OSError("disk full")

        client.on_audio_sent = broken
        await client.send(b"\x01\x00")
        await client.send(b"\x02\x00")
        self.assertEqual(Socket.sent, [b"\x01\x00", b"\x02\x00"])


class TestHeaderWhileRecording(unittest.TestCase):
    def test_a_file_read_mid_recording_states_its_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = AudioRecorder(output_dir=tmp)
            recorder.start_recording("kitchen")
            output = _only(tmp, "output_")
            self.assertEqual(os.path.getsize(output), 44, "a new file is a valid empty WAV")
            recorder.record_output_audio(b"\x05\x00" * 24000)
            recorder.record_output_audio(b"\x06\x00" * 12000)
            rate, _channels, frames = _wav(output)
            self.assertEqual(len(frames) / 2 / rate, 1.5)
            recorder.record_output_audio(b"\x07\x00" * 2400)
            self.assertEqual(len(_wav(output)[2]), 2 * (24000 + 12000 + 2400),
                             "later writes append after the data, not over the header")
            recorder.stop_recording()
            self.assertEqual(len(_wav(output)[2]), 2 * (24000 + 12000 + 2400))


class TestHandlerWiresTheSocketTap(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.recording = AudioRecordingService(enable_recording=True, output_dir=self.tmp.name)
        store = WakeEventStore(CaptureConfig(mode="off", probe_dir=self.tmp.name), instance="r")
        self.handler = WebSocketHandler(wake_events=store, audio_recording_service=self.recording)
        self.handler.WEDGE_TIMEOUT_S = 60

    async def asyncTearDown(self):
        for task in list(self.handler._background):
            task.cancel()
        self.recording.cleanup()
        self.tmp.cleanup()

    async def test_output_is_recorded_at_the_socket_not_in_the_pipeline(self):
        built = []
        build_pipeline = self.handler.build_pipeline

        def keep(connection, *args):
            result = build_pipeline(connection, *args)
            built.append(result[0])
            return result

        self.handler.build_pipeline = keep
        kitchen = build_runnable(self.handler, "kitchen")
        recorders = [p for p in built[0]._processors if isinstance(p, AudioFrameRecorder)]
        self.assertEqual([r.frame_type.__name__ for r in recorders], ["InputAudioRawFrame"],
                         "output audio is not recorded inside the pipeline")
        tap = kitchen.transport.client.on_audio_sent
        self.assertIsNotNone(tap)
        tap(b"\x03\x00" * 2400)
        output = _only(self.tmp.name, "output_kitchen_")
        self.assertEqual(_wav(output)[2], b"\x03\x00" * 2400)

    async def test_a_second_device_is_not_recorded_and_a_released_owner_stops(self):
        kitchen = build_runnable(self.handler, "kitchen")
        office = build_runnable(self.handler, "office")
        self.assertIsNone(getattr(office.transport.client, "on_audio_sent", None))
        tap = kitchen.transport.client.on_audio_sent
        self.handler._release_recording("kitchen")
        den = build_runnable(self.handler, "den")
        tap(b"\x04\x00" * 100)  # a late frame on the departed connection
        den.transport.client.on_audio_sent(b"\x05\x00" * 100)
        _rate, _ch, frames = _wav(_only(self.tmp.name, "output_den_"))
        self.assertEqual(frames, b"\x05\x00" * 100, "the old owner never writes into the new file")


if __name__ == "__main__":
    unittest.main()
