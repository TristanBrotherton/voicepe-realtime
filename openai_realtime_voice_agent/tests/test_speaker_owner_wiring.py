"""The speaker owner wired into the add-on (per-device `speaker_owner`).

With the switch off the device must receive exactly what it received before
the owner existed: the fixed scenario in tests/switch_off_scenario.py is run
here and compared with the digest the add-on produced before this work.
With it on: the device protocol (caps, interrupt reasons, utt_ack, the
hello additions), model audio through the owner in both framings, clips and
announcements through the owner, redelivery after a lost link, and the
session gate waiting for speech that is still being delivered.
"""
import asyncio
import json
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import speaker_owner as so
from app.device_config import DeviceDirectory
from app.device_registry import DeviceConnection
from app.live_session_gate import CLOSED, OPEN, LiveSessionGate, LiveSessionPolicy, PLAYOUT_WAIT_MAX_S
from app.live_cost import LiveSpendLedger
from app.raw_audio_serializer import (RawAudioSerializer, parse_interrupt, parse_start,
                                      parse_utt_ack)
from app.live_service import OpenAILiveLLMService
from app.websocket_handler import WebSocketHandler

from tests.switch_off_scenario import run_fixed_scenario
from tests.test_live_session_gate import Clock, FakeLiveService

# What the device received in the fixed scenario from the add-on as it was
# before the speaker owner (0.20.4, measured on that code with this file).
BEFORE_SHA256 = "c91950de4a1790c53aab0a1f87486caa6642f6bb117063ccf622ed353a19d2b8"
BEFORE_BYTES = 144000
BEFORE_TEXTS = [{"type": "phase", "value": "replying"}]
BEFORE_HELLO = {"type": "hello", "proto": 2, "audio_out": "pcm", "follow_up_ms": 0,
                "follow_up_open_delay_ms": 700, "wake_open_delay_ms": 700,
                "playback_prebuffer_ms": 0, "trigger_capture": 0}


_OWNER_LOG = logging.getLogger("app.speaker_owner")
_OWNER_LOG_LEVEL = _OWNER_LOG.level


def setUpModule():
    _OWNER_LOG.setLevel(logging.WARNING)


def tearDownModule():
    _OWNER_LOG.setLevel(_OWNER_LOG_LEVEL)


class TestWithoutTheOwnerIsUnchanged(unittest.IsolatedAsyncioTestCase):
    """The transport's output path, as the Realtime runtime uses it."""

    async def test_no_owner_is_byte_identical_to_before(self):
        keep = {}
        digest, size, texts, hello = await run_fixed_scenario(keep=keep)
        self.assertEqual((digest, size, texts, hello),
                         (BEFORE_SHA256, BEFORE_BYTES, BEFORE_TEXTS, BEFORE_HELLO))
        self.assertIsNone(keep["connection"].speaker_owner)
        self.assertIsNone(keep["service"].speaker_owner)


