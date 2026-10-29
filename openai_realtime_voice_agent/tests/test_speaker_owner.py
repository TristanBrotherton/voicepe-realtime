"""The speaker owner (app/speaker_owner.py): unit tests on a recording sink,
then end-to-end runs against the fake device (tests/fake_device.py) on a
virtual clock: a fixed matrix plus seeded random scenarios, both framings,
checked for properties P1, P2, P5, P6 and P7 of the design."""
import asyncio
import json
import logging
import os
import unittest
from types import SimpleNamespace

from app import speaker_owner as so
from tests import fake_device as fd
from tests.fake_device import Item, Scenario, check_properties, run_scenario, run_virtual


_OWNER_LOG = logging.getLogger("app.speaker_owner")
_OWNER_LOG_LEVEL = _OWNER_LOG.level


def setUpModule():
    # One `utt` line per utterance over hundreds of scenarios is noise here.
    _OWNER_LOG.setLevel(logging.WARNING)


def tearDownModule():
    _OWNER_LOG.setLevel(_OWNER_LOG_LEVEL)


class Sink:
    """Records every frame the owner writes, with its virtual send time."""

    def __init__(self):
        self.frames = []
        self.loop = asyncio.get_event_loop()

    async def text(self, data):
        self.frames.append((self.loop.time(), json.loads(data)))

    async def binary(self, data):
        self.frames.append((self.loop.time(), bytes(data)))

    def texts(self, kind=None):
        return [f for _, f in self.frames if isinstance(f, dict) and (kind is None or f["type"] == kind)]

    def audio_bytes(self):
        return sum(len(f) for _, f in self.frames if isinstance(f, bytes))


def owner_with_sink(framing=so.UTT, **kwargs):
    sink = Sink()
    loop = asyncio.get_event_loop()
    owner = so.SpeakerOwner(sink.text, sink.binary, framing=framing, clock=loop.time, **kwargs)
    return owner, sink


def pcm(ms, value=1):
    return int(value).to_bytes(2, "little", signed=True) * (24 * ms)


class TestWire(unittest.TestCase):
    def test_utt_framing_brackets_each_utterance(self):
        async def go():
            owner, sink = owner_with_sink()
            utt = owner.submit(so.CLIP, pcm(500), turn="t1")
            await asyncio.sleep(1.0)
            kinds = [f["type"] if isinstance(f, dict) else "pcm" for _, f in sink.frames]
            self.assertEqual(kinds[0], "utt_start")
            self.assertEqual(kinds[-1], "utt_end")
            self.assertEqual(set(kinds[1:-1]), {"pcm"})
            self.assertEqual(sink.texts("utt_start")[0], {"type": "utt_start", "id": utt.id,
                                                         "kind": "clip", "turn": "t1"})
            self.assertEqual(sink.texts("utt_end")[0], {"type": "utt_end", "id": utt.id,
                                                       "bytes": 24000})
            self.assertTrue(all(len(f) <= so.FRAME_BYTES for _, f in sink.frames
                                if isinstance(f, bytes)))
            self.assertEqual(sink.audio_bytes(), 24000)
            await owner.shutdown()
        run_virtual(go())

    def test_no_message_carries_a_value_key(self):
        """The firmware's phase parser matches "value" in any text frame."""
        async def go():
            owner, sink = owner_with_sink()
            owner.submit(so.ANNOUNCE, pcm(300))
            await asyncio.sleep(0.1)
            owner.interrupt(3, "stop")
            await owner.acknowledge_interrupt(3)
            await asyncio.sleep(0.5)
            self.assertTrue(sink.texts())
            for message in sink.texts():
                self.assertNotIn("value", message)
            with self.assertRaises(ValueError):
                await owner._text({"type": "x", "value": "idle"})
            await owner.shutdown()
        run_virtual(go())

    def test_legacy_framing_is_plain_audio_with_a_clip_lead_in_when_cold(self):
        async def go():
            owner, sink = owner_with_sink(so.LEGACY)
            owner.submit(so.CLIP, pcm(300))
            await asyncio.sleep(1.0)
            self.assertEqual(sink.texts(), [])
            self.assertEqual(sink.audio_bytes(), 48 * (300 + so.LEGACY_LEAD_IN_MS))
            first = next(f for _, f in sink.frames)
            self.assertEqual(set(first), {0})
            await owner.shutdown()
        run_virtual(go())

    def test_framing_follows_the_device_caps(self):
        owner = so.SpeakerOwner(None, None)
        self.assertEqual(owner.framing, so.LEGACY)
        self.assertEqual(owner.set_framing(["ireason1", "utt1"]), so.UTT)
        self.assertEqual(owner.set_framing(["ireason1"]), so.LEGACY)
        self.assertEqual(owner.set_framing(None), so.LEGACY)


