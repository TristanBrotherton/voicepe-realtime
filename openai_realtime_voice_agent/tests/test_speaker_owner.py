"""The speaker owner (app/speaker_owner.py): unit tests on a recording sink,
then end-to-end runs against the fake device (tests/fake_device.py) on a
virtual clock: a fixed matrix plus seeded random scenarios, both framings,
checked for properties P1, P2, P5, P6 and P7 of the design."""
import asyncio
import json
import logging
import os
import unittest

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

    def test_a_missing_done_is_assumed_and_flagged(self):
        async def go():
            owner, sink = owner_with_sink()
            utt = owner.submit(so.REPLY, pcm(300))
            await asyncio.sleep(0.2 + 0.3 + so.ACK_OVERDUE_S + 0.1)
            self.assertEqual((utt.state, utt.estimated), (so.DONE, True))
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


if __name__ == "__main__":
    unittest.main()
