"""The exact failure the second canary heard: idle silence, then speech.

The 2026-10-07 09:48 PDT reply was audibly static while every byte-level check
passed — 18/64 deltas forwarded, 86,400 bytes, ``odd-length 0``,
``undecodable 0``, ``backlog-dropped 0``, pace 0.981x, and the device reporting
``underrun 1`` with ``ws_gap_max_ms 4804``. Byte-integrity tests could not
reproduce it because the bytes were never wrong; the *timing* at the device was.

So these tests do not look at bytes alone. They replay a protocol-faithful
stream — multi-second idle silence first, then speech at 0.985x real time with
the measured p95 jitter — through the real assembler, the real
``OutputLeadBuffer``, the real pipecat ``FastAPIWebsocketOutputTransport`` and
the real ``RawAudioSerializer``, and then assert on *when* each socket write
happened:

* idle silence never reaches the device, so the device never enters REPLYING on
  zeros and the playout lead is accumulated on speech;
* once speech starts, the transport never runs out of queued audio — no write
  gap longer than one chunk plus tolerance, which is what a device underrun is;
* an in-reply pause is still forwarded in real time, because withholding it
  would starve the very audio chain the lead exists to protect.

``test_without_a_lead_the_device_starves`` is the control: it proves these
assertions can actually fail, by removing the lead and watching the gaps appear.
"""
import asyncio
import time
import unittest

from pipecat.frames.frames import BotStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameProcessor

from app.voice_runtime import LIVE_OUTPUT_LEAD_MS

from tests import live_fixtures as fx
from tests.test_live_device_audio import CHUNK_BYTES, DevicePath

# GPT-Live's measured delivery, in speech: 100 ms of audio per delta arriving
# every ~95 ms (0.985x real time overall), with a p95 spike to 160 ms every
# tenth delta. Taken from the three app.live_probe captures of 2026-10-07.
DELTA_PERIOD_S = 0.095
JITTER_PERIOD_S = 0.160
JITTER_EVERY = 10

# The transport writes whole 40 ms chunks (1,920 bytes at 24 kHz mono PCM16) off
# a 1.0x monotonic pacer. A gap materially longer than one chunk means it had
# nothing queued when the pacer came round, which is exactly what reaches the
# speaker as a dry I2S chain. 90 ms leaves room for event-loop scheduling on a
# loaded CI box while still being far below the 160 ms source jitter the lead is
# there to absorb.
MAX_WRITE_GAP_MS = 90

# 3 s of idle silence, then speech, then idle again: the shape that precedes
# every real reply, because the endpoint streams silence continuously and there
# is no output-audio-done event.
#
# Every plan here forwards a whole multiple of the transport's 40 ms chunk
# (speech + the 800 ms in-reply tail), so "every delivered byte arrived" is an
# exact assertion rather than one modulo a part-chunk left in the transport.
IDLE_THEN_SPEECH = (
    ("silence", 3000, 0),
    ("speech", 1200, 6000),
    ("silence", 2000, 0),
)

# A reply with a 600 ms inter-sentence pause — the longest measured — to prove
# an in-reply pause is still delivered rather than withheld.
SPEECH_WITH_PAUSE = (
    ("speech", 800, 6000),
    ("silence", 600, fx.IDLE_PEAK),
    ("speech", 800, 6000),
    ("silence", 2000, 0),
)


class Watch(FrameProcessor):
    """Counts the speaking frames the output transport emits."""

    def __init__(self) -> None:
        super().__init__()
        self.bot_started = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_started += 1
        await self.push_frame(frame, direction)