class TestOrder(unittest.TestCase):
    def test_one_acknowledgement_per_turn(self):
        async def go():
            owner, sink = owner_with_sink()
            ack = owner.submit(so.ACK, pcm(400), turn="t1")
            await asyncio.sleep(0.05)
            clip = owner.submit(so.CLIP, pcm(400), turn="t1")
            other = owner.submit(so.CLIP, pcm(400), turn="t2")
            await asyncio.sleep(2.0)
            self.assertEqual(clip.state, so.DROPPED)
            self.assertEqual(clip.reason, "one_ack")
            self.assertTrue(ack.started)
            self.assertNotEqual(other.reason, "one_ack")
            await owner.shutdown()
        run_virtual(go())

    def test_a_clip_is_dropped_when_the_answer_is_already_queued(self):
        async def go():
            owner, sink = owner_with_sink()
            playing = owner.open(so.REPLY, turn="t0")
            owner.feed(playing, pcm(200))
            clip = owner.submit(so.CLIP, pcm(400), turn="t1")
            result = owner.submit(so.RESULT, pcm(400), turn="t1")
            owner.end(playing)
            await asyncio.sleep(2.0)
            self.assertEqual(clip.state, so.DROPPED)
            self.assertEqual(clip.reason, "one_ack")
            self.assertTrue(result.started)
            await owner.shutdown()
        run_virtual(go())

    def test_a_late_clip_is_dropped_and_never_cuts_a_reply(self):
        async def go():
            owner, sink = owner_with_sink()
            reply = owner.open(so.REPLY, turn="t1")
            owner.feed(reply, pcm(300))
            clip = owner.submit(so.CLIP, pcm(400), turn="t2")
            await asyncio.sleep(1.0)       # the reply's producer is still open
            owner.feed(reply, pcm(300))
            owner.end(reply)
            await asyncio.sleep(1.0)
            self.assertEqual(clip.state, so.DROPPED)
            self.assertEqual(clip.reason, "late")
            self.assertEqual(reply.sent, reply.total)
            await owner.shutdown()
        run_virtual(go())

    def test_an_announcement_waits_for_the_boundary_then_goes_first(self):
        async def go():
            owner, sink = owner_with_sink()
            reply = owner.open(so.REPLY, turn="t1")
            owner.feed(reply, pcm(300))
            await asyncio.sleep(0.1)
            later = owner.open(so.REPLY, turn="t2")
            owner.feed(later, pcm(300))
            owner.end(later)
            announce = owner.submit(so.ANNOUNCE, pcm(500), turn="")
            await asyncio.sleep(0.2)
            self.assertFalse(announce.started, "never inside the playing utterance")
            owner.feed(reply, pcm(300))
            owner.end(reply)
            await asyncio.sleep(3.0)
            starts = [f["id"] for f in sink.texts("utt_start")]
            self.assertEqual(starts, [reply.id, announce.id, later.id])
            await owner.shutdown()
        run_virtual(go())


class TestPacing(unittest.TestCase):
    def test_model_audio_goes_out_as_it_is_fed(self):
        async def go():
            owner, sink = owner_with_sink()
            loop = asyncio.get_event_loop()
            utt = owner.open(so.REPLY, turn="t1")
            fed_at = loop.time()
            owner.feed(utt, pcm(600))
            await asyncio.sleep(0.001)
            self.assertEqual(sink.audio_bytes(), 48 * 600, "no hold, no 1.0x pacer")
            self.assertLess(sink.frames[-1][0] - fed_at, 0.01)
            await owner.shutdown()
        run_virtual(go())

    def test_complete_audio_runs_at_most_the_cap_ahead(self):
        async def go():
            owner, sink = owner_with_sink()
            loop = asyncio.get_event_loop()
            start = loop.time()
            owner.submit(so.ANNOUNCE, pcm(10000))
            await asyncio.sleep(0.01)
            ahead = sink.audio_bytes() / 48000.0
            self.assertLessEqual(ahead, so.CAP_MS / 1000.0 + 0.2 + 0.11)
            await asyncio.sleep(4.0)
            sent = sink.audio_bytes() / 48000.0
            played = loop.time() - start - 0.2
            self.assertLessEqual(sent - played, so.CAP_MS / 1000.0 + 0.11)
            await asyncio.sleep(10.0)
            self.assertEqual(sink.audio_bytes(), 48 * 10000)
            await owner.shutdown()
        run_virtual(go())


