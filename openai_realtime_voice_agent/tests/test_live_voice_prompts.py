"""Spoken prompts rendered in the assistant's own Live voice.

No provider: the render session runs on a fake socket that answers the way
the real endpoint did on 2026-10-11 (an interjection such as "Hmm." before
the verbatim line, 200 ms transcript blocks on the session clock, continuous
PCM), and the spend ledger is the real class on a temporary file.
"""
import asyncio
import base64
import json
import tempfile
import time
import unittest
from unittest import mock

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from app import live_voice_prompts as lvp
from app.live_cost import LiveSpendLedger
from app.live_voice_prompts import (
    LiveVoiceRenderer,
    LiveVoiceUnavailable,
    cut_line,
    normalize_line,
)
from tests import live_fixtures as fx

RATE = 24000


def ms(n):
    return RATE * 2 * n // 1000


def transcript(text, start_ms, end_ms):
    return (text, start_ms, end_ms)


class CutLine(unittest.TestCase):
    def test_normalization_folds_case_punctuation_and_apostrophes(self):
        self.assertEqual(normalize_line("I’ll turn that OFF, for you."),
                         normalize_line("i'll turn that off for you"))

    def test_the_interjection_before_the_line_is_discarded(self):
        # 0-400 ms "Hmm." audio, 400-800 quiet, 800-1600 the line, then quiet.
        timeline = fx.tone(400) + fx.silence(400) + fx.tone(800, freq=330) + fx.silence(600)
        deltas = [transcript("Hmm.", 0, 200), transcript(" One", 800, 1000),
                  transcript(" moment.", 1200, 1400)]
        pcm = cut_line(timeline, deltas, "One moment.")
        self.assertIsNotNone(pcm)
        # Starts just ahead of the line's first block, never inside "Hmm.".
        self.assertGreaterEqual(len(pcm), ms(800 + 40))
        self.assertLess(len(pcm), ms(400 + 400 + 800 + 600))
        self.assertEqual(fx.samples_of(timeline[ms(800):ms(1600)])[100:110].tolist(),
                         fx.samples_of(pcm[ms(40):ms(840)])[100:110].tolist(),
                         "the kept bytes are the line's own audio")

    def test_the_cut_runs_past_the_last_block_until_the_output_is_quiet(self):
        # Transcript says the line ends at 600 ms; the voice keeps going to 1100.
        timeline = fx.tone(1100) + fx.silence(800)
        pcm = cut_line(timeline, [transcript("Setting the timer.", 0, 600)], "Setting the timer.")
        self.assertIsNotNone(pcm)
        self.assertGreaterEqual(len(pcm), ms(1100))

    def test_a_transcript_that_is_not_the_line_yields_nothing(self):
        timeline = fx.tone(1000) + fx.silence(600)
        for deltas in ([transcript("Okay.", 0, 200)],
                       [transcript("One moment, I'll check.", 0, 800)],
                       [transcript("One", 0, 200)],
                       []):
            with self.subTest(deltas=deltas):
                self.assertIsNone(cut_line(timeline, deltas, "One moment."))

    def test_a_transcript_with_no_audible_audio_yields_nothing(self):
        timeline = fx.silence(2000)
        self.assertIsNone(cut_line(timeline, [transcript("One moment.", 0, 400)], "One moment."))

    def test_a_transcript_without_timestamps_yields_nothing(self):
        timeline = fx.tone(1000) + fx.silence(600)
        self.assertIsNone(cut_line(timeline, [transcript("One moment.", None, None)], "One moment."))


class FakeRenderSocket:
    """The endpoint as observed: interjection, delegation, verbatim line."""

    def __init__(self, behaviour=None):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False
        self.position_ms = 0
        self.behaviour = behaviour or (lambda line, attempt: line)

    async def send(self, data):
        payload = json.loads(data)
        self.sent.append(payload)
        kind = payload["type"]
        if kind == "session.start":
            self.incoming.put_nowait(json.dumps({"type": "session.started",
                                                 "session": {"id": "render_1", "expires_at": 0}}))
        elif kind == "session.commentary.append":
            # Client delegation: the line goes in as commentary, the endpoint
            # acknowledges it, and the voice then says it.
            line = payload["content"]
            attempt = sum(1 for m in self.sent if m["type"] == "session.commentary.append"
                          and m["content"] == line)
            self.incoming.put_nowait(json.dumps({"type": "session.commentary.appended",
                                                 "event_id": payload.get("event_id")}))
            self._speak(self.behaviour(line, attempt))
        elif kind == "session.close":
            self.incoming.put_nowait(json.dumps({"type": "session.closed"}))

    def _emit_audio(self, pcm):
        for i in range(0, len(pcm), fx.DELTA_BYTES):
            chunk = pcm[i:i + fx.DELTA_BYTES]
            self.incoming.put_nowait(json.dumps({"type": "session.output_audio.delta",
                                                 "delta": base64.b64encode(chunk).decode()}))
            self.position_ms += len(chunk) * 1000 // (RATE * 2)

    def _emit_text(self, text, duration_ms):
        start = self.position_ms
        self.incoming.put_nowait(json.dumps({"type": "session.output_transcript.delta",
                                             "delta": text, "start_ms": start,
                                             "end_ms": start + duration_ms}))

    def _speak(self, spoken):
        # Interjection first, as the frontend does, then the backend answer.
        self._emit_text("Hmm.", 200)
        self._emit_audio(fx.tone(300) + fx.silence(300))
        self.incoming.put_nowait(json.dumps({"type": "session.delegation.created",
                                             "delegation": {"id": "d", "target": "responses"}}))
        if spoken:
            words = spoken.split()
            for index, word in enumerate(words):
                self._emit_text((" " if index else "") + word, 200)
                self._emit_audio(fx.tone(200, freq=330))
        self.incoming.put_nowait(json.dumps({"type": "response.event", "delegation_id": "d",
                                             "event": {"type": "response.completed",
                                                       "response": {"id": "r", "output": []}}}))
        self._emit_audio(fx.silence(1500))

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message