class Replay:
    """One instrumented run: stream a plan, record every device write."""

    def __init__(self, plan, lead_ms=LIVE_OUTPUT_LEAD_MS, tail=None):
        self.plan = plan
        self.lead_ms = lead_ms
        self._tail = tail
        self.writes = []          # (seconds since t0, bytes)
        self.speech_begins = []   # seconds since t0 of each speech stage
        self.t0 = 0.0

    async def __aenter__(self):
        self._path = DevicePath(lead_ms=self.lead_ms, tail=self._tail)
        path = await self._path.__aenter__()
        self.path = path
        socket = path.device
        original = socket.send_bytes

        async def timed(data):
            self.writes.append((time.monotonic() - self.t0, len(data)))
            await original(data)

        socket.send_bytes = timed
        return self

    async def __aexit__(self, *exc):
        await self._path.__aexit__(*exc)

    async def stream(self):
        """Push the plan's deltas at the measured in-speech cadence."""
        self.t0 = time.monotonic()
        index = 0
        for kind, ms, level in self.plan:
            if kind == "speech":
                self.speech_begins.append(time.monotonic() - self.t0)
            for delta in fx.output_audio_deltas(fx.reply_pcm(((kind, ms, level),))):
                self.path.model.push(delta)
                index += 1
                period = (JITTER_PERIOD_S if index % JITTER_EVERY == 0
                          else DELTA_PERIOD_S)
                await asyncio.sleep(period)
        # Let the 1.0x pacer drain whatever the lead is still holding.
        await asyncio.sleep(self.lead_ms / 1000.0 + 0.6)

    # -- what the device experienced ---------------------------------------

    @property
    def pcm(self) -> bytes:
        return self.path.device.pcm

    @property
    def first_write_s(self) -> float:
        return self.writes[0][0]

    @property
    def write_gaps_ms(self):
        return [round((self.writes[i][0] - self.writes[i - 1][0]) * 1000)
                for i in range(1, len(self.writes))]

    @property
    def max_write_gap_ms(self) -> int:
        gaps = self.write_gaps_ms
        return max(gaps) if gaps else 0


