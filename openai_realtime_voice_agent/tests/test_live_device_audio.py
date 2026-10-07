"""End-to-end: GPT-Live output audio as the Voice PE actually receives it.

The canary's reply was unintelligible at the speaker while every component
looked healthy in isolation, so this test refuses to stop at a frame boundary.
It replays the protocol-faithful ``session.output_audio.delta`` sequence into
the real ``OpenAILiveLLMService``, through the real ``OutputLeadBuffer``, the
real pipecat ``FastAPIWebsocketOutputTransport`` (which owns the output
resampling and the 10 ms chunking) and the real ``RawAudioSerializer``, and
then asserts on the exact bytes handed to the device socket:

* PCM16, mono, 24 kHz, even byte count, every chunk a whole number of samples;
* the concatenation is byte-identical to the audible part of the model output,
  in order, with nothing re-sampled, duplicated, dropped or spliced;
* each speech segment decodes to its exact waveform with no discontinuity;
* the relay hands the device a playout lead before the first word, because
  GPT-Live streams at about real time and the device cannot build one itself.
"""
import array
import asyncio
import json
import math
import unittest
from unittest.mock import patch

from pipecat.frames.frames import StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from starlette.websockets import WebSocketState

import app.live_service as live_service_module
from app.live_service import OpenAILiveLLMService
from app.multi_client_transport import MixedFastAPIWebsocketTransport
from app.output_lead_buffer import OutputLeadBuffer
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.turn_timeline import TurnTimeline
from app.voice_runtime import LIVE_OUTPUT_LEAD_MS, LiveConfig
from app.websocket_handler import PIPELINE_SAMPLE_RATE

from tests import live_fixtures as fx


class DeviceSocket:
    """A starlette WebSocket stand-in that records what the device would get."""

    def __init__(self) -> None:
        self.binary = []
        self.text = []
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self._inbound: asyncio.Queue = asyncio.Queue()

    async def receive(self):
        return await self._inbound.get()

    async def send_bytes(self, data):
        self.binary.append(bytes(data))

    async def send_text(self, data):
        self.text.append(data)

    async def close(self, code=1000):
        self.client_state = WebSocketState.DISCONNECTED

    async def iter_bytes(self):
        while True:
            message = await self._inbound.get()
            if message.get("bytes") is not None:
                yield message["bytes"]

    async def iter_text(self):
        while True:
            message = await self._inbound.get()
            if message.get("text") is not None:
                yield message["text"]

    @property
    def pcm(self) -> bytes:
        return b"".join(self.binary)


class ModelSocket:
    """The OpenAI side: records client events, replays server events."""

    def __init__(self) -> None:
        self.sent = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.started = asyncio.Event()

    async def send(self, data):
        payload = json.loads(data)
        self.sent.append(payload)
        if payload["type"] == "session.start":
            self.started.set()

    async def close(self):
        self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def push(self, event):
        self.incoming.put_nowait(json.dumps(event))