class TestInterrupt(unittest.TestCase):
    def test_flush_cuts_the_playing_one_drops_the_rest_and_waits_for_the_ack(self):
        async def go():
            owner, sink = owner_with_sink()
            first = owner.submit(so.RESULT, pcm(1000), turn="t1")
            second = owner.submit(so.REPLY, pcm(1000), turn="t1")
            await asyncio.sleep(0.4)
            owner.on_ack({"type": "utt_ack", "id": first.id, "st": "playing", "fed": 0, "wait_ms": 200})
            result = owner.interrupt(7, "stop", first.id, 9600)
            self.assertEqual(result, {so.CUT: [first.id], so.DROPPED: [second.id]})
            self.assertEqual((first.state, first.reason, first.fed), (so.CUT, "stop", 9600))
            self.assertEqual(second.state, so.DROPPED)
            self.assertEqual(owner.state, so.INTERRUPTED)
            sent = len(sink.frames)
            later = owner.submit(so.REPLY, pcm(300), turn="t2")
            await asyncio.sleep(0.5)
            self.assertEqual(len(sink.frames), sent, "nothing is sent until interrupt_ack")
            await owner.acknowledge_interrupt(7)
            await asyncio.sleep(1.0)
            self.assertEqual(sink.texts("interrupt_ack"), [{"type": "interrupt_ack", "seq": 7}])
            self.assertTrue(later.started)
            await owner.shutdown()
        run_virtual(go())

    def test_a_started_utterance_is_never_cut_by_the_add_on(self):
        """Only interrupt() and link_lost() end a started utterance."""
        async def go():
            owner, sink = owner_with_sink()
            utt = owner.open(so.RESULT, turn="t1")
            owner.feed(utt, pcm(400))
            await asyncio.sleep(0.1)
            owner.submit(so.ANNOUNCE, pcm(300))
            owner.submit(so.CLIP, pcm(300), turn="t1")
            await asyncio.sleep(1.0)
            owner.feed(utt, pcm(400))
            owner.end(utt)
            await asyncio.sleep(1.0)
            self.assertEqual(utt.sent, utt.total)
            await owner.shutdown()
        run_virtual(go())