def expected_bytes(plan, max_silence_ms: int = 800, silence_peak: int = 4) -> bytes:
    """What a correct assembler delivers, derived from the plan alone.

    Three dispositions for an inaudible delta, and no other:

    * inside a reply, run still under ``max_silence_ms`` — delivered now, so
      the device's chain is fed through the pause;
    * before a reply, run still under ``max_silence_ms`` — held, then delivered
      in order ahead of the first audible delta;
    * run over ``max_silence_ms`` — idle: nothing from that run is delivered,
      including whatever was held at its head, and the reply is over.
    """
    out = bytearray()
    held = bytearray()
    run_ms = 0.0
    in_speech = False
    for delta in fx.output_audio_deltas(fx.reply_pcm(plan)):
        pcm = fx._decode(delta["delta"])
        peak = max((abs(s) for s in fx.samples_of(pcm)), default=0)
        duration = 1000.0 * (len(pcm) // 2) / fx.SAMPLE_RATE
        if peak <= silence_peak:
            run_ms += duration
            if run_ms > max_silence_ms:
                in_speech = False
                held.clear()
            elif in_speech:
                out.extend(pcm)
            else:
                held.extend(pcm)
            continue
        run_ms = 0.0
        in_speech = True
        out.extend(held)
        held.clear()
        out.extend(pcm)
    return bytes(out)


class TestIdleSilenceIsNotDelivered(unittest.IsolatedAsyncioTestCase):
    async def test_idle_silence_never_reaches_the_device(self):
        async with Replay(IDLE_THEN_SPEECH) as replay:
            await replay.stream()
            self.assertEqual(replay.pcm, expected_bytes(IDLE_THEN_SPEECH),
                             "the device must receive the reply, not the idle stream")
            discarded = replay.path.service.audio.metrics.idle_head_discarded_deltas
            self.assertGreaterEqual(discarded, 8,
                                    "the head of the leading idle run must be discarded")

    async def test_the_lead_is_accumulated_on_speech_not_spent_on_idle(self):
        """The defect itself: zeros used to arm and drain the lead.

        Before the fix the first device write landed at 0.52 s — 800 ms of
        digital silence bursting out of the lead buffer — and was followed by a
        3,659 ms gap with the device already in REPLYING. The first write must
        now follow the first audible delta.
        """
        async with Replay(IDLE_THEN_SPEECH) as replay:
            await replay.stream()
            speech_at = replay.speech_begins[0]
            self.assertGreater(replay.first_write_s, speech_at,
                               "the device was written to before the model spoke")
            # And the lead really was held: the burst arrives about lead_ms
            # after speech starts, not immediately.
            self.assertGreater(replay.first_write_s - speech_at,
                               LIVE_OUTPUT_LEAD_MS / 1000.0 * 0.5,
                               "no lead was accumulated before the first word")

    async def test_the_device_is_told_to_reply_once_per_reply(self):
        """Idle zeros used to put the device into REPLYING before any speech."""
        watch = Watch()
        async with Replay(IDLE_THEN_SPEECH, tail=watch) as replay:
            await replay.stream()
            self.assertEqual(watch.bot_started, 1,
                             "the device entered REPLYING on silence")
            self.assertGreater(replay.first_write_s, replay.speech_begins[0])


class TestNoDeviceUnderrun(unittest.IsolatedAsyncioTestCase):
    async def test_speech_is_written_without_a_starvation_gap(self):
        async with Replay(IDLE_THEN_SPEECH) as replay:
            await replay.stream()
            self.assertGreater(len(replay.writes), 10, "the reply was delivered")
            self.assertLessEqual(
                replay.max_write_gap_ms, MAX_WRITE_GAP_MS,
                f"the device chain ran dry mid-reply: gaps {replay.write_gaps_ms}",
            )
            self.assertTrue(all(n == CHUNK_BYTES for n in
                                (b for _, b in replay.writes[:-1])),
                            "every write but the last is a full 40 ms chunk")

    async def test_without_a_lead_the_device_starves(self):
        """The control: the same stream with no lead must fail the gap bound.

        Without this, a test that always passes would look like a guarantee.
        """
        async with Replay(IDLE_THEN_SPEECH, lead_ms=0) as replay:
            await replay.stream()
            self.assertGreater(
                replay.max_write_gap_ms, MAX_WRITE_GAP_MS,
                "a 0.985x source with 160 ms jitter must starve a lead-free "
                "device, or this bound proves nothing",
            )


class TestInReplyPausesSurvive(unittest.IsolatedAsyncioTestCase):
    async def test_an_in_reply_pause_is_delivered_in_real_time(self):
        """Withholding a mid-reply pause would starve the chain it protects."""
        async with Replay(SPEECH_WITH_PAUSE) as replay:
            await replay.stream()
            self.assertEqual(replay.pcm, expected_bytes(SPEECH_WITH_PAUSE))
            metrics = replay.path.service.audio.metrics
            # 600 ms of pause = 6 deltas, delivered rather than withheld.
            self.assertGreaterEqual(metrics.silence_released_deltas, 6)
            self.assertEqual(metrics.idle_head_discarded_deltas, 0,
                             "nothing was withheld at the head of this reply")
            self.assertLessEqual(
                replay.max_write_gap_ms, MAX_WRITE_GAP_MS,
                f"the pause starved the device: gaps {replay.write_gaps_ms}",
            )

    async def test_a_short_lead_in_is_released_with_the_first_word(self):
        """A sub-threshold run before speech is real output, not idle."""
        plan = (("silence", 400, 0), ("speech", 800, 6000), ("silence", 2000, 0))
        async with Replay(plan) as replay:
            await replay.stream()
            self.assertEqual(replay.pcm, expected_bytes(plan))
            lead_in = b"\x00\x00" * int(fx.SAMPLE_RATE * 0.4)
            self.assertTrue(replay.pcm.startswith(lead_in),
                            "the 400 ms lead-in must survive byte-for-byte")
            self.assertEqual(
                replay.path.service.audio.metrics.idle_head_discarded_deltas, 0)


if __name__ == "__main__":
    unittest.main()