class Ready(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        await self.push_frame(frame, direction)


class DevicePath:
    """service -> OutputLeadBuffer -> real websocket output transport -> socket."""

    def __init__(self, lead_ms: int = LIVE_OUTPUT_LEAD_MS, tail=None) -> None:
        self.lead_ms = lead_ms
        # Optional processor placed AFTER transport.output(), so a test can see
        # the speaking/control frames the output transport itself generates.
        self.tail = tail
        self.model_sockets = []

    async def __aenter__(self):
        async def fake_connect(uri, additional_headers=None, **_kwargs):
            socket = ModelSocket()
            self.model_sockets.append(socket)
            return socket

        self._patch = patch.object(live_service_module, "websocket_connect", fake_connect)
        self._patch.start()
        self.device = DeviceSocket()
        self.serializer = RawAudioSerializer("kitchen")
        self.transport = MixedFastAPIWebsocketTransport(
            websocket=self.device,
            params=FastAPIWebsocketParams(
                serializer=self.serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
                audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
            ),
        )
        self.service = OpenAILiveLLMService(
            api_key="sk-test", config=LiveConfig(voice="marin"),
            instructions="Be brief.", backend_instructions="Use tools.",
            tools=[], fill_silence=False,
        )
        self.service.turn_timeline = TurnTimeline("kitchen")
        self.service.turn_liveness = TurnLiveness()
        self.service.device_id = "kitchen"
        self.ready = Ready()
        stages = [
            self.ready, self.service,
            OutputLeadBuffer(lead_ms=self.lead_ms),
            self.transport.output(),
        ]
        if self.tail is not None:
            stages.append(self.tail)
        self.pipeline = Pipeline(stages)
        self.task = PipelineTask(self.pipeline, idle_timeout_secs=None,
                                 cancel_on_idle_timeout=False)
        self.runner = PipelineRunner(handle_sigint=False)
        self.run = asyncio.create_task(self.runner.run(self.task))
        await asyncio.wait_for(self.ready.started.wait(), 5)
        await asyncio.wait_for(self._model_ready(), 5)
        self.model.push(fx.session_started())
        await asyncio.wait_for(self._session_started(), 5)
        return self

    async def _model_ready(self):
        while not self.model_sockets:
            await asyncio.sleep(0.005)
        await self.model_sockets[-1].started.wait()

    async def _session_started(self):
        while not self.service._session_started:
            await asyncio.sleep(0.005)

    @property
    def model(self) -> ModelSocket:
        return self.model_sockets[-1]

    async def replay(self, deltas, cadence: float = 0.0):
        """Push output audio deltas, optionally at their real-time cadence."""
        for delta in deltas:
            self.model.push(delta)
            if cadence:
                await asyncio.sleep(cadence)

    async def drain(self, expected_bytes: int, timeout: float = 5.0):
        async def _wait():
            while len(self.device.pcm) < expected_bytes:
                await asyncio.sleep(0.005)
        try:
            await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            pass
        # Let any trailing chunk settle so a "too much arrived" bug is visible.
        await asyncio.sleep(0.2)

    async def __aexit__(self, *exc):
        try:
            await asyncio.wait_for(self.task.cancel(), 5)
            await asyncio.wait_for(self.run, 5)
        finally:
            self._patch.stop()


PLAN = fx.SHORT_PLAN
DELTAS = fx.output_audio_deltas(fx.reply_pcm(PLAN))
EXPECTED = fx.expected_forwarded(PLAN)
# pipecat writes to the device in whole audio_out_10ms_chunks: 4 x 10 ms of
# mono PCM16 at 24 kHz = 1,920 bytes = 40 ms per socket write.
CHUNK_BYTES = 1920


class TestDeviceAudio(unittest.IsolatedAsyncioTestCase):
    async def test_the_device_receives_the_model_audio_byte_for_byte(self):
        async with DevicePath() as path:
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            pcm = path.device.pcm
            self.assertEqual(len(pcm), len(EXPECTED),
                             "the device must receive every audible byte, and no others")
            self.assertEqual(pcm, EXPECTED,
                             "audio was altered between the model and the device")

    async def test_the_delivered_stream_is_mono_pcm16_at_24k_and_aligned(self):
        async with DevicePath() as path:
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            self.assertEqual(PIPELINE_SAMPLE_RATE, 24000)
            self.assertEqual(len(path.device.pcm) % 2, 0, "whole 16-bit samples")
            for chunk in path.device.binary:
                self.assertEqual(len(chunk) % 2, 0, "every socket write is sample-aligned")
            # 10 ms of mono PCM16 at 24 kHz is 480 bytes; a stereo or 8-bit
            # regression anywhere in the chain shows up in these sizes.
            self.assertTrue(all(len(c) % 480 == 0 for c in path.device.binary))
            self.assertEqual(path.transport.output().sample_rate, 24000)
            self.assertEqual(path.transport.output().audio_chunk_size, CHUNK_BYTES)

    async def test_each_speech_segment_decodes_to_a_clean_waveform(self):
        async with DevicePath() as path:
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            pcm = path.device.pcm
            for offset, expected in fx.speech_spans(PLAN):
                self.assertEqual(pcm[offset:offset + len(expected)], expected,
                                 f"speech at byte {offset} was corrupted")
            samples = array.array("h")
            samples.frombytes(pcm)
            offset, loud = fx.speech_spans(PLAN)[0]
            begin, end = offset // 2, offset // 2 + len(loud) // 2
            # A clean 220 Hz sine at 24 kHz steps by at most ~2*pi*220/24000 of
            # its amplitude per sample. A splice or a one-byte shift is far
            # larger, so this bound catches both.
            bound = 6000 * 2 * math.pi * 220 / 24000 * 1.2
            worst = max(abs(samples[i] - samples[i - 1]) for i in range(begin + 1, end))
            self.assertLess(worst, bound, "a splice or byte shift reached the device")

    async def test_no_gap_or_reorder_across_socket_writes(self):
        """Chunk continuity: writes concatenate to one unbroken stream."""
        async with DevicePath() as path:
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            rebuilt = b"".join(path.device.binary)
            self.assertEqual(rebuilt, EXPECTED)
            self.assertEqual(len(path.device.binary), len(EXPECTED) // CHUNK_BYTES,
                             "every write is a full chunk; none is dropped or doubled")

    async def test_the_idle_tail_is_withheld_so_the_reply_ends(self):
        async with DevicePath() as path:
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            source = fx.reply_pcm(PLAN)
            self.assertLess(len(path.device.pcm), len(source))
            withheld_ms = (len(source) - len(path.device.pcm)) / (24000 * 2) * 1000
            self.assertAlmostEqual(withheld_ms, 800.0, delta=1.0)
            self.assertEqual(path.service.audio.metrics.silence_suppressed_deltas, 8)

    async def test_a_full_length_reply_arrives_intact(self):
        """The same guarantee on the full 8 s fixture, pacing included."""
        expected = fx.expected_forwarded(fx.REPLY_PLAN)
        async with DevicePath() as path:
            await path.replay(fx.output_audio_deltas(fx.reply_pcm(fx.REPLY_PLAN)))
            await path.drain(len(expected), timeout=20.0)
            pcm = path.device.pcm
            self.assertEqual(len(pcm), len(expected))
            self.assertEqual(pcm, expected)
            for offset, speech in fx.speech_spans(fx.REPLY_PLAN):
                self.assertEqual(pcm[offset:offset + len(speech)], speech)


class TestPlayoutLead(unittest.IsolatedAsyncioTestCase):
    """Why a lead is needed, and that it does not alter the audio.

    pipecat's websocket output transport writes to the device at exactly 1.0x
    real time from a monotonic clock, and when it falls behind (because no
    audio was available) it resets its schedule instead of catching up. A 1.0x
    source therefore passes every input stall straight through to the device as
    an equal-length output stall. GPT-Live is a 1.0x source with measured
    in-speech gaps to ~160 ms and session-wide spikes to ~380 ms, so the relay
    must keep a queue in front of that pacer.
    """

    async def test_the_relay_holds_a_lead_before_the_first_word(self):
        async with DevicePath(lead_ms=LIVE_OUTPUT_LEAD_MS) as path:
            lead_bytes = 24000 * 2 * LIVE_OUTPUT_LEAD_MS // 1000
            # Five 100 ms deltas: less than the 600 ms lead, so nothing is due.
            await path.replay(DELTAS[:5])
            await asyncio.sleep(0.3)
            self.assertEqual(path.device.pcm, b"",
                             "nothing is released until the lead is built")
            await path.replay(DELTAS[5:7])
            await path.drain(CHUNK_BYTES, timeout=2.0)
            self.assertGreaterEqual(path.service.audio.metrics.bytes_out, lead_bytes)
            self.assertGreater(len(path.device.pcm), 0, "the lead is released as a burst")

    async def test_a_source_stall_delivers_the_same_bytes_with_or_without_a_lead(self):
        """A 400 ms source stall mid-reply changes the timing, not the audio.

        What the stall does to the *device* — whether the transport still had
        audio queued when the 1.0x pacer came round — is asserted on write
        timestamps in ``tests/test_live_idle_to_speech.py``, which also keeps a
        lead-free control case so the bound there cannot pass vacuously.
        """
        stalled = list(DELTAS[:16])

        async def run(lead_ms):
            async with DevicePath(lead_ms=lead_ms) as path:
                for index, delta in enumerate(stalled):
                    path.model.push(delta)
                    # Real cadence, with one 400 ms stall after 800 ms of audio.
                    await asyncio.sleep(0.4 if index == 8 else 0.1)
                await path.drain(len(stalled) * 4800, timeout=6.0)
                return path.service.audio.metrics.bytes_out, path.device.pcm

        produced_lead, delivered_lead = await run(LIVE_OUTPUT_LEAD_MS)
        produced_bare, delivered_bare = await run(0)
        self.assertEqual(produced_lead, produced_bare, "the model sent the same audio")
        # 1,600 ms of SHORT_PLAN, none of it an idle run, so every byte is due.
        self.assertEqual(len(delivered_lead), produced_lead)
        self.assertEqual(delivered_lead, delivered_bare,
                         "the lead changed the bytes, not just their timing")

    async def test_the_lead_does_not_change_the_bytes(self):
        async with DevicePath(lead_ms=LIVE_OUTPUT_LEAD_MS) as path:
            await path.replay(DELTAS)
            await path.drain(len(EXPECTED))
            self.assertEqual(path.device.pcm, EXPECTED)

    async def test_without_a_lead_audio_goes_straight_out(self):
        """The canary's configuration, kept as the contrast case."""
        async with DevicePath(lead_ms=0) as path:
            # Audible from the first byte: a leading inaudible delta would be
            # held rather than forwarded, which is a different test.
            await path.replay(fx.output_audio_deltas(fx.tone(100, amplitude=6000)))
            await path.drain(2 * CHUNK_BYTES, timeout=2.0)
            # One 100 ms delta is two full 40 ms writes plus a 20 ms remainder
            # the transport holds: no cushion, it goes out as it arrives.
            self.assertEqual(len(path.device.pcm), 2 * CHUNK_BYTES)

    async def test_the_live_default_lead_is_configured(self):
        self.assertEqual(LIVE_OUTPUT_LEAD_MS, 600)
        self.assertGreater(LIVE_OUTPUT_LEAD_MS, 380,
                           "the worst inter-delta gap measured on the real endpoint")


class TestNegotiatedFormat(unittest.IsolatedAsyncioTestCase):
    async def test_a_mismatched_negotiated_format_is_refused_not_played(self):
        async with DevicePath() as path:
            errors = []

            async def record(error_msg="", **_kw):
                errors.append(error_msg)

            path.service.push_error = record
            await path.service._verify_negotiated_audio(
                {"audio": {"format": {"type": "audio/pcmu", "rate": 8000}}}
            )
            self.assertEqual(len(errors), 1)
            self.assertIn("audio format mismatch", errors[0])
            self.assertIn("audio/pcmu", errors[0])

    async def test_the_real_negotiated_format_is_accepted(self):
        async with DevicePath() as path:
            errors = []

            async def record(error_msg="", **_kw):
                errors.append(error_msg)

            path.service.push_error = record
            await path.service._verify_negotiated_audio(fx.session_started()["session"])
            self.assertEqual(errors, [])

    async def test_a_session_that_echoes_no_format_is_taken_at_its_word(self):
        async with DevicePath() as path:
            errors = []

            async def record(error_msg="", **_kw):
                errors.append(error_msg)

            path.service.push_error = record
            await path.service._verify_negotiated_audio({"audio": {"output": {"voice": "marin"}}})
            await path.service._verify_negotiated_audio({})
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