class TestOutcomes(unittest.TestCase):
    def test_device_acks_are_the_facts(self):
        async def go():
            owner, sink = owner_with_sink()
            events = []
            owner.observers.append(lambda u, e: events.append(e))
            utt = owner.submit(so.PROMPT, pcm(300), turn="t1")
            await asyncio.sleep(0.2)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "playing", "fed": 0, "wait_ms": 210})
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "done", "fed": 14400, "und": 1})
            self.assertEqual((utt.state, utt.fed, utt.device_wait_ms, utt.underruns, utt.estimated),
                             (so.DONE, 14400, 210, 1, False))
            self.assertEqual(events, ["head", "sending", "sent", "playing", "done"])
            await owner.shutdown()
        run_virtual(go())

    def test_legacy_estimates_playing_and_done(self):
        async def go():
            owner, sink = owner_with_sink(so.LEGACY)
            utt = owner.submit(so.ANNOUNCE, pcm(500))
            await asyncio.sleep(0.1)
            self.assertEqual(utt.state, so.SENT)
            await asyncio.sleep(0.2)
            self.assertEqual(utt.state, so.PLAYING)
            await asyncio.sleep(0.5)
            self.assertEqual((utt.state, utt.estimated, utt.fed), (so.DONE, True, 24000))
            self.assertFalse(owner.busy)
            await owner.shutdown()
        run_virtual(go())

    def test_a_missing_done_is_unknown_never_done(self):
        """On a test device an answer the device never finished (last ack
        `playing`, fed 0, no `done`) was logged done, fed 0, est:true. With
        bytes unaccounted for it is `unknown`; a late report from the device
        corrects it."""
        async def go():
            owner, sink = owner_with_sink()
            utt = owner.submit(so.RESULT, pcm(900))
            await asyncio.sleep(0.3)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "playing", "fed": 0, "wait_ms": 200})
            await asyncio.sleep(0.9 + so.ACK_OVERDUE_S + 0.2)
            self.assertEqual((utt.state, utt.estimated, utt.fed, utt.reason),
                             (so.UNKNOWN, True, 0, "no_done"))
            self.assertNotEqual(utt.state, so.DONE)
            self.assertFalse(owner.busy, "an unknown outcome still frees the queue")
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "done", "fed": 43200, "und": 0})
            self.assertEqual((utt.state, utt.fed, utt.estimated), (so.DONE, 43200, False),
                             "the device's late account is the fact")
            self.assertEqual(owner.counters["unknown"], 0)
            self.assertEqual(owner.counters["done"], 1)
            await owner.shutdown()
        run_virtual(go())

    def test_only_the_device_makes_an_utterance_done(self):
        """Progress up to the last byte is not a `done`: the device never
        finished it (its `utt_end` may not have arrived)."""
        async def go():
            owner, sink = owner_with_sink()
            utt = owner.submit(so.REPLY, pcm(300))
            await asyncio.sleep(0.3)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "playing", "fed": 0, "wait_ms": 200})
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "progress", "fed": 14400})
            await asyncio.sleep(0.3 + so.ACK_OVERDUE_S + 0.2)
            self.assertEqual((utt.state, utt.estimated, utt.fed), (so.UNKNOWN, True, 14400))
            await owner.shutdown()
        run_virtual(go())

    def test_estimated_done_never_has_fewer_bytes_than_sent(self):
        """est:true never becomes done with fed < bytes, however the wait
        ends (no acks at all, a playing ack, progress short of the end)."""
        async def go():
            for acks in ([], [("playing", 0)], [("playing", 0), ("progress", 4800)]):
                owner, sink = owner_with_sink()
                utt = owner.submit(so.REPLY, pcm(300))
                await asyncio.sleep(0.3)
                for st, fed in acks:
                    owner.on_ack({"type": "utt_ack", "id": utt.id, "st": st, "fed": fed})
                await asyncio.sleep(0.6 + so.ACK_OVERDUE_S + 0.2)
                self.assertTrue(utt.terminal, acks)
                self.assertFalse(utt.state == so.DONE and utt.estimated and (utt.fed or 0) < utt.total,
                                 acks)
                self.assertEqual(utt.state, so.UNKNOWN, acks)
                await owner.shutdown()
        run_virtual(go())

    def test_a_late_start_or_a_stall_the_device_reports_gets_its_time(self):
        """On a test device the device started 1.9 s after the
        first byte went out and stalled mid-utterance; the add-on gave up on
        its send-time estimate. Progress the device reports moves the
        deadline: the real `done` lands and is the outcome."""
        async def go():
            owner, sink = owner_with_sink()
            utt = owner.submit(so.RESULT, pcm(2700))       # 129600 bytes
            await asyncio.sleep(1.9)                        # late start
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "playing", "fed": 0, "wait_ms": 370})
            await asyncio.sleep(1.0)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "progress", "fed": 52800})
            await asyncio.sleep(1.0)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "progress", "fed": 79200})
            for _ in range(3):                              # stalled: no new bytes
                await asyncio.sleep(1.0)
                owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "progress", "fed": 79200})
            self.assertFalse(utt.terminal, "still within the time the rest takes")
            await asyncio.sleep(0.5)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "progress", "fed": 110000})
            await asyncio.sleep(0.5)
            owner.on_ack({"type": "utt_ack", "id": utt.id, "st": "done", "fed": 129600, "und": 1})
            self.assertEqual((utt.state, utt.fed, utt.estimated), (so.DONE, 129600, False))
            await owner.shutdown()
        run_virtual(go())

    def test_link_loss_marks_everything_unfinished_lost(self):
        async def go():
            owner, sink = owner_with_sink()
            playing = owner.submit(so.RESULT, pcm(1000))
            queued = owner.submit(so.ANNOUNCE, pcm(1000))
            await asyncio.sleep(0.1)
            owner.link_lost()
            self.assertEqual((playing.state, queued.state), (so.LOST, so.LOST))
            late = owner.submit(so.CLIP, pcm(100))
            self.assertEqual(late.state, so.LOST)
            await owner.shutdown()
        run_virtual(go())

    def test_a_stalled_link_loses_an_utterance_whole_instead_of_cutting_it(self):
        async def go():
            owner, sink = owner_with_sink()
            head = owner.open(so.REPLY, turn="t1")
            owner.feed(head, pcm(100))
            waiting = owner.open(so.RESULT, turn="t1")
            for _ in range(31):
                owner.feed(waiting, pcm(1000))
            self.assertEqual((waiting.state, waiting.reason), (so.LOST, "overflow"))
            self.assertNotEqual(head.state, so.LOST)
            await owner.shutdown()
        run_virtual(go())

    def test_waiters_and_busy(self):
        async def go():
            owner, sink = owner_with_sink(so.LEGACY)
            idle = []
            owner.idle_observers.append(lambda: idle.append(True))
            utt = owner.submit(so.COACH, pcm(400))
            self.assertTrue(owner.busy)
            self.assertTrue(owner.must_finish_pending)
            self.assertEqual(await utt.wait_settled(5.0), so.SENT)
            self.assertFalse(owner.must_finish_pending)
            self.assertEqual(await utt.wait_finished(5.0), so.DONE)
            self.assertFalse(owner.busy)
            self.assertEqual(idle, [True])
            await owner.shutdown()
        run_virtual(go())


