"""The GPT-Live service in on-demand mode, through the real pipeline.

``tests/test_live_session_gate.py`` proves the *policy*. This module proves the
*service* honours it on a real pipecat pipeline and a real
``session.start``/``session.close`` exchange: nothing connects at startup, an
explicit open connects exactly once, and the device audio that arrives while
the handshake is in flight is preserved byte-for-byte and in order.
"""
import asyncio
import base64
import unittest
from unittest import mock

from pipecat.frames.frames import InputAudioRawFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from app.live_protocol import LIVE_SAMPLE_RATE
from app.live_service import PRESTART_AUDIO_MAX_S

from tests import live_fixtures as fx
from tests.live_harness import LiveHarness


def ramp(nsamples, start=0):
    """Distinguishable PCM16 so order and loss are both detectable."""
    return b"".join(
        ((start + i) % 20000).to_bytes(2, "little", signed=False)
        for i in range(nsamples)
    )


def sent_input_audio(socket) -> bytes:
    return b"".join(
        base64.b64decode(m["audio"]) for m in socket.of("session.input_audio.append")
    )


class OnDemandCase(unittest.IsolatedAsyncioTestCase):
    def harness(self, **kwargs):
        kwargs.setdefault("on_demand", True)
        return LiveHarness(self, **kwargs)

    async def feed(self, harness, pcm, rate=LIVE_SAMPLE_RATE):
        await harness.source.push_frame(
            InputAudioRawFrame(audio=pcm, sample_rate=rate, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        await asyncio.sleep(0.01)


class TestNothingOpensByItself(OnDemandCase):
    async def test_startup_opens_no_socket(self):
        async with self.harness() as h:
            self.assertEqual(h.sockets, [], "StartFrame must not buy a session")
            self.assertFalse(h.service.session_open)
            self.assertFalse(h.service.session_ready)

    async def test_idle_device_audio_opens_no_socket(self):
        """Audio alone never connects: the gate decides, not the audio path."""
        async with self.harness() as h:
            for _ in range(20):
                await self.feed(h, ramp(480))
            self.assertEqual(h.sockets, [])

    async def test_silence_clock_only_runs_inside_paid_session_without_activity(self):
        async with self.harness(fill_silence=True) as h:
            user_activity=[]
            h.service.on_input_audio=lambda: user_activity.append(True)
            await asyncio.sleep(.15)
            self.assertEqual(h.sockets, [])
            self.assertIsNone(h.service._silence_task)
            await h.open_session()
            self.assertEqual(h.socket.of('session.input_audio.append'), [])
            await h.start_session()
            await asyncio.sleep(.35)
            self.assertGreaterEqual(len(h.socket.of('session.input_audio.append')), 2)
            self.assertEqual(set(sent_input_audio(h.socket)), {0})
            self.assertEqual(user_activity, [], 'clock ticks must not buy idle time')
            await h.service._disconnect()
            count=len(h.socket.of('session.input_audio.append'))
            await asyncio.sleep(.25)
            self.assertEqual(len(h.socket.of('session.input_audio.append')), count)
            self.assertIsNone(h.service._silence_task)
            self.assertEqual(len(h.sockets), 1, 'silence must never reconnect')

    async def test_the_default_runtime_still_connects_at_startup(self):
        """Realtime parity: on_demand is opt-in and off by default."""
        async with LiveHarness(self) as h:
            self.assertFalse(h.service.on_demand)
            self.assertEqual(len(h.sockets), 1)


class TestOpeningOnDemand(OnDemandCase):
    async def test_open_session_connects_once_and_starts_the_session(self):
        async with self.harness() as h:
            await h.open_session()
            self.assertEqual(len(h.sockets), 1)
            self.assertEqual(h.socket.types(), ["session.start"])
            self.assertTrue(h.service.session_open)
            self.assertFalse(h.service.session_ready, "not until session.started")
            await h.start_session("sess_live_1")
            self.assertTrue(h.service.session_ready)
            self.assertEqual(h.service.session_id, "sess_live_1")

    async def test_repeated_open_session_does_not_reconnect(self):
        async with self.harness() as h:
            await h.open_session()
            await h.start_session()
            await h.service.open_session()
            await h.service.open_session()
            self.assertEqual(len(h.sockets), 1)

    async def test_the_server_expiry_is_captured_for_the_gate(self):
        async with self.harness() as h:
            await h.open_session()
            await h.start_session()
            self.assertEqual(h.service.session_expires_at,
                             float(fx.SESSION_STARTED["session"]["expires_at"]))

    async def test_usage_updates_become_billable_seconds(self):
        async with self.harness() as h:
            await h.open_session()
            await h.start_session()
            h.socket.push({"type": "session.usage.updated", "usage": {"seconds": 73}})
            await asyncio.sleep(0.05)
            self.assertEqual(h.service.session_seconds, 73.0)


class TestEarlyAudioIsPreserved(OnDemandCase):
    async def test_audio_during_the_handshake_is_replayed_in_order(self):
        """The first utterance must survive the handshake.

        The device starts streaming the post-wake utterance immediately; the
        Live protocol forbids sending audio before ``session.started``. So it
        is buffered locally and replayed in order — not dropped, and not
        reordered behind the frames that arrive after the session exists.
        """
        async with self.harness() as h:
            await h.open_session()
            early = [ramp(480, start=i * 480) for i in range(5)]
            for chunk in early:
                await self.feed(h, chunk)
            self.assertEqual(h.socket.of("session.input_audio.append"), [],
                             "nothing may be sent before session.started")

            await h.start_session()
            late = ramp(480, start=5 * 480)
            await self.feed(h, late)
            await asyncio.sleep(0.05)

            self.assertEqual(sent_input_audio(h.socket), b"".join(early) + late)

    async def test_the_pre_roll_is_bounded_and_keeps_the_newest_audio(self):
        """A bounded buffer: it drops the oldest, never the word being spoken."""
        async with self.harness() as h:
            await h.open_session()
            limit = LIVE_SAMPLE_RATE * 2 * PRESTART_AUDIO_MAX_S
            # Twice the cap, in 0.5 s pieces.
            piece = LIVE_SAMPLE_RATE // 2
            pieces = [ramp(piece, start=i * 7) for i in range(2 * PRESTART_AUDIO_MAX_S + 2)]
            for chunk in pieces:
                await self.feed(h, chunk)
            self.assertLessEqual(len(h.service._prestart_audio), limit)

            await h.start_session()
            await asyncio.sleep(0.05)
            sent = sent_input_audio(h.socket)
            self.assertLessEqual(len(sent), limit)
            self.assertTrue(b"".join(pieces).endswith(sent),
                            "the tail of the utterance is what survives")

    async def test_resampled_device_audio_survives_the_handshake(self):
        """16 kHz device audio is resampled, then held — not held, then lost."""
        async with self.harness() as h:
            await h.open_session()
            await self.feed(h, ramp(1600), rate=16000)
            self.assertGreater(len(h.service._prestart_audio), 0)
            await h.start_session()
            await asyncio.sleep(0.05)
            self.assertGreater(len(sent_input_audio(h.socket)), 0)

    async def test_a_second_session_starts_with_an_empty_pre_roll(self):
        async with self.harness() as h:
            await h.open_session()
            await self.feed(h, ramp(480))
            await h.service.close_session("device stop")
            self.assertEqual(len(h.service._prestart_audio), 0)
            await h.open_session()
            await h.start_session()
            await asyncio.sleep(0.05)
            self.assertEqual(sent_input_audio(h.socket), b"",
                             "last turn's audio must not leak into the next session")


class TestClosingOnDemand(OnDemandCase):
    async def test_close_session_closes_gracefully_then_drops_the_socket(self):
        async with self.harness() as h:
            await h.open_session()
            await h.start_session()
            socket = h.socket
            closer = asyncio.create_task(h.service.close_session("idle"))
            await socket.wait_for_sent("session.close")
            socket.push({"type": "session.closed", "reason": "close_requested",
                         "usage": {"seconds": 12}})
            await asyncio.wait_for(closer, 3)
            self.assertIn("session.close", socket.types())
            self.assertTrue(socket.closed)
            self.assertFalse(h.service.session_open)
            self.assertFalse(h.service.session_ready)
            self.assertIsNone(h.service.session_expires_at)

    async def test_close_session_is_a_no_op_with_no_session(self):
        async with self.harness() as h:
            await h.service.close_session("idle")
            self.assertEqual(h.sockets, [])

    async def test_close_survives_a_server_that_never_acknowledges(self):
        async with self.harness() as h:
            await h.open_session()
            await h.start_session()
            socket = h.socket
            # No session.closed is ever pushed: the close must still complete
            # and still drop the socket, or an unclosed session keeps billing.
            with mock.patch("app.live_service.SESSION_CLOSE_TIMEOUT_S", 0.05):
                await asyncio.wait_for(h.service.close_session("timeout test"), 3)
            self.assertTrue(socket.closed)
            self.assertFalse(h.service.session_open)

    async def test_an_open_turn_is_closed_before_the_session_is(self):
        async with self.harness() as h:
            await h.open_session()
            await h.start_session()
            h.socket.push_all(fx.transcript_in("turn on the lights"))
            await asyncio.sleep(0.05)
            self.assertTrue(h.service._user_turn.open)
            await h.service.close_session("device stop")
            self.assertFalse(h.service._user_turn.open,
                             "no turn is left dangling across a close")


class TestLifecycleObservers(OnDemandCase):
    """What the gate's deadlines are actually driven by."""

    def observers(self, service):
        seen = {"input": 0, "audible": 0, "backend": 0, "reply": 0}
        service.on_input_audio = lambda: seen.__setitem__("input", seen["input"] + 1)
        service.on_audible_output = lambda: seen.__setitem__("audible", seen["audible"] + 1)
        service.on_backend_activity = lambda: seen.__setitem__("backend", seen["backend"] + 1)
        service.on_reply_finished = lambda: seen.__setitem__("reply", seen["reply"] + 1)
        return seen

    async def test_device_audio_reports_activity_even_before_the_session(self):
        async with self.harness() as h:
            seen = self.observers(h.service)
            await h.open_session()
            await self.feed(h, ramp(480))
            self.assertEqual(seen["input"], 1)

    async def test_audible_reply_audio_reports_activity(self):
        async with self.harness() as h:
            seen = self.observers(h.service)
            await h.open_session()
            await h.start_session()
            h.socket.push_all(fx.output_audio_deltas(fx.tone(400)))
            await h.sink.wait_for(TTSAudioRawFrame, count=1, timeout=3)
            await asyncio.sleep(0.05)
            self.assertGreater(seen["audible"], 0)

    async def test_inaudible_output_reports_no_activity(self):
        """The whole anti-silence rule, at its source.

        GPT-Live streams continuous digital silence between replies. If those
        frames counted as activity the session would never close.
        """
        async with self.harness() as h:
            seen = self.observers(h.service)
            await h.open_session()
            await h.start_session()
            h.socket.push_all(fx.output_audio_deltas(fx.silence(3000)))
            await asyncio.sleep(0.4)
            self.assertEqual(seen["audible"], 0)

    async def test_a_delegation_reports_backend_activity(self):
        async with self.harness() as h:
            seen = self.observers(h.service)
            await h.open_session()
            await h.start_session()
            h.socket.push(fx.delegation_created())
            await asyncio.sleep(0.05)
            self.assertEqual(seen["backend"], 1)

    async def test_the_end_of_a_reply_opens_the_follow_up_window(self):
        async with self.harness() as h:
            seen = self.observers(h.service)
            await h.open_session()
            await h.start_session()
            h.socket.push({"type": "session.output_transcript.delta",
                           "delta": "Sure.", "start_ms": 0, "end_ms": 300})
            await asyncio.sleep(0.05)
            await h.service._close_open_turns()
            self.assertEqual(seen["reply"], 1)


if __name__ == "__main__":
    unittest.main()