class OffsetClock:
    """Real monotonic time plus an offset the test moves forward."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


class SocketClosedBySecondLine(FakeRenderSocket):
    """The endpoint closes after the first line; the transport then raises
    ``ConnectionClosed`` from ``send``, as websockets does."""

    def __init__(self):
        super().__init__()
        self.lines = 0

    async def send(self, data):
        payload = json.loads(data)
        if payload["type"] == "session.commentary.append":
            self.lines += 1
            if self.lines >= 2:
                self.closed = True
                raise ConnectionClosedOK(None, None)
        if payload["type"] == "session.close" and self.closed:
            raise ConnectionClosedOK(None, None)
        await super().send(data)


class SocketThePeerDrops(FakeRenderSocket):
    """The peer closes while nothing is being sent: the reader sees a close
    event and EOF, and every later ``send`` raises ``ConnectionClosed``."""

    def server_drops(self):
        self.closed = True
        self.incoming.put_nowait(json.dumps({"type": "session.closed"}))
        self.incoming.put_nowait(None)

    async def send(self, data):
        if self.closed:
            raise ConnectionClosedError(None, None)
        await super().send(data)


class SocketThatDiesAfterTheLine(FakeRenderSocket):
    """The peer drops the connection right after the line completes, in the
    quiet the renderer waits through before the next line."""

    def _speak(self, spoken):
        super()._speak(spoken)
        self.closed = True
        self.incoming.put_nowait(None)

    async def send(self, data):
        if self.closed:
            raise ConnectionClosedError(None, None)
        await super().send(data)


class SocketThatVoicesAfterCompletion(FakeRenderSocket):
    """The order production shows: the frontend's interjection, then the
    delegated backend's ``response.completed``, continuous silence, and only
    then, seconds later, the frontend voicing the backend's text (about 3.7 s
    in the retained canary turn)."""

    VOICE_DELAY_S = 3.7

    def _speak(self, spoken):
        self._emit_text("Hmm.", 200)
        self._emit_audio(fx.tone(300) + fx.silence(300))
        self.incoming.put_nowait(json.dumps({"type": "session.delegation.created",
                                             "delegation": {"id": "d", "target": "responses"}}))
        self.incoming.put_nowait(json.dumps({"type": "response.event", "delegation_id": "d",
                                             "event": {"type": "response.completed",
                                                       "response": {"id": "r", "output": []}}}))
        self._emit_audio(fx.silence(1300))

        def voice():
            for index, word in enumerate(spoken.split()):
                self._emit_text((" " if index else "") + word, 200)
                self._emit_audio(fx.tone(200, freq=330))
            self._emit_audio(fx.silence(1500))

        asyncio.get_running_loop().call_later(self.VOICE_DELAY_S, voice)


class Rendering(unittest.IsolatedAsyncioTestCase):
    def make(self, behaviour=None, ledger=None, connect=None, socket_class=None, **kw):
        sockets = []

        async def fake_connect(uri, additional_headers=None, **_):
            socket = (socket_class or FakeRenderSocket)(behaviour)
            sockets.append(socket)
            return socket

        kw.setdefault("line_timeout_s", 3.0)
        renderer = LiveVoiceRenderer(
            "sk-test", model="gpt-live-1", backend_model="gpt-6-luna", voice="vesper",
            ledger=ledger, connect=connect or fake_connect, linger_s=0.0, **kw)
        renderer.sockets = sockets
        return renderer

    async def test_lines_come_back_verbatim_in_one_session_and_the_interjection_is_cut(self):
        renderer = self.make()
        with self.assertLogs("app.live_voice_prompts", level="INFO") as logs:
            result = await renderer.render(["One moment.", "Let me check that."])
        self.assertEqual(set(result), {"One moment.", "Let me check that."})
        for line, pcm in result.items():
            # One 200 ms tone per word, plus the 40 ms lead and the 300 ms
            # quiet tail: never the 300 ms "Hmm." tone and its 300 ms gap,
            # which would add 600 ms.
            words = len(line.split())
            self.assertGreaterEqual(len(pcm), ms(200 * words + 300))
            self.assertLess(len(pcm), ms(200 * words + 340 + 300))
        self.assertEqual(len(renderer.sockets), 1, "one session for the whole set")
        self.assertTrue(renderer.sockets[0].closed)
        self.assertIn("session.close", [m["type"] for m in renderer.sockets[0].sent])
        self.assertTrue(any("render session opened" in m for m in logs.output))
        self.assertTrue(any("render session closed" in m for m in logs.output))

    async def test_the_session_start_names_the_voice_and_carries_no_tools(self):
        renderer = self.make()
        await renderer.render(["One moment."])
        start = renderer.sockets[0].sent[0]
        self.assertEqual(start["type"], "session.start")
        self.assertEqual(start["session"]["audio"]["output"]["voice"], "vesper")
        self.assertEqual(start["session"]["delegation"], {"type": "client"},
                         "lines go in as commentary; the voice gets no tools and no backend")
        self.assertEqual(len(start["session"]["input"]), 2 * len(lvp.RENDER_EXAMPLES))

    async def test_an_unverbatim_line_is_retried_then_left_out(self):
        def paraphrase(line, attempt):
            return "Just a second." if line == "One moment." else line

        renderer = self.make(paraphrase, max_attempts=2)
        with self.assertLogs("app.live_voice_prompts", level="INFO") as logs:
            result = await renderer.render(["One moment.", "Setting the timer."])
        self.assertEqual(set(result), {"Setting the timer."})
        self.assertEqual(renderer.unverified, 1)
        attempts = [m for m in renderer.sockets[0].sent
                    if m["type"] == "session.commentary.append"
                    and m["content"] == "One moment."]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(any("did not render a prompt verbatim" in m for m in logs.output))

    async def test_a_line_voiced_after_the_backend_completes_is_rendered(self):
        renderer = self.make(socket_class=SocketThatVoicesAfterCompletion,
                             line_timeout_s=10.0, max_attempts=1)
        result = await renderer.render(["One moment."])
        self.assertIn("One moment.", result)
        pcm = result["One moment."]
        # The two words and the quiet tail, never the "Hmm." tone before them.
        self.assertGreaterEqual(len(pcm), ms(400 + 300))
        self.assertLess(len(pcm), ms(400 + 340 + 300))
        sent = [m for m in renderer.sockets[0].sent if m["type"] == "session.commentary.append"]
        self.assertEqual(len(sent), 1, "rendered on the first attempt")

    async def test_a_line_voiced_in_the_quiet_after_an_attempt_is_kept(self):
        # An attempt judged before the voice spoke (here a short settle): the
        # line's transcript then arrives while the renderer waits for quiet,
        # and must still be cut rather than dropped.
        class VoicesAfterOneSecond(SocketThatVoicesAfterCompletion):
            VOICE_DELAY_S = 1.0

        renderer = self.make(socket_class=VoicesAfterOneSecond,
                             line_timeout_s=10.0, max_attempts=1)
        with mock.patch.object(lvp, "ANSWER_SETTLE_S", 0.5, create=True):
            result = await renderer.render(["One moment."])
        self.assertIn("One moment.", result)
        sent = [m for m in renderer.sockets[0].sent if m["type"] == "session.commentary.append"]
        self.assertEqual(len(sent), 1)

    async def test_a_render_that_gets_nothing_verbatim_stops_after_a_few_lines(self):
        renderer = self.make(lambda line, attempt: "Sure thing.", max_attempts=1)
        lines = ["One moment.", "Stopping that.", "Setting the timer.",
                 "Updating the list.", "Adjusting that now.", "Let me check that."]
        with self.assertLogs("app.live_voice_prompts", level="INFO") as logs:
            result = await renderer.render(lines)
        self.assertEqual(result, {})
        tried = [m["content"] for m in renderer.sockets[0].sent
                 if m["type"] == "session.commentary.append"]
        self.assertEqual(tried, lines[:3], "three unverified lines in a row end the render")
        self.assertTrue(renderer.sockets[0].closed)
        self.assertTrue(any("in a row verbatim" in m for m in logs.output))

    async def test_a_second_attempt_that_is_verbatim_is_kept(self):
        def flaky(line, attempt):
            return "Okay." if attempt == 1 else line

        renderer = self.make(flaky, max_attempts=3)
        result = await renderer.render(["One moment."])
        self.assertIn("One moment.", result)

    async def test_the_session_is_charged_on_the_ledger_under_its_own_device_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = LiveSpendLedger(max_session_minutes_per_day=20, max_usd_per_day=1.0,
                                     path=f"{tmp}/ledger.json")
            renderer = self.make(ledger=ledger)
            await renderer.render(["One moment."])
            self.assertEqual(ledger.sessions_total, 1)
            self.assertEqual(ledger._history[-1].device_id, "prompt-render")
            self.assertIn("prompt render", ledger._history[-1].reason)
            self.assertEqual(ledger._active, {}, "the reservation is settled")

    async def test_the_spend_cutoff_refuses_a_render_session_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = LiveSpendLedger(max_session_minutes_per_day=20, max_usd_per_day=0.0001,
                                     path=f"{tmp}/ledger.json")
            from app.live_cost import SessionCharge
            ledger.record(SessionCharge("kitchen", "s", 600.0, "test", ledger.clock()))
            renderer = self.make(ledger=ledger)
            with self.assertRaises(LiveVoiceUnavailable) as caught:
                await renderer.render(["One moment."])
            self.assertEqual(caught.exception.reason, "spend cutoff")
            self.assertEqual(renderer.sockets, [], "no socket is bought")

    async def test_an_unreachable_endpoint_is_named(self):
        async def refuse(uri, additional_headers=None, **_):
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = LiveSpendLedger(max_session_minutes_per_day=20, max_usd_per_day=1.0,
                                     path=f"{tmp}/ledger.json")
            renderer = self.make(ledger=ledger, connect=refuse)
            with self.assertRaises(LiveVoiceUnavailable) as caught:
                await renderer.render(["One moment."])
            self.assertEqual(caught.exception.reason, "unreachable")
            self.assertEqual(ledger._active, {}, "a failed open leaves no reservation")

    async def test_a_lingering_session_serves_the_next_line_and_then_closes(self):
        sockets = []

        async def fake_connect(uri, additional_headers=None, **_):
            socket = FakeRenderSocket()
            sockets.append(socket)
            return socket

        renderer = LiveVoiceRenderer("sk-test", model="gpt-live-1", backend_model="gpt-6-luna",
                                     voice="vesper", connect=fake_connect, linger_s=0.3,
                                     line_timeout_s=3.0)
        await renderer.render(["One moment."])
        await renderer.render(["Setting the timer."])
        self.assertEqual(len(sockets), 1, "two renders inside the linger share a session")
        self.assertFalse(sockets[0].closed)
        await asyncio.sleep(0.5)
        self.assertTrue(sockets[0].closed, "the idle session is closed and settled")

    def ledger_and_renderer(self, tmp, clock, socket_class, **kw):
        sockets = []

        async def connect(uri, additional_headers=None, **_):
            socket = socket_class()
            sockets.append(socket)
            return socket

        ledger = LiveSpendLedger(max_session_minutes_per_day=60, max_usd_per_day=3.0,
                                 path=f"{tmp}/ledger.json", clock=clock)
        renderer = LiveVoiceRenderer("sk-test", model="gpt-live-1", backend_model="gpt-6-luna",
                                     voice="vesper", ledger=ledger, connect=connect,
                                     line_timeout_s=3.0, clock=clock, **kw)
        return ledger, renderer, sockets

    async def test_a_socket_the_endpoint_closes_mid_set_is_closed_and_settled_for_its_real_seconds(self):
        clock = OffsetClock()
        with tempfile.TemporaryDirectory() as tmp:
            ledger, renderer, sockets = self.ledger_and_renderer(
                tmp, clock, SocketClosedBySecondLine, linger_s=0.0)
            with self.assertLogs("app.live_voice_prompts", level="WARNING") as logs:
                result = await renderer.render(["One moment.", "Setting the timer."])
            self.assertEqual(set(result), {"One moment."}, "the line rendered before the drop is kept")
            self.assertIsNone(renderer._session, "the dead session is not kept")
            self.assertEqual(ledger._active, {}, "no reservation is left open")
            self.assertEqual(len(ledger._history), 1)
            self.assertLess(ledger._history[-1].seconds, 5.0, "only the seconds really open are charged")
            self.assertTrue(any("render session lost" in m for m in logs.output))
            clock.offset += 4000
            verdict = ledger.check()
            self.assertTrue(verdict.allowed, verdict.reason)
            self.assertLess(ledger.snapshot()["session_minutes"], 1.0,
                            "an hour with a dropped socket cost nothing")
            result = await renderer.render(["Let me check that."])
            self.assertIn("Let me check that.", result, "the next line gets a fresh session")
            self.assertEqual(len(sockets), 2)
            self.assertEqual(ledger._active, {})
            self.assertTrue(all(c.seconds < 5.0 for c in ledger._history), [c.describe() for c in ledger._history])

    async def test_a_socket_dropped_during_the_linger_is_closed_at_once_and_the_next_line_gets_a_fresh_session(self):
        clock = OffsetClock()
        with tempfile.TemporaryDirectory() as tmp:
            ledger, renderer, sockets = self.ledger_and_renderer(
                tmp, clock, SocketThePeerDrops, linger_s=lvp.LINGER_S)
            self.assertEqual(set(await renderer.render(["One moment."])), {"One moment."})
            self.assertEqual(len(ledger._active), 1, "the lingering session holds its reservation")
            await asyncio.sleep(0.2)
            sockets[0].server_drops()
            await asyncio.sleep(0.3)
            self.assertIsNone(renderer._session, "the dropped session is closed without waiting out the linger")
            self.assertEqual(ledger._active, {}, "its reservation is settled at once")
            self.assertEqual(len(ledger._history), 1)
            self.assertLess(ledger._history[-1].seconds, 5.0)
            result = await renderer.render(["Your timer is done."])
            self.assertIn("Your timer is done.", result, "a dynamic line still renders, on a fresh session")
            self.assertEqual(len(sockets), 2)
            self.assertEqual(len(ledger._active), 1, "the fresh session lingers with its own reservation")
            await renderer.close("shutdown")
            self.assertEqual(ledger._active, {})
            self.assertTrue(all(c.seconds < 5.0 for c in ledger._history), [c.describe() for c in ledger._history])
            clock.offset += 40 * 60
            verdict = ledger.check()
            self.assertTrue(verdict.allowed, verdict.reason)
            self.assertLess(ledger.snapshot()["session_minutes"], 1.0,
                            "forty idle minutes after a dropped socket cost nothing")

    async def test_a_socket_that_dies_in_the_quiet_after_a_line_keeps_the_line_and_closes_the_session(self):
        clock = OffsetClock()
        with tempfile.TemporaryDirectory() as tmp:
            ledger, renderer, sockets = self.ledger_and_renderer(
                tmp, clock, SocketThatDiesAfterTheLine, linger_s=lvp.LINGER_S)
            result = await renderer.render(["One moment.", "Setting the timer."])
            self.assertIn("One moment.", result, "the verbatim line is kept")
            self.assertIsNone(renderer._session)
            self.assertEqual(ledger._active, {}, "EOF in the quiet after a line is not discarded")
            self.assertEqual(len(ledger._history), 1)
            self.assertLess(ledger._history[-1].seconds, 5.0)

    async def test_a_cancel_while_the_session_is_opening_leaves_no_reservation_and_no_socket(self):
        clock = OffsetClock()
        never = asyncio.Event()
        sockets = []

        async def hanging_connect(uri, additional_headers=None, **_):
            await never.wait()

        class NeverStarts(FakeRenderSocket):
            async def send(self, data):
                if json.loads(data)["type"] == "session.start":
                    self.sent.append(json.loads(data))
                    return  # the endpoint never answers session.started
                await super().send(data)

        async def connect_but_never_start(uri, additional_headers=None, **_):
            socket = NeverStarts()
            sockets.append(socket)
            return socket

        with tempfile.TemporaryDirectory() as tmp:
            ledger = LiveSpendLedger(max_session_minutes_per_day=60, max_usd_per_day=3.0,
                                     path=f"{tmp}/ledger.json", clock=clock)
            for label, connect in (("during connect", hanging_connect),
                                   ("before session.started", connect_but_never_start)):
                with self.subTest(label):
                    renderer = LiveVoiceRenderer(
                        "sk-test", model="gpt-live-1", backend_model="gpt-6-luna", voice="vesper",
                        ledger=ledger, connect=connect, linger_s=lvp.LINGER_S,
                        line_timeout_s=3.0, clock=clock)
                    task = asyncio.create_task(renderer.render(["One moment."]))
                    await asyncio.sleep(0.1)
                    self.assertEqual(len(ledger._active), 1, "the reservation is held while opening")
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(ledger._active, {}, f"{label}: the reservation is released")
                    self.assertIsNone(renderer._session)
                    self.assertTrue(all(c.seconds < 5.0 for c in ledger._history))
            self.assertTrue(sockets[0].closed, "the socket that was up is closed")
            await asyncio.sleep(0)
            self.assertTrue(all(task.done() for task in asyncio.all_tasks()
                                if task.get_name() == "live-render-read"),
                            "no reader task outlives the cancel")
            clock.offset += 40 * 60
            self.assertTrue(ledger.check().allowed)
            self.assertLess(ledger.snapshot()["session_minutes"], 1.0,
                            "nothing counts against the device gate afterwards")

    async def test_a_session_at_its_bound_is_closed_not_lingered_and_the_next_line_gets_a_fresh_one(self):
        clock = OffsetClock()

        class ThirtySecondsALine(FakeRenderSocket):
            async def send(self, data):
                if json.loads(data)["type"] == "session.commentary.append":
                    clock.offset += 30.0
                await super().send(data)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(lvp, "QUIET_AFTER_LINE_S", 0.1):
            ledger, renderer, sockets = self.ledger_and_renderer(
                tmp, clock, ThirtySecondsALine, linger_s=lvp.LINGER_S)
            warm = ["a", "b", "c", "d", "e", "f", "g"]
            with self.assertLogs("app.live_voice_prompts", level="INFO") as logs:
                result = await renderer.render(warm)
            self.assertLess(len(result), len(warm), "the set crosses the bound")
            self.assertIsNone(renderer._session, "a session at its bound does not linger")
            self.assertEqual(ledger._active, {})
            self.assertLessEqual(ledger._history[-1].seconds, lvp.MAX_SESSION_S + lvp.RESERVE_MARGIN_S)
            self.assertTrue(any("closed (bound)" in m for m in logs.output))
            result = await renderer.render(["Your timer is done."])
            self.assertIn("Your timer is done.", result, "the next line renders on a fresh session")
            self.assertEqual(len(sockets), 2)

    async def test_a_lingering_session_past_its_bound_is_replaced_not_reused(self):
        clock = OffsetClock()

        class SeventySecondsALine(FakeRenderSocket):
            async def send(self, data):
                if json.loads(data)["type"] == "session.commentary.append":
                    clock.offset += 70.0
                await super().send(data)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(lvp, "QUIET_AFTER_LINE_S", 0.1):
            ledger, renderer, sockets = self.ledger_and_renderer(
                tmp, clock, SeventySecondsALine, linger_s=lvp.LINGER_S)
            self.assertEqual(set(await renderer.render(["One moment.", "Setting the timer."])),
                             {"One moment.", "Setting the timer."})
            self.assertIsNotNone(renderer._session, "140 s in, the session may still linger")
            clock.offset += 20.0  # the bound passes while it lingers
            result = await renderer.render(["Your timer is done."])
            self.assertIn("Your timer is done.", result, "rendered on a fresh session, not the expired one")
            self.assertEqual(len(sockets), 2)
            self.assertTrue(sockets[0].closed)
            self.assertLessEqual(ledger._history[0].seconds, lvp.MAX_SESSION_S + lvp.RESERVE_MARGIN_S)
            self.assertEqual(len(ledger._active), 1, "only the fresh session is reserved")
            await renderer.close()
            self.assertEqual(ledger._active, {})

    async def test_one_line_never_runs_the_session_past_its_reservation(self):
        clock = OffsetClock()

        class SlowUnverbatimSocket(FakeRenderSocket):
            def __init__(self):
                super().__init__(lambda line, attempt: "Not it.")

            async def send(self, data):
                if json.loads(data)["type"] == "session.commentary.append":
                    clock.offset += 20.0  # every attempt costs 20 s on the session clock
                await super().send(data)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(lvp, "QUIET_AFTER_LINE_S", 0.1):
            ledger, renderer, sockets = self.ledger_and_renderer(
                tmp, clock, SlowUnverbatimSocket, linger_s=0.0, max_attempts=3)
            reserved = []
            original = ledger.reserve

            def spy(maximum_seconds, reader, minimum_seconds=15.0):
                reserved.append(maximum_seconds)
                return original(maximum_seconds, reader, minimum_seconds)

            ledger.reserve = spy
            await renderer.render(["a", "b", "c", "d"])
            self.assertEqual(reserved, [lvp.MAX_SESSION_S + lvp.RESERVE_MARGIN_S])
            self.assertEqual(ledger._active, {})
            charged = ledger._history[-1].seconds
            self.assertLessEqual(charged, reserved[0],
                                 "a line that straddles the bound ends at the bound, inside the reservation")
            self.assertGreaterEqual(charged, lvp.MAX_SESSION_S, "the bound is used, not undercut")


class RecordingSocket(FakeRenderSocket):
    """A fake endpoint socket that records when it was really open and can
    misbehave in each of the ways a render session has to survive."""

    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        self.clock = endpoint.clock
        self.mode = endpoint.mode
        self.opened = self.clock()
        self.closed_at = None
        self.items = 0
        self.release = asyncio.Event()

    @property
    def real_seconds(self):
        end = self.closed_at if self.closed_at is not None else self.clock()
        return end - self.opened

    def drop(self, event=None):
        """The peer goes away: an optional last event, then EOF."""
        if self.closed_at is None:
            self.closed_at = self.clock()
        self.closed = True
        if event:
            self.incoming.put_nowait(json.dumps(event))
        self.incoming.put_nowait(None)

    async def send(self, data):
        payload = json.loads(data)
        kind = payload["type"]
        if self.closed:
            raise ConnectionClosedError(None, None)
        if kind == "session.start" and self.mode == "never_started":
            self.sent.append(payload)
            return
        if kind == "session.commentary.append":
            self.items += 1
        if kind == "session.commentary.append":
            self.clock.offset += self.endpoint.attempt_cost_s
            if self.mode == "silent_line":
                self.sent.append(payload)
                return
            if self.mode == "send_hangs":
                # Transport backpressure: the send returns only when the
                # socket is closed under it, as websockets does.
                self.sent.append(payload)
                await self.release.wait()
                raise ConnectionClosedError(None, None)
            if self.mode == "closed_on_line2" and self.items >= 2:
                self.drop()
                raise ConnectionClosedError(None, None)
            if self.mode == "reader_raises" and self.items >= 2:
                self.sent.append(payload)
                self.incoming.put_nowait("RAISE")
                return
        await super().send(data)
        if kind == "session.commentary.append" and self.items == 1:
            if self.mode == "eof_after_line":
                self.drop()
            elif self.mode == "error_after_line":
                self.incoming.put_nowait(json.dumps({"type": "error", "error": {"message": "late"}}))

    async def __anext__(self):
        message = await self.incoming.get()
        if message == "RAISE":
            self.drop()
            raise ConnectionClosedError(None, None)
        if message is None:
            raise StopAsyncIteration
        return message

    async def close(self):
        if self.closed_at is None:
            self.closed_at = self.clock()
        self.release.set()
        if self.endpoint.close_delay_s:
            await asyncio.sleep(self.endpoint.close_delay_s)
        await super().close()


class Endpoint:
    def __init__(self, clock):
        self.clock = clock
        self.mode = None
        self.close_delay_s = 0.0
        self.attempt_cost_s = 0.0
        self.sockets = []

    async def connect(self, uri, additional_headers=None, **_):
        if self.mode == "hang_connect":
            await asyncio.Event().wait()
        socket = RecordingSocket(self)
        self.sockets.append(socket)
        return socket


async def cancelled(task):
    try:
        await task
    except asyncio.CancelledError:
        return True
    except LiveVoiceUnavailable:
        return False
    return False


class RenderSessionLifecycle(unittest.IsolatedAsyncioTestCase):
    """Every way a render session can end, against the real ledger.

    Each scenario drives the real renderer with the production linger, then
    the same invariants are checked: while idle, the only reservation on the
    ledger belongs to the renderer's current open session; after a shutdown
    there is no reservation, no render task, no open socket; every
    reservation was settled exactly once; no charge exceeds the seconds its
    socket was really open or the session's reservation; a restart from the
    persisted ledger charges exactly what was settled; and a device-sized
    session can still be reserved.
    """

    CAP_MINUTES = 60
    CAP_USD = 3.0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = f"{self._tmp.name}/ledger.json"
        self.clock = OffsetClock()
        self.endpoint = Endpoint(self.clock)
        self.ledger = LiveSpendLedger(max_session_minutes_per_day=self.CAP_MINUTES,
                                      max_usd_per_day=self.CAP_USD, path=self.path,
                                      clock=self.clock)
        self.reserved, self.settled = [], []
        reserve, settle = self.ledger.reserve, self.ledger.settle

        def reserve_spy(maximum_seconds, reader, minimum_seconds=15.0):
            owner = reserve(maximum_seconds, reader, minimum_seconds)
            if owner is not None:
                self.reserved.append(owner)
            return owner

        def settle_spy(owner, charge):
            self.settled.append(owner)
            return settle(owner, charge)

        self.ledger.reserve, self.ledger.settle = reserve_spy, settle_spy
        self.renderer = LiveVoiceRenderer(
            "sk-test", model="gpt-live-1", backend_model="gpt-6-luna", voice="vesper",
            ledger=self.ledger, connect=self.endpoint.connect, linger_s=lvp.LINGER_S,
            line_timeout_s=3.0, clock=self.clock)
        self.quiet = mock.patch.object(lvp, "QUIET_AFTER_LINE_S", 0.1)
        self.quiet.start()

    async def asyncTearDown(self):
        self.quiet.stop()
        for task in asyncio.all_tasks():
            if task.get_name().startswith("live-render") and not task.done():
                task.cancel()
        self._tmp.cleanup()

    def live_render_tasks(self):
        return [t for t in asyncio.all_tasks()
                if t.get_name().startswith("live-render") and not t.done()]

    async def assert_lifecycle_invariants(self, device_room=True):
        await asyncio.sleep(0.4)
        session = self.renderer._session
        if self.ledger._active:
            self.assertIsNotNone(session, "a reservation with no current session is a leak")
            self.assertEqual(len(self.ledger._active), 1, "only the current session may hold a reservation")
            self.assertLess(self.clock(), session.deadline_mono,
                            "a session past its bound still holds its reservation")
            self.assertLessEqual(self.ledger.active_seconds, session.open_seconds + 0.5,
                                 "the reservation reads only its own session")
        await self.renderer.close("shutdown")
        self.assertEqual(self.ledger._active, {}, "a shutdown leaves no reservation")
        self.assertIsNone(self.renderer._session)
        self.assertEqual(self.live_render_tasks(), [], "no render task outlives the shutdown")
        self.assertTrue(all(s.closed for s in self.endpoint.sockets), "every socket bought is closed")
        self.assertEqual(sorted(self.settled), sorted(self.reserved),
                         "every reservation is settled exactly once")
        charges = [c for c in self.ledger._history if c.device_id == "prompt-render"]
        bound = lvp.MAX_SESSION_S + lvp.RESERVE_MARGIN_S
        for charge in charges:
            self.assertLessEqual(charge.seconds, bound, charge.describe())
        real = sum(s.real_seconds for s in self.endpoint.sockets)
        self.assertLessEqual(sum(c.seconds for c in charges), real + 0.5 * len(charges) + 0.5,
                             "only seconds a socket was really open are charged")
        self.assertGreaterEqual(sum(c.seconds for c in charges), real - 0.5 * len(charges) - 0.5,
                                "every second a socket was really open is charged")
        restarted = LiveSpendLedger(max_session_minutes_per_day=self.CAP_MINUTES,
                                    max_usd_per_day=self.CAP_USD, path=self.path, clock=self.clock)
        self.assertAlmostEqual(restarted.window_seconds, self.ledger.window_seconds, delta=0.01,
                               msg="a restart charges exactly what was settled")
        if device_room:
            self.assertTrue(restarted.check().allowed)
            self.assertIsNotNone(restarted.reserve(900, lambda: 0.0),
                                 "a device-sized session can still be reserved")

    # -- the open ------------------------------------------------------------

    async def test_a_cancel_during_connect(self):
        self.endpoint.mode = "hang_connect"
        task = asyncio.create_task(self.renderer.render(["Hold still and say your name."]))
        await asyncio.sleep(0.1)
        task.cancel()
        self.assertTrue(await cancelled(task))
        await self.assert_lifecycle_invariants()

    async def test_a_cancel_after_connect_before_the_session_started(self):
        self.endpoint.mode = "never_started"
        task = asyncio.create_task(self.renderer.render(["Hold still and say your name."]))
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.endpoint.sockets), 1)
        self.clock.offset += 5.0
        self.assertGreaterEqual(self.ledger.active_seconds, 5.0,
                                "a socket bought while opening counts against the gate")
        task.cancel()
        self.assertTrue(await cancelled(task))
        await self.assert_lifecycle_invariants()

    async def test_a_second_cancel_while_the_first_is_being_handled(self):
        self.endpoint.mode = "never_started"
        task = asyncio.create_task(self.renderer.render(["Hold still and say your name."]))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await cancelled(task)
        await self.assert_lifecycle_invariants()

    async def test_repeated_cancelled_opens_leave_room_for_a_device_session(self):
        self.endpoint.mode = "hang_connect"
        for _ in range(25):
            task = asyncio.create_task(self.renderer.render(["line"]))
            await asyncio.sleep(0.02)
            task.cancel()
            self.assertTrue(await cancelled(task), "every open is cancellable")
        self.assertEqual(self.ledger._active, {})
        self.endpoint.mode = None
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        await self.assert_lifecycle_invariants()

    async def test_a_shutdown_while_a_session_is_opening(self):
        self.endpoint.mode = "never_started"
        task = asyncio.create_task(self.renderer.render(["One moment."]))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        await self.renderer.close("shutdown")
        self.assertLess(time.monotonic() - started, 2.0, "the shutdown does not wait out the open timeout")
        self.assertEqual(self.ledger._active, {})
        await asyncio.gather(task, return_exceptions=True)
        await self.assert_lifecycle_invariants()

    # -- the render ----------------------------------------------------------

    async def test_a_cancel_mid_line(self):
        self.endpoint.mode = "silent_line"
        task = asyncio.create_task(self.renderer.render(["One moment."]))
        await asyncio.sleep(0.5)
        task.cancel()
        self.assertTrue(await cancelled(task))
        self.assertIsNone(self.renderer._session, "a cancelled render closes its session at once")
        self.assertEqual(self.ledger._active, {})
        await self.assert_lifecycle_invariants()

    async def test_an_endpoint_error_ends_the_session_and_its_seconds_are_charged(self):
        self.endpoint.mode = "error_after_line"
        self.endpoint.attempt_cost_s = 30.0
        lines = ["One moment.", "Setting the timer.", "Let me check that.", "Your timer is done."]
        result = await self.renderer.render(lines)
        self.assertEqual(set(result), {"One moment."}, "the verbatim line before the error is kept")
        self.assertEqual(self.endpoint.sockets[0].items, 1, "no line is sent after the error")
        self.assertEqual(self.ledger._active, {})
        await self.assert_lifecycle_invariants()

    async def test_an_endpoint_error_while_lingering_ends_the_session_at_once(self):
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        self.endpoint.sockets[0].incoming.put_nowait(json.dumps({"type": "error", "error": {"message": "x"}}))
        await asyncio.sleep(0.3)
        self.assertEqual(self.ledger._active, {})
        self.assertIn("Your timer is done.", await self.renderer.render(["Your timer is done."]),
                      "the next line renders on a fresh session")
        await self.assert_lifecycle_invariants()

    async def test_nothing_opens_after_a_shutdown(self):
        await self.renderer.close("shutdown")
        with self.assertRaises(LiveVoiceUnavailable):
            await self.renderer.render(["One moment."])
        self.assertEqual(self.endpoint.sockets, [])
        await self.assert_lifecycle_invariants()

    async def test_nothing_opens_when_the_shutdown_lands_while_a_session_closes(self):
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        self.endpoint.close_delay_s = 0.5
        self.endpoint.sockets[0].drop({"type": "session.closed"})
        await asyncio.sleep(0.05)
        task = asyncio.create_task(self.renderer.render(["Your timer is done."]))
        await asyncio.sleep(0.05)
        await self.renderer.close("shutdown")
        with self.assertRaises(LiveVoiceUnavailable):
            await task
        self.assertEqual(len(self.endpoint.sockets), 1, "no socket is bought after the shutdown")
        await self.assert_lifecycle_invariants()

    async def test_connect_time_is_not_charged_as_socket_time(self):
        async def slow_connect(uri, additional_headers=None, **kw):
            self.clock.offset += 8.0  # an 8 s TLS handshake before any socket exists
            return await self.endpoint.connect(uri, additional_headers, **kw)

        self.renderer._connect = slow_connect
        self.renderer.linger_s = 0.0
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        self.assertLess(self.ledger._history[-1].seconds, self.endpoint.sockets[0].real_seconds + 0.5)
        await self.assert_lifecycle_invariants()

    async def test_connection_closed_on_a_send(self):
        self.endpoint.mode = "closed_on_line2"
        result = await self.renderer.render(["One moment.", "Setting the timer."])
        self.assertEqual(set(result), {"One moment."})
        await self.assert_lifecycle_invariants()

    async def test_connection_closed_by_the_reader_mid_line(self):
        self.endpoint.mode = "reader_raises"
        result = await self.renderer.render(["One moment.", "Setting the timer."])
        self.assertEqual(set(result), {"One moment."})
        await self.assert_lifecycle_invariants()

    async def test_connection_closed_in_the_drain_after_a_line(self):
        for mode in ("eof_after_line", "error_after_line"):
            with self.subTest(mode):
                self.endpoint.mode = mode
                result = await self.renderer.render(["One moment.", "Setting the timer."])
                self.assertIn("One moment.", result)
                await asyncio.sleep(0.3)
                self.assertIsNone(self.renderer._session)
                self.assertEqual(self.ledger._active, {})
        await self.assert_lifecycle_invariants()

    async def test_connection_closed_during_the_linger(self):
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        await asyncio.sleep(0.2)
        self.endpoint.sockets[0].drop({"type": "session.closed"})
        await asyncio.sleep(0.4)
        self.assertEqual(self.ledger._active, {}, "the drop is settled without waiting out the linger")
        self.clock.offset += 40 * 60
        self.assertIn("Your timer is done.", await self.renderer.render(["Your timer is done."]))
        self.assertEqual(len(self.endpoint.sockets), 2)
        await self.assert_lifecycle_invariants()

    # -- the bound -----------------------------------------------------------

    async def test_a_lingering_session_past_its_bound_is_never_reused(self):
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        self.clock.offset += lvp.MAX_SESSION_S + 1
        for i in range(6):
            line = f"Your timer number {i} is done."
            self.assertIn(line, await self.renderer.render([line]), "a healthy endpoint renders the line")
            self.clock.offset += 10.0
        self.assertGreater(len(self.endpoint.sockets), 1)
        await self.assert_lifecycle_invariants()

    async def test_a_warm_up_that_uses_the_bound_then_on_demand_lines(self):
        self.endpoint.attempt_cost_s = 9.0
        warm = [f"Prompt number {i}." for i in range(20)]
        await self.renderer.render(warm)
        self.endpoint.attempt_cost_s = 0.0
        for i in range(8):
            line = f"Your timer number {i} is done."
            self.assertIn(line, await self.renderer.render([line]), "a healthy endpoint renders the line")
            self.clock.offset += 8.0
        await self.assert_lifecycle_invariants()

    async def test_the_bound_is_enforced_by_a_timer_even_while_a_send_hangs(self):
        self.endpoint.mode = "send_hangs"
        task = asyncio.create_task(self.renderer.render(["One moment."]))
        await asyncio.sleep(0.2)
        self.clock.offset += lvp.MAX_SESSION_S + 1
        done, _ = await asyncio.wait({task}, timeout=2.0)
        self.assertIn(task, done, "the render is released at the bound")
        self.assertEqual(self.ledger._active, {})
        await asyncio.gather(task, return_exceptions=True)
        await self.assert_lifecycle_invariants()

    async def test_a_grant_too_small_for_one_line_buys_no_socket(self):
        from app.live_cost import SessionCharge
        # About 30 s of budget: a grant above the ledger's minimum, but a
        # bound (grant less the close margin) shorter than one line timeout.
        self.ledger.record(SessionCharge("kitchen", "s", self.CAP_MINUTES * 60 - 30, "test", time.time()))
        self.renderer.line_timeout_s = 20.0
        with self.assertRaises(LiveVoiceUnavailable) as caught:
            await self.renderer.render(["One moment."])
        self.assertEqual(caught.exception.reason, "spend reservation refused")
        self.assertEqual(self.endpoint.sockets, [], "no socket is bought")
        await self.assert_lifecycle_invariants(device_room=False)

    async def test_a_smaller_grant_is_the_session_bound(self):
        from app.live_cost import SessionCharge
        # Leave about a minute of budget: the ledger grants less than asked.
        self.ledger.record(SessionCharge("kitchen", "s", self.CAP_MINUTES * 60 - 60, "test", time.time()))
        # A long linger, so only the bound can end the session here.
        self.renderer.linger_s = 120.0
        self.assertIn("One moment.", await self.renderer.render(["One moment."]))
        self.clock.offset += 50.0
        await asyncio.sleep(0.6)
        self.assertEqual(self.ledger._active, {}, "the session closes at the bound its grant allows")
        self.assertLessEqual(self.ledger._history[-1].seconds, 60.0)
        await self.assert_lifecycle_invariants(device_room=False)


if __name__ == "__main__":
    unittest.main()
