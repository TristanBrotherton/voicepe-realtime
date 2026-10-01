"""D-80: a device that reconnects while its old pipeline is being cancelled
must get a pipeline that hears it.

Live 2026-10-01 (kontoret): comms restarted, the device reconnected while
PipelineTask#2 was cancelling, and from then on every wake streamed audio
that never reached OpenAI. The journal shows the cause:
`OutputLeadBuffer#3 Trying to process CancelFrame#2` -- the OLD pipeline's
CancelFrame walked into the NEW pipeline. AudioRecordingService handed the
same two AudioFrameRecorder instances (AudioFrameRecorder#0/#1) to every
pipeline, so building the new pipeline relinked them into it, and the old
CancelFrame set `_cancelling = True` on them -- which pipecat never resets,
so `queue_frame` drops every later frame, including the device's audio.

Real Pipeline/PipelineTask/PipelineRunner; only the device and the model
are replaced by a collector at the end of the pipeline.
"""
import asyncio
import tempfile
import unittest

from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.audio_recording_service import AudioRecordingService


class Collector(FrameProcessor):
    """Stands in for the realtime service: records the audio it receives."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.audio = asyncio.Event()

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        if isinstance(frame, InputAudioRawFrame):
            self.audio.set()
        await self.push_frame(frame, direction)


def build(service: AudioRecordingService):
    """The recorder part of WebSocketHandler.build_pipeline, as it wires it."""
    collector = Collector()
    pipeline = Pipeline([
        service.get_input_recorder(),
        collector,
        service.get_output_recorder(),
    ])
    return PipelineTask(pipeline, idle_timeout_secs=None, cancel_on_idle_timeout=False), collector


class TestReconnectDuringTeardown(unittest.IsolatedAsyncioTestCase):
    async def test_new_pipeline_hears_audio_after_old_one_is_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AudioRecordingService(enable_recording=True, output_dir=tmp)
            runner = PipelineRunner(handle_sigint=False)

            old_task, old_collector = build(service)
            old_run = asyncio.create_task(runner.run(old_task))
            await asyncio.wait_for(old_collector.started.wait(), 5)
            await asyncio.sleep(0.2)  # StartFrame reaches the end; the old pipeline is live

            # The device reconnects: the new pipeline is built, and the old
            # one is cancelled while the new one starts -- the live order.
            new_task, new_collector = build(service)
            cancelling = asyncio.create_task(old_task.cancel())
            new_run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(new_task))
            await asyncio.wait_for(new_collector.started.wait(), 5)
            await asyncio.sleep(0.2)  # let the old CancelFrame finish its walk

            await new_task.queue_frame(InputAudioRawFrame(
                audio=b"\0" * 960, sample_rate=24000, num_channels=1))
            try:
                await asyncio.wait_for(new_collector.audio.wait(), 2)
                heard = True
            except asyncio.TimeoutError:
                heard = False

            # Teardown is bounded: on the broken code the old task waits for
            # a CancelFrame that the shared recorder swallowed.
            for t in (cancelling, old_run, new_run):
                t.cancel()
            await asyncio.wait({cancelling, old_run, new_run}, timeout=2)
            service.cleanup()

            self.assertTrue(heard, "device audio never reached the model after reconnect")


if __name__ == "__main__":
    unittest.main()