class TestWithTheOwner(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_framing_delivers_the_same_audio_without_the_hold(self):
        keep = {}
        digest, size, texts, hello = await run_fixed_scenario(True, keep=keep)
        self.assertEqual((digest, size), (BEFORE_SHA256, BEFORE_BYTES),
                         "the same audio, byte for byte (replies get no lead-in)")
        self.assertEqual(hello, dict(BEFORE_HELLO, chain_prime_ms=200, utt_cushion_ms=0))
        self.assertIn({"type": "phase", "value": "replying"}, texts,
                      "the owner makes the Bot frames the transport used to")
        self.assertFalse([t for t in texts if t["type"].startswith("utt") or
                          t["type"] == "interrupt_ack"], "older firmware gets plain audio")
        owner = keep["connection"].speaker_owner
        self.assertEqual(owner.framing, so.LEGACY)
        kinds = [u.kind for u in owner.history()]
        self.assertTrue(kinds and set(kinds) == {so.REPLY})

    async def test_utt1_framing_brackets_utterances_and_acks_interrupts(self):
        keep = {}
        start = {"type": "start", "caps": ["ireason1", "utt1"], "fw": "1.4.0"}
        interrupt = {"type": "interrupt", "reason": "stop", "seq": 4}
        digest, size, texts, hello = await run_fixed_scenario(True, start=start,
                                                              interrupt=interrupt, keep=keep)
        owner = keep["connection"].speaker_owner
        self.assertEqual(owner.framing, so.UTT)
        starts = [t for t in texts if t["type"] == "utt_start"]
        ends = [t for t in texts if t["type"] == "utt_end"]
        self.assertEqual(len(starts), 2, "two replies, two utterances")
        # The first reply ended; the second was still open (its source had
        # not gone quiet for long) when the stop arrived, so it has no end.
        self.assertEqual([e["id"] for e in ends], [starts[0]["id"]])
        first, second = owner.utterance(starts[0]["id"]), owner.utterance(starts[1]["id"])
        self.assertEqual(ends[0]["bytes"], first.total)
        self.assertEqual(first.total + second.sent, size)
        self.assertEqual(second.reason, "stop")
        self.assertEqual(texts[-1], {"type": "interrupt_ack", "seq": 4})
        for message in texts:
            if message["type"] != "phase":
                self.assertNotIn("value", message)

    async def test_the_lead_buffer_and_pacer_are_bypassed(self):
        """The 600 ms hold is gone: the first audio reaches the device at once."""
        keep = {}
        await run_fixed_scenario(True, keep=keep)
        owner = keep["connection"].speaker_owner
        first = owner.history()[0]
        self.assertLess(first.first_sent_mono - first.submitted, 0.1)


class TestDeviceMessages(unittest.IsolatedAsyncioTestCase):
    def test_interrupt_fields(self):
        self.assertEqual(parse_interrupt({"type": "interrupt"}), {})
        self.assertEqual(parse_interrupt({"reason": "stop", "seq": 3, "utt": 7, "fed": 4800}),
                         {"reason": "stop", "seq": 3, "utt": 7, "fed": 4800})
        self.assertEqual(parse_interrupt({"reason": "shout", "seq": -1, "fed": "x"}), {})

    def test_start_fields(self):
        info = parse_start({"caps": ["ireason1", "utt1", 5, "<x>"], "fw": "1.4.0",
                            "last_disconnect": {"why": "tx_timeout", "esp_err": 263,
                                                "up_s": 3600, "ago_s": 2, "x": "y"},
                            "prev_utt": {"id": 12, "fed": 9600, "bytes": 48000, "st": "playing"}})
        self.assertEqual(info["caps"], ["ireason1", "utt1", "x"])
        self.assertEqual(info["last_disconnect"],
                         {"why": "tx_timeout", "esp_err": 263, "up_s": 3600, "ago_s": 2})
        self.assertEqual(info["prev_utt"], {"id": 12, "fed": 9600, "bytes": 48000, "st": "playing"})
        self.assertEqual(parse_start({"type": "start"}), {})
        last = parse_start({"last_disconnect": {"why": "ws_error", "errno": 104,
                                                "reset": "sw_reset"}})["last_disconnect"]
        self.assertEqual(last, {"why": "ws_error", "errno": 104, "reset": "sw_reset"})

    def test_utt_ack_fields(self):
        self.assertEqual(parse_utt_ack({"id": 3, "st": "done", "fed": 100, "und": 0, "x": 1}),
                         {"type": "utt_ack", "id": 3, "st": "done", "fed": 100, "und": 0})
        self.assertIsNone(parse_utt_ack({"id": 3, "st": "maybe"}))
        self.assertIsNone(parse_utt_ack({"st": "done"}))

    async def test_handlers_get_the_reason_and_old_ones_still_work(self):
        serializer = RawAudioSerializer("testdevice")
        calls = []

        async def no_args():
            calls.append("old")
        serializer.set_interrupt_handler(no_args)
        await serializer.deserialize(json.dumps({"type": "interrupt", "reason": "stop", "seq": 1}))
        self.assertEqual(calls, ["old"])

        async def with_info(info):
            calls.append(info)
        serializer.set_interrupt_handler(with_info)
        await serializer.deserialize(json.dumps({"type": "interrupt", "reason": "wake", "seq": 2}))
        self.assertEqual(calls[-1], {"reason": "wake", "seq": 2})

    async def test_a_housekeeping_interrupt_does_not_acknowledge_a_timer(self):
        serializer = RawAudioSerializer("testdevice")
        await serializer.deserialize(json.dumps({"type": "interrupt", "reason": "no_speech"}))
        self.assertEqual(serializer._last_interrupt_mono, 0.0)
        await serializer.deserialize(json.dumps({"type": "interrupt", "reason": "stop"}))
        self.assertGreater(serializer._last_interrupt_mono, 0.0)
        before = serializer._last_interrupt_mono
        serializer._last_interrupt_mono = 0.0
        await serializer.deserialize(json.dumps({"type": "interrupt"}))
        self.assertGreater(serializer._last_interrupt_mono, 0.0, "older firmware: as before")
        del before

    async def test_the_owner_learns_of_an_interrupt_enrollment_ignores(self):
        serializer = RawAudioSerializer("testdevice")
        serializer._enrolling = lambda: True
        seen, stops = [], []

        async def speaker(info):
            seen.append(info)

        async def stop(info=None):
            stops.append(info)
        serializer.set_speaker_interrupt_handler(speaker)
        serializer.set_interrupt_handler(stop)
        await serializer.deserialize(json.dumps({"type": "interrupt", "reason": "stop", "seq": 9}))
        self.assertEqual(seen, [{"reason": "stop", "seq": 9, "ignored": True}])
        self.assertEqual(stops, [])

    async def test_start_and_utt_ack_reach_their_handlers(self):
        serializer = RawAudioSerializer("testdevice")
        got = []

        async def on_start(info):
            got.append(("start", info))

        async def on_ack(ack):
            got.append(("ack", ack))
        serializer.set_session_start_handler(on_start)
        serializer.set_utt_ack_handler(on_ack)
        await serializer.deserialize(json.dumps({"type": "start", "caps": ["utt1"]}))
        await serializer.deserialize(json.dumps({"type": "utt_ack", "id": 1, "st": "playing",
                                                 "fed": 0, "wait_ms": 210}))
        self.assertEqual(got[0], ("start", {"caps": ["utt1"]}))
        self.assertEqual(serializer.device_caps, ["utt1"])
        self.assertEqual(got[1][1]["wait_ms"], 210)


class TestStopReason(unittest.IsolatedAsyncioTestCase):
    """#35: only the stop word marks the request. The same interrupt also means
    a barge-in wake, the no-speech watchdog, mute and the button, and firmware
    without reasons sends none: all of those behave as before."""

    async def _interrupt(self, message):
        keep = {}
        calls = []
        with patch.object(OpenAILiveLLMService, "note_device_stop",
                          lambda service: calls.append(service)):
            await run_fixed_scenario(True, keep=keep, interrupt=message)
        return calls

    async def test_the_stop_word_marks_the_request(self):
        self.assertEqual(len(await self._interrupt({"type": "interrupt", "reason": "stop", "seq": 1})), 1)

    async def test_no_other_reason_does(self):
        for reason in ("wake", "button", "mute", "no_speech", "residual"):
            calls = await self._interrupt({"type": "interrupt", "reason": reason, "seq": 1})
            self.assertEqual(calls, [], reason)

    async def test_firmware_without_a_reason_behaves_as_before(self):
        self.assertEqual(await self._interrupt({"type": "interrupt"}), [])


class TestOption(unittest.TestCase):
    def test_the_cold_start_lead_is_a_per_device_option_and_stays_200_by_default(self):
        """The 200 and 400 ms comparison needs no rebuild and no redeploy."""
        directory = DeviceDirectory.from_json(json.dumps([
            {"name": "a", "chain_prime_ms": 400, "utt_cushion_ms": 100},
            {"name": "b", "chain_prime_ms": 9000},
            {"name": "c"},
        ]))
        a, b, c = directory.get("a"), directory.get("b"), directory.get("c")
        self.assertEqual((a.chain_prime_ms, a.utt_cushion_ms), (400, 100))
        self.assertIsNone(b.chain_prime_ms, "out of range is ignored")
        self.assertIsNone(c.chain_prime_ms)
        handler = WebSocketHandler()
        handler.device_directory = directory
        handler.speaker_owner_enabled = True
        hello = handler.hello_payload("a")
        self.assertEqual((hello["chain_prime_ms"], hello["utt_cushion_ms"]), (400, 100))
        for device in ("b", "c", "unknown"):
            hello = handler.hello_payload(device)
            self.assertEqual((hello["chain_prime_ms"], hello["utt_cushion_ms"]), (200, 0), device)

    def test_the_realtime_runtime_sends_no_lead(self):
        handler = WebSocketHandler()
        self.assertNotIn("chain_prime_ms", handler.hello_payload("a"))

    def test_an_old_speaker_owner_option_is_ignored(self):
        """Options saved while it was a switch still parse."""
        directory = DeviceDirectory.from_json(json.dumps([
            {"name": "a", "speaker_owner": False, "chain_prime_ms": 300}]))
        self.assertEqual(directory.get("a").chain_prime_ms, 300)
        self.assertFalse(hasattr(directory.get("a"), "speaker_owner"))

    def test_every_live_connection_gets_an_owner(self):
        from app.device_registry import DeviceConnection

        class Client:
            async def send(self, _data):
                pass

        class Transport:
            client = Client()
        handler = WebSocketHandler()
        connection = DeviceConnection(device_id="a", websocket=None)
        connection.transport = Transport()
        self.assertIsNone(handler._build_speaker_owner(connection), "Realtime: no owner")
        handler.speaker_owner_enabled = True
        owner = handler._build_speaker_owner(connection)
        self.assertIsInstance(owner, so.SpeakerOwner)
        self.assertEqual(owner.framing, so.LEGACY, "legacy framing until the device offers utt1")
        self.assertEqual(owner.prime_ms, 200)

    def test_a_device_on_older_firmware_gets_legacy_framing(self):
        async def _noop(_data):
            pass
        owner = so.SpeakerOwner(_noop, _noop, device_id="a")
        self.assertEqual(owner.set_framing([]), so.LEGACY)
        self.assertEqual(owner.set_framing(["ireason1"]), so.LEGACY)
        self.assertEqual(owner.set_framing(["ireason1", "utt1"]), so.UTT)


def _handler_with_owner(framing=so.UTT):
    handler = WebSocketHandler()
    handler.device_directory = DeviceDirectory.from_json("")
    handler.speaker_owner_enabled = True
    sent = []

    async def send(data):
        sent.append(data)
    owner = so.SpeakerOwner(send, send, framing=framing, device_id="testdevice")
    serializer = RawAudioSerializer("testdevice")
    connection = DeviceConnection(device_id="testdevice", websocket=None, serializer=serializer,
                                  profile=handler.device_directory.get("testdevice"))
    connection.speaker_owner = owner
    handler.devices._devices["testdevice"] = connection
    return handler, connection, owner, sent


class TestCompleteAudio(unittest.IsolatedAsyncioTestCase):
    async def test_without_an_owner_callers_keep_their_old_path(self):
        handler = WebSocketHandler()
        self.assertIsNone(await handler.play_owned(b"\x00\x00" * 100, None, so.CLIP))

    async def test_a_clip_plays_and_a_second_one_in_the_turn_does_not(self):
        handler, connection, owner, sent = _handler_with_owner()
        self.assertTrue(await handler.play_owned(b"\x01\x00" * 2400, "testdevice", so.CLIP))
        owner.observers  # noqa: B018 - wired below
        handler._observe_owner(connection, None)
        second = owner.submit(so.CLIP, b"\x01\x00" * 2400, turn="")
        await asyncio.sleep(0.05)
        self.assertNotEqual(second.state, so.QUEUED)
        await owner.shutdown()

    async def test_an_announcement_guards_the_mic_from_first_byte_to_end(self):
        handler, connection, owner, sent = _handler_with_owner(so.LEGACY)
        handler._observe_owner(connection, None)
        serializer = connection.serializer
        task = asyncio.create_task(handler.play_owned(b"\x01\x00" * 4800, "testdevice", so.ANNOUNCE,
                                                      wait="finished", keep=True))
        await asyncio.sleep(0.05)
        self.assertGreater(serializer.suppress_inbound_until, time.monotonic() + 60)
        self.assertTrue(await task)
        self.assertLess(serializer.suppress_inbound_until, time.monotonic() + 1.3)
        await owner.shutdown()

    async def test_a_stopped_announcement_counts_as_heard(self):
        handler, connection, owner, sent = _handler_with_owner()
        task = asyncio.create_task(handler.play_owned(b"\x01\x00" * 48000, "testdevice",
                                                      so.ANNOUNCE, wait="finished"))
        await asyncio.sleep(0.05)
        utt = owner.history()[-1]
        owner.on_ack({"id": utt.id, "st": "playing", "fed": 0})
        owner.interrupt(1, "stop", utt.id, 9600)
        self.assertTrue(await task)
        self.assertEqual((utt.state, utt.reason), (so.CUT, "stop"))
        await owner.shutdown()


class TestRedelivery(unittest.IsolatedAsyncioTestCase):
    async def test_a_lost_announcement_is_delivered_again_once(self):
        handler, connection, owner, sent = _handler_with_owner()
        lost = owner.submit(so.ANNOUNCE, b"\x01\x00" * 240000, keep=True)
        clip = owner.submit(so.CLIP, b"\x01\x00" * 2400, turn="t1")
        await asyncio.sleep(0.05)
        self.assertLess(lost.sent, lost.total, "capped: not all of it is on the device yet")
        owner.link_lost()
        handler._lost_owners["testdevice"] = (owner, time.monotonic())
        new_owner, new_sent = so.SpeakerOwner(AsyncMock(), AsyncMock()), []
        connection.speaker_owner = new_owner
        handler._redeliver(connection, {"id": lost.id, "fed": 9600, "st": "playing"})
        again = new_owner.history()
        self.assertEqual([u.kind for u in again], [so.ANNOUNCE])
        self.assertEqual(again[0].total, lost.total)
        self.assertNotIn(clip.id, [u.id for u in again])
        handler._redeliver(connection, None)
        self.assertEqual(len(new_owner.history()), 1, "once")
        await new_owner.shutdown()

    async def test_nothing_is_redelivered_that_the_device_finished(self):
        handler, connection, owner, sent = _handler_with_owner()
        utt = owner.submit(so.ANNOUNCE, b"\x01\x00" * 2400, keep=True)
        await asyncio.sleep(0.05)
        owner.link_lost()
        handler._lost_owners["testdevice"] = (owner, time.monotonic())
        new_owner = so.SpeakerOwner(AsyncMock(), AsyncMock())
        connection.speaker_owner = new_owner
        handler._redeliver(connection, {"id": utt.id, "fed": 4800, "st": "done"})
        self.assertEqual(new_owner.history(), [])

    async def test_nothing_is_redelivered_after_thirty_seconds(self):
        handler, connection, owner, sent = _handler_with_owner()
        owner.submit(so.ANNOUNCE, b"\x01\x00" * 240000, keep=True)
        await asyncio.sleep(0.01)
        owner.link_lost()
        handler._lost_owners["testdevice"] = (owner, time.monotonic() - 31)
        new_owner = so.SpeakerOwner(AsyncMock(), AsyncMock())
        connection.speaker_owner = new_owner
        handler._redeliver(connection, None)
        self.assertEqual(new_owner.history(), [])

    async def test_old_lost_owners_are_pruned_at_teardown(self):
        handler, connection, owner, sent = _handler_with_owner()
        stale = so.SpeakerOwner(AsyncMock(), AsyncMock())
        handler._lost_owners["gone-device"] = (stale, time.monotonic() - 31)
        handler._lost_owners["recent-device"] = (stale, time.monotonic())
        handler._note_link_lost(connection)
        self.assertNotIn("gone-device", handler._lost_owners)
        self.assertIn("recent-device", handler._lost_owners)
        self.assertIn("testdevice", handler._lost_owners)
        before = handler._lost_owners["testdevice"]
        handler._note_link_lost(connection)
        self.assertIs(handler._lost_owners["testdevice"], before, "recorded once")
        await owner.shutdown()
        await stale.shutdown()


class TestSessionWaitsForSpeech(unittest.IsolatedAsyncioTestCase):
    def _gate(self, service):
        self.clock = Clock()
        gate = LiveSessionGate(
            service, device_id="testdevice",
            policy=LiveSessionPolicy(idle_s=10.0, followup_s=8.0, max_silent_s=45.0,
                                     max_session_s=300.0, open_timeout_s=10.0, tick_s=0.005),
            ledger=LiveSpendLedger(clock=Clock(1_700_000_000.0), path=None),
            monotonic=self.clock, wall_clock=Clock(1_700_000_000.0))
        return gate

    async def _advance(self, gate, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1.0)
            await asyncio.sleep(0.01)

    async def test_an_idle_close_waits_while_speech_is_being_delivered(self):
        service = FakeLiveService()
        service.speaker_busy = True
        gate = self._gate(service)
        await gate.open("wake")
        await self._advance(gate, 12)
        self.assertEqual(gate.state, OPEN)
        service.speaker_busy = False
        await self._advance(gate, 1)
        await asyncio.sleep(0.05)
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(service.closes, ["idle"])

    async def test_the_wait_is_bounded(self):
        service = FakeLiveService()
        service.speaker_busy = True
        gate = self._gate(service)
        await gate.open("wake")
        await self._advance(gate, 11 + PLAYOUT_WAIT_MAX_S + 1)
        await asyncio.sleep(0.05)
        self.assertEqual(gate.state, CLOSED)

    async def test_a_stop_never_waits(self):
        service = FakeLiveService()
        service.speaker_busy = True
        gate = self._gate(service)
        await gate.open("wake")
        await gate.close("device stop")
        self.assertEqual(service.closes, ["device stop"])


class TestServiceLabels(unittest.TestCase):
    def _service(self):
        from app.live_service import OpenAILiveLLMService
        from app.voice_runtime import LiveConfig
        return OpenAILiveLLMService(api_key="sk-test", config=LiveConfig(voice="marin"),
                                    instructions="x", backend_instructions="y", tools=[],
                                    fill_silence=False, admit_complete_replies=False)

    def test_kinds_follow_the_gate(self):
        service = self._service()
        self.assertEqual(service._utt_kind(), so.REPLY)
        service.router = object()
        for gate, acked, pending, kind in (("pre", False, set(), so.ACK),
                                           ("post", False, set(), so.RESULT),
                                           ("post", True, set(), so.RESULT),
                                           ("post", True, {"c1"}, so.PROMPT),
                                           ("open", False, set(), so.REPLY)):
            service._gate, service._result_acked = gate, acked
            service._confirmation_prompts_pending = set(pending)
            self.assertEqual(service._utt_kind(), kind, gate)

    def test_without_an_owner_nothing_is_labelled(self):
        from app.live_audio import OutputChunk
        service = self._service()
        service._enqueue_audio(OutputChunk(pcm=b"\x01\x00" * 10, audible=True))
        item = service._audio_queue.get_nowait()
        self.assertIsInstance(item, OutputChunk)
        self.assertFalse(service.speaker_busy)

    def test_a_task_result_verdict_comes_from_its_outcome(self):
        service = self._service()
        owner = so.SpeakerOwner(AsyncMock(), AsyncMock())
        service.attach_speaker_owner(owner)
        since = time.monotonic() - 1
        self.assertIsNone(service._owner_result_verdict(since))

        def result(state, reason=None, sent=100):
            utt = so.Utterance(1, so.RESULT, "t", time.monotonic(), False)
            utt.state, utt.reason, utt.sent, utt.total = state, reason, sent, 100
            return utt
        service._owner_results = [result(so.DONE)]
        self.assertEqual(service._owner_result_verdict(since), "heard")
        service._owner_results = [result(so.CUT, "stop")]
        self.assertEqual(service._owner_result_verdict(since), "stopped")
        service._owner_results = [result(so.LOST, "link", sent=50)]
        self.assertEqual(service._owner_result_verdict(since), "lost")
        service._owner_results = [result(so.DONE)]
        self.assertIsNone(service._owner_result_verdict(time.monotonic() + 5), "older ones")


if __name__ == "__main__":
    unittest.main()