# ---------------------------------------------------------------------------
# end to end against the fake device
# ---------------------------------------------------------------------------

def matrix(mode):
    """Named scenarios: each is the situation one design property is about."""
    legacy = mode == so.LEGACY
    return {
        "cold reply": Scenario([Item(0, so.REPLY, "t1", 0.2, 2500, streamed=True,
                                     spikes=(0.38, 0.16))], mode=mode),
        "clip into result": Scenario([Item(0, so.CLIP, "t1", 0.2, 900),
                                      Item(1, so.RESULT, "t1", 0.5, 2000, streamed=True)], mode=mode),
        "two acknowledgements": Scenario([Item(0, so.ACK, "t1", 0.2, 700, streamed=True),
                                          Item(1, so.CLIP, "t1", 0.4, 900),
                                          Item(2, so.RESULT, "t1", 1.4, 1500, streamed=True)],
                                         mode=mode),
        "timer during reply": Scenario([Item(0, so.REPLY, "t1", 0.2, 4000, streamed=True),
                                        Item(1, so.ANNOUNCE, "", 1.5, 2000),
                                        Item(2, so.REPLY, "t2", 3.0, 1500, streamed=True)], mode=mode),
        "stop mid result": Scenario([Item(0, so.RESULT, "t1", 0.2, 5000, streamed=True),
                                     Item(1, so.ANNOUNCE, "", 1.0, 1000),
                                     Item(2, so.REPLY, "t2", 4.5 if legacy else 3.5, 1500,
                                          streamed=True)],
                                    interrupts=[(2.7, "stop")], mode=mode),
        "disconnect mid result": Scenario([Item(0, so.RESULT, "t1", 0.2, 4000, streamed=True),
                                           Item(1, so.ANNOUNCE, "", 0.5, 1500)],
                                          disconnect_at=1.6, mode=mode),
        "back to back complete": Scenario([Item(0, so.COACH, "", 0.2, 1500),
                                           Item(1, so.COACH, "", 0.3, 1500),
                                           Item(2, so.ANNOUNCE, "", 0.35, 1500)], mode=mode),
        "long announcement": Scenario([Item(0, so.ANNOUNCE, "", 0.2, 9000),
                                       Item(1, so.REPLY, "t1", 1.0, 1000, streamed=True)], mode=mode),
    }


