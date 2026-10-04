"""Device control protocol: wake metadata, flags, metrics, trigger audio, non-blocking wake."""
import asyncio
import base64
import json
import time
import unittest

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame

from app.raw_audio_serializer import MAX_TRIGGER_AUDIO_BYTES, RawAudioSerializer, parse_wake_meta


class TestWakeMeta(unittest.TestCase):
    def test_parses_v2_fields_and_derives_float_cutoff(self):
        meta = parse_wake_meta({"type": "wake", "v": 2, "turn": "a1b2-17", "src": "wake_word",
                                "model": "hey_leonard", "model_sha": "8418e780", "cutoff_uint8": 163,
                                "window": 3, "tier": "moderate", "fire_age_ms": 1290})
        self.assertEqual(meta["turn"], "a1b2-17")
        self.assertEqual(meta["cutoff"], round(163 / 255, 3))
        self.assertEqual(meta["window"], 3)

    def test_drops_hostile_or_malformed_values(self):
        meta = parse_wake_meta({"turn": "x\"; drop table", "model": 5, "cutoff": True,
                                "window": 10**9, "extra": "nope"})
        self.assertEqual(meta, {"turn": "xdroptable"})


class TestControlDispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ser = RawAudioSerializer("kitchen")
        self.calls = []

    def record(self, name):
        async def handler(*args):
            self.calls.append((name, args))
        return handler

    async def send(self, obj):
        return await self.ser.deserialize(json.dumps(obj))

    async def test_wake_passes_metadata_and_sets_turn(self):
        self.ser.set_wake_handler(self.record("wake"))
        self.assertIsNone(await self.send({"type": "wake", "turn": "t-1", "model": "hey_leonard"}))
        self.assertEqual(self.calls, [("wake", ({"turn": "t-1", "model": "hey_leonard"},))])
        self.assertEqual(self.ser.current_turn_id, "t-1")

    async def test_legacy_wake_without_metadata(self):
        self.ser.set_wake_handler(self.record("wake"))
        await self.send({"type": "wake"})
        self.assertEqual(self.calls, [("wake", ({},))])

    async def test_slow_wake_handler_work_does_not_block_audio(self):
        # P0-6 regression: the wake path used to await an 8 s HTTP POST before
        # the next mic frame could be read. Handlers may only schedule work.
        async def wake(_meta):
            asyncio.get_running_loop().create_task(asyncio.sleep(8))

        self.ser.set_wake_handler(wake)
        t0 = time.monotonic()
        await self.send({"type": "wake"})
        frame = await self.ser.deserialize(b"\x00\x00" * 160)
        self.assertIsInstance(frame, InputAudioRawFrame)
        self.assertLess(time.monotonic() - t0, 0.05)

    async def test_first_audio_after_wake_acks_once(self):
        self.ser.set_first_audio_handler(self.record("first_audio"))
        await self.send({"type": "wake"})
        await self.ser.deserialize(b"\x00\x00" * 160)
        await self.ser.deserialize(b"\x00\x00" * 160)
        self.assertEqual([c[0] for c in self.calls], ["first_audio"])

    async def test_button_cancel_flags_only_fast_presses_before_any_reply(self):
        self.ser.set_button_cancel_handler(self.record("flag"))
        await self.send({"type": "wake", "turn": "t-9"})
        await self.send({"type": "button_cancel"})
        self.assertEqual(self.calls, [("flag", ("button", "", 0))])
        # After reply audio has played, a press is the normal "done" gesture.
        self.calls.clear()
        await self.ser.serialize(OutputAudioRawFrame(audio=b"\x00\x00", sample_rate=24000, num_channels=1))
        await self.send({"type": "button_cancel"})
        self.assertEqual(self.calls, [])

    async def test_double_press_carries_turn_and_age(self):
        self.ser.set_button_cancel_handler(self.record("flag"))
        await self.send({"type": "false_flag", "turn": "boot1-4", "age_ms": 42000})
        self.assertEqual(self.calls, [("flag", ("double_press", "boot1-4", 42000))])

    async def test_turn_metrics_dispatch(self):
        self.ser.set_turn_metrics_handler(self.record("metrics"))
        await self.send({"type": "turn_metrics", "turn": "t-1", "fire_to_mic_ms": 1200})
        await self.send({"type": "turn_metrics", "fire_to_mic_ms": 1200})  # no turn -> ignored
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1][0], "t-1")

    async def test_trigger_audio_chunks_reassemble(self):
        self.ser.set_trigger_audio_handler(self.record("trigger"))
        pcm = bytes(range(256)) * 4
        half = len(pcm) // 2
        await self.send({"type": "trigger_audio", "turn": "t-2", "seq": 0, "b64": base64.b64encode(pcm[:half]).decode()})
        self.assertEqual(self.calls, [])
        await self.send({"type": "trigger_audio", "turn": "t-2", "seq": 1, "last": 1, "rate": 16000,
                         "b64": base64.b64encode(pcm[half:]).decode()})
        self.assertEqual(self.calls, [("trigger", ("t-2", pcm, 16000))])

    async def test_trigger_audio_size_limit_and_bad_base64(self):
        self.ser.set_trigger_audio_handler(self.record("trigger"))
        big = base64.b64encode(b"\x00" * (MAX_TRIGGER_AUDIO_BYTES + 2)).decode()
        await self.send({"type": "trigger_audio", "turn": "t-3", "last": 1, "b64": big})
        await self.send({"type": "trigger_audio", "turn": "t-4", "last": 1, "b64": "!!not-base64!!"})
        self.assertEqual(self.calls, [])

    async def test_interrupt_ignored_during_enrollment(self):
        class Recorder:
            active = True
            device_id = "kitchen"

            def feed(self, pcm):
                pass

        self.ser.set_interrupt_handler(self.record("interrupt"))
        self.ser.set_enrollment_recorder(Recorder())
        await self.send({"type": "interrupt"})
        self.assertEqual(self.calls, [])

    async def test_output_audio_callback_and_inbound_suppression(self):
        hits = []
        self.ser.set_output_audio_handler(lambda: hits.append(1))
        await self.ser.serialize(OutputAudioRawFrame(audio=b"\x00\x00", sample_rate=24000, num_channels=1))
        self.assertEqual(hits, [1])
        self.ser.suppress_inbound_until = time.monotonic() + 5
        self.assertIsNone(await self.ser.deserialize(b"\x00\x00" * 10))

    async def test_audio_taps_receive_forwarded_frames(self):
        class Tap:
            def __init__(self):
                self.got = b""

            def feed(self, pcm):
                self.got += pcm

        tap = Tap()
        self.ser.add_audio_tap(tap)
        await self.ser.deserialize(b"\x01\x00" * 4)
        self.assertEqual(tap.got, b"\x01\x00" * 4)

    async def test_odd_length_audio_is_rejected(self):
        self.assertIsNone(await self.ser.deserialize(b"\x00\x00\x00"))

    async def test_reply_audio_waits_for_an_out_of_band_prompt(self):
        # An acknowledgement still playing must not interleave with the reply.
        frame = OutputAudioRawFrame(audio=b"\x02\x00", sample_rate=24000, num_channels=1)
        self.ser.begin_out_of_band()
        pending = asyncio.create_task(self.ser.serialize(frame))
        await asyncio.sleep(0.02)
        self.assertFalse(pending.done())
        self.ser.end_out_of_band()
        self.assertEqual(await asyncio.wait_for(pending, 1), b"\x02\x00")
        # Without a prompt in progress nothing waits.
        self.assertEqual(await asyncio.wait_for(self.ser.serialize(frame), 0.1), b"\x02\x00")


if __name__ == "__main__":
    unittest.main()