class TestEndToEnd(unittest.TestCase):
    def _check(self, name, scenario):
        result = run_scenario(scenario)
        self.assertEqual(check_properties(result), [], f"{scenario.mode} {name}")
        return result

    def test_matrix_utt1(self):
        for name, scenario in matrix(so.UTT).items():
            with self.subTest(name):
                self._check(name, scenario)

    def test_matrix_legacy(self):
        for name, scenario in matrix(so.LEGACY).items():
            with self.subTest(name):
                self._check(name, scenario)

    def test_what_each_matrix_case_is_about(self):
        cases = matrix(so.UTT)
        r = self._check("two acknowledgements", cases["two acknowledgements"])
        self.assertEqual((r.utts[1].state, r.utts[1].reason), (so.DROPPED, "one_ack"))
        r = self._check("timer during reply", cases["timer during reply"])
        self.assertEqual(r.heads, [0, 1, 2], "the announcement plays at the boundary, ahead of t2")
        r = self._check("stop mid result", cases["stop mid result"])
        self.assertEqual((r.utts[0].state, r.utts[0].reason), (so.CUT, "stop"))
        self.assertEqual(r.utts[0].fed, r.device.records[r.utts[0].id].fed)
        self.assertEqual(r.utts[2].state, so.DONE, "the next turn plays after interrupt_ack")
        r = self._check("disconnect mid result", cases["disconnect mid result"])
        self.assertEqual(r.utts[0].state, so.LOST)
        r = self._check("clip into result", cases["clip into result"])
        heard, _runs, span = fd.heard_by_key(r)
        self.assertLessEqual(span[1][0] - span[0][1], 0.06, "clip flows into the result")

    def test_seeded_random_scenarios(self):
        """About 200 scenarios per run, half per framing. FAKE_DEVICE_SEEDS
        widens the search (and FAKE_DEVICE_SEED_BASE moves it)."""
        count = int(os.environ.get("FAKE_DEVICE_SEEDS", "100"))
        base = int(os.environ.get("FAKE_DEVICE_SEED_BASE", "0"))
        failures = []
        for mode in (so.UTT, so.LEGACY):
            for seed in range(base, base + count):
                errors = check_properties(run_scenario(fd.random_scenario(seed, mode)))
                if errors:
                    failures.append(f"{mode} seed {seed}: {errors[:3]}")
        self.assertEqual(failures, [])


# ---------------------------------------------------------------------------
# device faults and model pauses (found in the first real-audio test)
# ---------------------------------------------------------------------------

def wired(mode=so.UTT):
    loop = asyncio.get_event_loop()
    device = fd.FakeDevice(mode)
    owner = so.SpeakerOwner(device.send_text, device.send_bytes, framing=mode, clock=loop.time)
    device.attach(owner)
    return owner, device


class TestDeviceFaults(unittest.TestCase):
    def test_a_stall_mid_utterance_still_ends_done_with_every_byte(self):
        """A 2.6 s Wi-Fi stall in the middle of a reply, with the
        device's acks delayed too. The device finishes late; its `done` is
        the outcome (fed == bytes), not an estimate."""
        async def go():
            owner, device = wired()
            utt = owner.open(so.RESULT, turn="t1")
            data = fd.tagged_pcm(1, 2700)
            for i in range(0, len(data), 4800):
                owner.feed(utt, data[i:i + 4800])
                if i == 4800 * 8:
                    device.stall(2.6, uplink=True)
                await asyncio.sleep(0.1)
            owner.end(utt)
            await utt.wait_finished(15.0)
            self.assertEqual((utt.state, utt.fed, utt.estimated), (so.DONE, len(data), False))
            self.assertEqual(device.records[utt.id].state, "done")
            device.stop()
            await owner.shutdown()
        run_virtual(go())

    def test_a_lost_utt_end_is_unknown_and_the_next_utterance_still_plays(self):
        """Older firmware lost a `utt_end` split across two TCP reads: the
        device never finished the utterance. The add-on must not call it
        done, and must not wait forever."""
        async def go():
            owner, device = wired()
            first = owner.open(so.RESULT, turn="t1")
            device.drop_text = lambda m: m.get("type") == "utt_end" and m.get("id") == first.id
            owner.feed(first, fd.tagged_pcm(1, 900))
            owner.end(first)
            await first.wait_finished(10.0)
            self.assertEqual((first.state, first.estimated), (so.UNKNOWN, True))
            device.drop_text = None
            second = owner.submit(so.ANNOUNCE, fd.tagged_pcm(2, 500))
            await second.wait_finished(10.0)
            self.assertEqual((second.state, second.fed), (so.DONE, second.total))
            self.assertEqual((first.state, first.estimated), (so.DONE, False),
                             "the device's late done (at the next start) corrects it")
            device.stop()
            await owner.shutdown()
        run_virtual(go())


class TestQuietGaps(unittest.TestCase):
    """SpeakerOwnerProcessor: a pause inside a reply that is still under way
    keeps its utterance open (no end, no idle); only a dead source or the end
    of the reply ends it."""

    def _processor(self, owner, active, source):
        from app.speaker_owner_pipeline import SpeakerOwnerProcessor
        loop = asyncio.get_event_loop()
        proc = SpeakerOwnerProcessor(owner, reply_active=lambda: active["v"],
                                     source_activity=lambda: source["t"], clock=loop.time)
        frames = []
        proc._emit = lambda frame: frames.append(type(frame).__name__)
        return proc, frames

    @staticmethod
    def _frame(pcm_bytes, kind=so.RESULT, new=False):
        from pipecat.frames.frames import OutputAudioRawFrame
        from app.speaker_owner_pipeline import label
        return label(OutputAudioRawFrame(audio=pcm_bytes, sample_rate=24000, num_channels=1),
                     kind, new, "t1")

    def test_an_answer_after_a_pause_is_not_lost_and_nothing_goes_idle(self):
        """After a result: "One moment, <name>." then 3.4 s of model silence, then
        the answer. The first utterance used to end on the quiet gap, the
        device went idle and the answer was never played (logged done)."""
        async def go():
            loop = asyncio.get_event_loop()
            owner, device = wired()
            idle = []
            owner.idle_observers.append(lambda: idle.append(loop.time()))
            active = {"v": True}
            source = {"t": loop.time()}
            proc, frames = self._processor(owner, active, source)
            ack = fd.tagged_pcm(1, 1000)
            for i in range(0, len(ack), 4800):
                proc._feed(self._frame(ack[i:i + 4800]))
                source["t"] = loop.time()
                await asyncio.sleep(0.1)
            first = proc._open
            for _ in range(34):                 # the model streams silence
                source["t"] = loop.time()
                await asyncio.sleep(0.1)
            self.assertFalse(first.closed, "the pause did not end the reply's utterance")
            self.assertEqual(idle, [], "the owner never went idle mid-reply")
            answer = fd.tagged_pcm(2, 900)
            for i in range(0, len(answer), 4800):
                proc._feed(self._frame(answer[i:i + 4800], new=i == 0))
                source["t"] = loop.time()
                await asyncio.sleep(0.1)
            second = proc._open
            self.assertIsNot(first, second, "the answer opened its own utterance")
            self.assertTrue(first.closed)
            active["v"] = False                 # the reply is over
            await second.wait_finished(10.0)
            await first.wait_finished(1.0)
            self.assertEqual([(u.state, u.fed == u.total) for u in (first, second)],
                             [(so.DONE, True), (so.DONE, True)])
            heard, _runs, _span = fd.heard_by_key(SimpleNamespace(
                device=device, pcm={1: ack, 2: answer}))
            self.assertEqual((heard[1], heard[2]), (ack, answer), "both heard whole")
            self.assertEqual(len(idle), 1, "idle once, after the answer")
            self.assertNotIn("BotStoppedSpeakingFrame", frames[:-1])
            device.stop()
            await proc._stop_tasks()
            await owner.shutdown()
        run_virtual(go())

    def test_a_dead_source_ends_the_utterance_as_truncated(self):
        async def go():
            loop = asyncio.get_event_loop()
            owner, device = wired()
            active = {"v": True}
            source = {"t": loop.time()}
            proc, _frames = self._processor(owner, active, source)
            proc._feed(self._frame(fd.tagged_pcm(1, 300)))
            source["t"] = loop.time()
            utt = proc._open
            from app import speaker_owner_pipeline as sop
            await asyncio.sleep(sop.SOURCE_DEAD_S + 1.0)
            self.assertTrue(utt.closed)
            self.assertTrue(utt.truncated, "a source that died is flagged")
            device.stop()
            await proc._stop_tasks()
            await owner.shutdown()
        run_virtual(go())

    def test_a_quiet_gap_after_the_reply_ends_the_utterance(self):
        async def go():
            loop = asyncio.get_event_loop()
            owner, device = wired()
            active = {"v": False}
            source = {"t": loop.time()}
            proc, _frames = self._processor(owner, active, source)
            proc._feed(self._frame(fd.tagged_pcm(1, 300)))
            utt = proc._open
            from app import speaker_owner_pipeline as sop
            for _ in range(int(sop.SOURCE_QUIET_S * 10) + 3):
                source["t"] = loop.time()
                await asyncio.sleep(0.1)
            self.assertTrue(utt.closed)
            self.assertFalse(utt.truncated)
            device.stop()
            await proc._stop_tasks()
            await owner.shutdown()
        run_virtual(go())


if __name__ == "__main__":
    unittest.main()
