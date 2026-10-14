"""Chat as fast as native (fastchat stream): audit improvements 3, 5 and 6.

* 3: a cheap tool-less classifier runs beside the full route; a CHAT verdict
  opens the pre-result gate before the backend's verdict. Every other outcome
  (REQUEST, NO_REQUEST, a timeout, an error, any veto) changes nothing.
* 5: acknowledgement audio is released only up to the session-timeline end of
  the last transcript delta that formed a complete acknowledgement, so a late
  transcript cannot let the words after it through; dropped speech is named
  in the result commentary.
* 6: the voice's pre-result words are logged, redacted, with what happened
  to them, so the acknowledgement grammar can be tuned from reality.

Classes marked NEW fail on the v33 snapshot base; classes marked PIN pass on
both and pin that the fast path left v33's safety behaviour unchanged.
"""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from pipecat.frames.frames import TTSAudioRawFrame

import app.live_service as live_service_module
from app.action_gate import ActionGate, EntityDirectory

from tests import live_fixtures as fx
from tests.test_live_router import (
    STATES, FakeBackend, FakeRecognizer, Patched, RouterHarness, SlowHouse, call, message, response,
)
from app.live_router import LocalMatch

BYTES_PER_MS = 48  # 24 kHz, 16-bit mono


def b64(pcm):
    return base64.b64encode(pcm).decode("ascii")


class FakeClassifier:
    """Answers ``label`` after ``delay_s``; records what it was asked."""

    def __init__(self, label="CHAT", delay_s=0.05):
        self.label = label
        self.delay_s = delay_s
        self.asked = []

    async def classify(self, text, context=""):
        from app.live_classifier import Verdict
        self.asked.append((text, context))
        await asyncio.sleep(self.delay_s)
        return Verdict(self.label, int(self.delay_s * 1000))


async def heard_within(h, timeout):
    try:
        await h.sink.wait_for(TTSAudioRawFrame, timeout=timeout)
    except Exception:
        pass
    await asyncio.sleep(0.05)
    return h.heard()


# ---------------------------------------------------------------------------
# improvement 3: the fast chat path
# ---------------------------------------------------------------------------

class ChatOpensTheGateEarly(Patched):
    """NEW. v33 held a chat answer until the full backend call said CHAT."""

    async def test_a_chat_verdict_plays_the_answer_before_the_backend_answers(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()  # the full route never answers in this test
        async with RouterHarness(self, backend) as h:
            clf = FakeClassifier("CHAT", 0.05)
            h.service.chat_classifier = clf
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.12)
            h.voice_says("Owls can turn their heads very far.", ms=400)
            heard = await heard_within(h, 1.0)
            self.assertEqual(heard, fx.tone(400), "released before the backend's verdict, byte for byte")
            self.assertEqual([t for t, _ in clf.asked], ["Tell me a fun fact about owls."])
            self.assertEqual(h.executed, [])
            self.assertEqual(h.socket.of("session.commentary.append"), [])
            self.assertEqual(h.service.router_stats["fast_chat"], 1)
            op = h.service._op
            self.assertTrue(op.done)
            self.assertEqual(op.result.source, "classifier")
            await asyncio.sleep(0.05)
            self.assertTrue(op.task.done(), "the full route is cancelled, not left running")
            backend.gate.set()
            await asyncio.sleep(0.1)
            self.assertEqual(h.executed, [], "and can never reach a tool afterwards")

    async def test_the_chat_turn_is_kept_for_follow_ups(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.chat_classifier = FakeClassifier("CHAT", 0.02)
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.3)
            self.assertEqual(h.service.router._history[-1][0],
                             {"role": "user", "content": "Tell me a fun fact about owls."})


class SpeculativeVerdict(Patched):
    """NEW. The classifier starts on a pause in the user's transcript, so the
    verdict is usually in when the turn closes; it is used only for the exact
    words that closed."""

    async def test_a_verdict_started_before_the_close_is_used_at_the_close(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.6), \
                patch.object(live_service_module, "CHAT_SPECULATIVE_PAUSE_S", 0.1):
            async with RouterHarness(self, backend) as h:
                clf = FakeClassifier("CHAT", 0.3)
                h.service.chat_classifier = clf
                await h.user_says("Tell me a fun fact about owls.")
                await asyncio.sleep(0.3)
                h.voice_says("Owls can turn their heads very far.", ms=400)
                await asyncio.sleep(0.2)
                self.assertEqual(h.heard(), b"", "nothing before the turn closes")
                heard = await heard_within(h, 0.5)
                self.assertEqual(heard, fx.tone(400), "released at the close, not a classifier call later")
                self.assertEqual(len(clf.asked), 1, "one call, made during the pause")

    async def test_words_added_after_the_pause_are_classified_again(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.5), \
                patch.object(live_service_module, "CHAT_SPECULATIVE_PAUSE_S", 0.1):
            async with RouterHarness(self, backend) as h:
                clf = FakeClassifier("CHAT", 0.05)
                h.service.chat_classifier = clf
                h.socket.push({"type": "session.input_transcript.delta", "delta": "Tell me about",
                               "start_ms": 1000, "end_ms": 1500})
                await asyncio.sleep(0.25)
                h.socket.push({"type": "session.input_transcript.delta", "delta": " turning off the Atrium Lamp.",
                               "start_ms": 1500, "end_ms": 2500})
                await asyncio.sleep(0.9)
                self.assertEqual([t for t, _ in clf.asked], ["Tell me about"],
                                 "the speculative call saw a prefix; the closed text is request-shaped")
                self.assertEqual(h.service._op.fast_veto, "request-shaped")
                self.assertEqual(h.service.router_stats["fast_chat"], 0)


class OnlyChatActs(Patched):
    """PIN. REQUEST, NO_REQUEST, unknown and slow verdicts leave v33 behaviour."""

    async def _held_until_backend(self, label, delay_s=0.02, classifier=None):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.chat_classifier = classifier or FakeClassifier(label, delay_s)
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.12)
            h.voice_says("Owls can turn their heads very far.", ms=400)
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"", f"{label}: held exactly as v33 holds it")
            backend.gate.set()
            heard = await heard_within(h, 1.0)
            self.assertEqual(heard, fx.tone(400), f"{label}: released by the backend's CHAT")
            self.assertEqual(h.service.router_stats["fast_chat"], 0)

    async def test_request(self):
        await self._held_until_backend("REQUEST")

    async def test_no_request(self):
        await self._held_until_backend("NO_REQUEST")

    async def test_unknown(self):
        await self._held_until_backend("UNKNOWN")

    async def test_a_timeout_is_unknown(self):
        from app.live_classifier import ChatClassifier

        async def never(body):
            await asyncio.sleep(30)
        clf = ChatClassifier("k", post=never, timeout_s=0.1)
        await self._held_until_backend("timeout", classifier=clf)

    async def test_an_error_is_unknown(self):
        from app.live_classifier import ChatClassifier

        async def boom(body):
            raise RuntimeError("HTTP 500")
        await self._held_until_backend("error", classifier=ChatClassifier("k", post=boom))


class AMisreadRequestNeverOpensTheGate(Patched):
    """PIN. The classifier says CHAT for something that is not chat; a
    deterministic veto keeps the v33 hold, and a guessed outcome stays unheard."""

    async def _guess_stays_unheard(self, h, utterance, guess="Done, it's on."):
        await h.user_says(utterance)
        await asyncio.sleep(0.12)
        h.voice_says(guess, ms=400)
        await asyncio.sleep(0.4)
        self.assertEqual(h.heard(), b"", f"{utterance!r}: the guess is held")
        self.assertEqual(h.service.router_stats["fast_chat"], 0)

    async def test_request_shaped_wording_is_never_sent_to_the_classifier(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
                              response(message("The Atrium Lamp is on.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            clf = FakeClassifier("CHAT", 0.01)
            h.service.chat_classifier = clf
            await self._guess_stays_unheard(h, "Turn on the Atrium Lamp.")
            self.assertEqual(clf.asked, [], "no call: the wording already rules chat out")
            self.assertEqual(h.service._op.fast_veto, "request-shaped")
            backend.gate.set()
            await h.wait_commentary()
            await asyncio.sleep(0.1)
            self.assertEqual(h.heard(), b"", "dropped once the verified result is coming")
            self.assertEqual(len(h.executed), 1)

    async def test_current_or_personal_information_is_never_chat(self):
        for text in ("What's the weather tomorrow?", "Anything on my calendar today?",
                     "What time", "Any news?", "Yes.", "Do it.", "The other one."):
            veto = live_service_module.chat_fast_path_text_veto(text, ["llm__GetDateTime"])
            self.assertIsNotNone(veto, text)
        for text in ("Tell me a joke.", "Why is the sky blue?", "Hello there!"):
            self.assertIsNone(live_service_module.chat_fast_path_text_veto(text, ["llm__GetDateTime"]), text)

    async def test_a_chat_verdict_after_a_tool_started_is_ignored(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
                              response(message("The Atrium Lamp is on.")))
        async with SlowHouse(self, backend) as h:
            h.service.chat_classifier = FakeClassifier("CHAT", 0.3)
            await self._guess_stays_unheard(h, "Could you sort that out for me?")
            self.assertEqual(h.service._op.fast_veto, "a tool already ran")
            h.release.set()
            await h.wait_commentary()

    async def test_held_speech_claiming_an_outcome_vetoes(self):
        backend = FakeBackend(response(message("Which lamp do you mean?")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.chat_classifier = FakeClassifier("CHAT", 0.3)
            await self._guess_stays_unheard(h, "Could you sort that out for me?",
                                            guess="Done, I switched it.")
            self.assertEqual(h.service._op.fast_veto, "held speech claims an outcome")
            backend.gate.set()
            await h.wait_commentary()

    async def test_a_claim_spoken_before_the_transcript_closed_vetoes(self):
        backend = FakeBackend(response(message("Which one do you mean?")))
        backend.gate.clear()
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.4):
            async with RouterHarness(self, backend) as h:
                h.service.chat_classifier = FakeClassifier("CHAT", 0.05)
                await h.user_says("Could you sort that out for me?")
                await asyncio.sleep(0.1)
                h.voice_says("Done, I switched it.", ms=300)  # before the close
                await asyncio.sleep(0.7)
                self.assertEqual(h.heard(), b"")
                self.assertEqual(h.service._op.fast_veto, "held speech claims an outcome")
                backend.gate.set()
                await h.wait_commentary()

    async def test_the_voice_delegating_vetoes(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.chat_classifier = FakeClassifier("CHAT", 0.3)
            await h.user_says("Could you sort that out for me?")
            await asyncio.sleep(0.1)
            h.socket.push({"type": "session.delegation.created",
                           "delegation": {"id": "item_d1", "target": "client"}})
            await asyncio.sleep(0.4)
            self.assertEqual(h.service._op.fast_veto, "the voice delegated")
            backend.gate.set()
            await h.wait_commentary()

    async def test_an_answer_to_the_assistants_question_vetoes(self):
        backend = FakeBackend(response(message("Which lamp do you mean?")),
                              response(message("CHAT")))
        async with RouterHarness(self, backend) as h:
            await h.user_says("Could you sort something out for me?")
            await h.wait_commentary()
            clf = FakeClassifier("CHAT", 0.01)
            h.service.chat_classifier = clf
            await h.user_says("I suppose the big one by the window", start_ms=5000)
            await asyncio.sleep(0.3)
            self.assertEqual(clf.asked, [])
            self.assertEqual(h.service._op.fast_veto, "answer to the assistant's question")

    async def test_a_pending_spoken_yes_vetoes(self):
        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = FakeBackend(response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
                              response(message("Do you want me to unlock the Entry Lock?")))
        async with RouterHarness(self, backend, gate=gate) as h:
            await h.user_says("Unlock the Entry Lock.")
            await h.wait_commentary()
            self.assertEqual(h.service._chat_context_veto(), "answer to the assistant's question")
            h.service._last_route_kind = None  # only the action gate's own state is left
            self.assertEqual(h.service._chat_context_veto(), "confirmation pending")

    async def test_a_reply_awaited_by_a_confirmation_is_never_classified(self):
        # merge (v35): the twelfth freeze routes any reply to a pending
        # confirmation (_confirmation_awaiting); the fast path honours the same
        # flag at call time, before _router_start clears it.
        backend = FakeBackend(response(message("Okay.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            clf = FakeClassifier("CHAT", 0.01)
            h.service.chat_classifier = clf
            h.service._confirmation_awaiting = True
            await h.user_says("I suppose that would be fine then")
            await asyncio.sleep(0.3)
            self.assertEqual(clf.asked, [])
            self.assertEqual(h.service._op.fast_veto, "confirmation pending")
            self.assertFalse(h.service._confirmation_awaiting, "cleared once routed")
            backend.gate.set()
            await h.wait_commentary()


class ClassifierCostAndSessionBounds(Patched):
    """NEW (cost rule). The classifier is a Responses call: it never keeps a
    billed Live session open, never counts as pending work, and is skipped
    when the spend cutoff is reached."""

    async def test_a_hung_classifier_holds_nothing_open(self):
        from app.live_classifier import ChatClassifier
        hung = asyncio.Event()

        async def never(body):
            await hung.wait()
        backend = FakeBackend(response(message("CHAT")))
        async with RouterHarness(self, backend) as h:
            activity = []
            h.service.on_backend_activity = lambda: activity.append(1)
            h.service.chat_classifier = ChatClassifier("k", post=never, timeout_s=60.0)
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.4)
            op = h.service._op
            self.assertTrue(op.done, "the backend's verdict arrived")
            self.assertFalse(h.service.router_pending, "a classifier in flight is not pending work")
            self.assertFalse(h.service.backend_work_active)
            await asyncio.sleep(0.05)
            self.assertTrue(op.classifier_task.done(), "and the moot classifier call is cancelled")
            self.assertEqual(len(activity), 2, "only the route itself noted backend activity")

    async def test_a_session_with_the_classifier_still_closes_at_its_idle_bound(self):
        from app.live_classifier import ChatClassifier
        hung = asyncio.Event()

        async def never(body):
            await hung.wait()
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        with patch.object(live_service_module, "ROUTER_SILENT_CLOSE_S", 0.3):
            async with RouterHarness(self, backend) as h:
                silent = []
                h.service.on_silent_response = lambda: silent.append(1)
                h.service.chat_classifier = FakeClassifier("CHAT", 0.02)
                await h.user_says("Tell me a fun fact about owls.")
                # The voice says nothing after a fast CHAT: the turn still ends
                # at the same silent-close bound as a backend CHAT would.
                await asyncio.sleep(0.8)
                self.assertEqual(silent, [1])
                self.assertFalse(h.service.router_pending)
                del never

    async def test_the_spend_cutoff_skips_the_call(self):
        backend = FakeBackend(response(message("CHAT")))
        async with RouterHarness(self, backend) as h:
            clf = FakeClassifier("CHAT", 0.01)
            h.service.chat_classifier = clf
            h.service.router_allowed = lambda: False
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.3)
            self.assertEqual(clf.asked, [])
            self.assertEqual(h.service._op.fast_veto, "spend cutoff")

    def test_cost_estimate_is_bounded(self):
        from app.live_classifier import (CLASSIFIER_INSTRUCTIONS, CLASSIFIER_MAX_OUTPUT_TOKENS,
                                         ChatClassifier, classifier_usage_usd)
        body = ChatClassifier("k").body("x" * 5000, "y" * 5000)
        self.assertLessEqual(len(json.dumps(body)), len(CLASSIFIER_INSTRUCTIONS) + 1200,
                             "user text and context are truncated: the prompt is bounded")
        self.assertEqual(body["max_output_tokens"], CLASSIFIER_MAX_OUTPUT_TOKENS)
        self.assertNotIn("tools", body)
        # ~330 input + 2 output tokens on the measured model: well under $0.0005 per turn.
        self.assertLess(classifier_usage_usd(body["model"], {"input_tokens": 400, "output_tokens": 16}), 0.0005)


class ClassifierParsing(unittest.TestCase):
    """NEW. Strict parsing: only a single known word is a verdict."""

    def test_parse(self):
        from app.live_classifier import parse_label
        self.assertEqual(parse_label("CHAT"), "CHAT")
        self.assertEqual(parse_label(" chat.\n"), "CHAT")
        self.assertEqual(parse_label("NO_REQUEST"), "NO_REQUEST")
        self.assertEqual(parse_label("no request"), "NO_REQUEST")
        for text in ("", "CHAT or REQUEST", "Sure! CHAT", "CHATTY", "I think CHAT"):
            self.assertEqual(parse_label(text), "UNKNOWN", text)


# ---------------------------------------------------------------------------
# improvement 5: the gate follows the session timeline
# ---------------------------------------------------------------------------

class TimelineGate(Patched):
    """NEW. A late transcript can delay acknowledgement audio, never leak the
    words after it."""

    def say(self, h, text, end_ms):
        h.socket.push({"type": "session.output_transcript.delta", "delta": text,
                       "start_ms": max(0, end_ms - 300), "end_ms": end_ms})

    def audio(self, h, ms):
        h.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(ms))})

    async def test_words_after_an_acknowledgement_wait_for_their_transcript(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
                              response(message("The Atrium Lamp is on.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp.")
            await asyncio.sleep(0.12)
            base = h.service._session_output_audio_bytes // BYTES_PER_MS
            self.say(h, "Sure, one sec.", base + 500)
            self.audio(h, 500)
            await asyncio.sleep(0.3)
            # The next words' audio arrives; their transcript is 1 s late.
            self.audio(h, 400)
            await asyncio.sleep(1.0)
            self.assertEqual(len(h.heard()), 500 * BYTES_PER_MS,
                             "the acknowledgement plays, the 400 ms after it does not")
            self.say(h, " It's on.", base + 900)
            await asyncio.sleep(0.2)
            self.assertEqual(len(h.heard()), 500 * BYTES_PER_MS, "and it is a guess: held")
            backend.gate.set()
            sent = await h.wait_commentary()
            await asyncio.sleep(0.2)
            self.assertEqual(len(h.heard()), 500 * BYTES_PER_MS, "dropped, never heard")
            self.assertIn("did not hear", sent[0]["content"],
                          "the voice is told its dropped words were not heard")

    async def test_unheard_lag_at_the_result_is_dropped_not_played_as_ack(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
                              response(message("The Atrium Lamp is on.")))
        backend.gate.clear()
        with patch.object(live_service_module, "ACK_LAG_S", 1.0):
            async with RouterHarness(self, backend) as h:
                await h.user_says("Turn on the Atrium Lamp.")
                await asyncio.sleep(0.12)
                base = h.service._session_output_audio_bytes // BYTES_PER_MS
                self.say(h, "Sure, one sec.", base + 300)
                self.audio(h, 300)
                self.audio(h, 400)  # words with no transcript yet
                await asyncio.sleep(0.1)
                backend.gate.set()  # the result lands while all of it is in the lag
                await h.wait_commentary()
                await asyncio.sleep(0.3)
                self.assertEqual(len(h.heard()), 300 * BYTES_PER_MS,
                                 "only the timestamped acknowledgement plays")

    async def test_an_acknowledgement_after_the_first_plays_without_waiting(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the shopping list?")
            await asyncio.sleep(0.12)
            base = h.service._session_output_audio_bytes // BYTES_PER_MS
            self.say(h, "Sure,", base + 200)
            self.say(h, " one sec.", base + 500)
            self.audio(h, 500)
            await asyncio.sleep(0.4)
            self.assertEqual(len(h.heard()), 500 * BYTES_PER_MS)
            backend.gate.set()
            await h.wait_commentary()


# ---------------------------------------------------------------------------
# improvement 6: what the voice said before the verdict, redacted
# ---------------------------------------------------------------------------

class HeldSpeechSamples(Patched):
    """NEW. The held text is logged, without household names, with its fate."""

    def test_redaction(self):
        from app.live_router import redact_for_log, set_known_names
        set_known_names(["Robin"])
        try:
            self.assertEqual(redact_for_log("Let me take a look at that for you, Robin."),
                             "Let me take a look at that for you, <name>.")
            self.assertEqual(redact_for_log("Sure. The Atrium Lamp is at 40 percent."),
                             "Sure. The <Name> <Name> is at <n> percent.")
            self.assertEqual(redact_for_log("I'll check Robin's calendar."), "I'll check <name>'s calendar.")
            self.assertTrue(redact_for_log("one " * 40).endswith("(+26 words)"))
        finally:
            set_known_names([])

    async def test_the_dropped_sample_is_logged_redacted(self):
        from app.live_router import set_known_names
        set_known_names(["Robin"])
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
                              response(message("The Atrium Lamp is on.")))
        backend.gate.clear()
        try:
            async with RouterHarness(self, backend) as h:
                await h.user_says("Turn on the Atrium Lamp.")
                await asyncio.sleep(0.12)
                h.voice_says("Let me take a look at that, Robin. Atrium Lamp is on.", ms=300)
                await asyncio.sleep(0.2)
                with self.assertLogs("app.live_service", level="INFO") as logs:
                    backend.gate.set()
                    await h.wait_commentary()
                lines = [line for line in logs.output if "pre-result speech" in line]
                self.assertEqual(len(lines), 1, logs.output)
                self.assertIn('"outcome":"dropped"', lines[0])
                self.assertIn("Let me take a look at that, <name>. <Name> <Name> is on.", lines[0])
                self.assertNotIn("Robin", lines[0])
                self.assertNotIn("Atrium Lamp", lines[0])
        finally:
            set_known_names([])



# ---------------------------------------------------------------------------
# v35 merge: fastchat review follow-ups
# ---------------------------------------------------------------------------

class SlowRecognizer(FakeRecognizer):
    def __init__(self, match=None, delay_s=2.0):
        super().__init__(match)
        self.delay_s = delay_s

    async def recognize(self, text, *args, **kwargs):
        self.sentences.append(text)
        await asyncio.sleep(self.delay_s)
        return self.match


class IntentEngineVetoesChat(Patched):
    """The home's intent engine runs first inside route(); a match means a
    command, so a CHAT verdict is never used; still undecided means no fast path."""

    async def _case(self, recognizer):
        backend = FakeBackend(response(message("Which one do you mean?")))
        backend.gate.clear()
        async with RouterHarness(self, backend, recognizer=recognizer) as h:
            h.service.chat_classifier = FakeClassifier("CHAT", 0.01)
            await h.user_says("Could you sort that out for me?")
            await asyncio.sleep(0.12)
            h.voice_says("Done, it's sorted.", ms=300)
            await asyncio.sleep(0.9)
            self.assertEqual(h.heard(), b"", "the guess stays held")
            self.assertEqual(h.service.router_stats["fast_chat"], 0)
            veto = h.service._op.fast_veto
            backend.gate.set()
            await h.wait_commentary()
            return veto

    async def test_a_matched_intent_vetoes(self):
        match = LocalMatch("HassTurnOn", {"name": "Entry Lock"}, ["lock"], 1)
        self.assertEqual(await self._case(FakeRecognizer(match)), "the home's intent engine matched")

    async def test_an_undecided_intent_engine_vetoes(self):
        self.assertEqual(await self._case(SlowRecognizer(None, 2.0)), "intent engine undecided")

    async def test_no_match_lets_chat_through(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend, recognizer=FakeRecognizer(None)) as h:
            h.service.chat_classifier = FakeClassifier("CHAT", 0.01)
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.4)
            self.assertEqual(h.service.router_stats["fast_chat"], 1)
            backend.gate.set()


class MoreDeviceWordsVeto(unittest.TestCase):
    def test_security_scenes_modes_climate_and_repeats(self):
        for text in ("Arm the security system please", "Disarm it for me now", "Close the gate for me",
                     "Start the movie scene now", "Switch to night mode now", "Run the bedtime routine",
                     "Put it on for me", "Put that off for now", "Can you put the AC up",
                     "Make the heater warmer please", "Do the same for me", "Do that again please"):
            self.assertIsNotNone(live_service_module.chat_fast_path_text_veto(text, []), text)


class ClassifierChargesOnTimeoutAndCancel(unittest.IsolatedAsyncioTestCase):
    async def test_flat_estimate(self):
        from app.live_classifier import FLAT_ESTIMATE_USAGE, ChatClassifier
        charged = []

        async def hang(body):
            await asyncio.sleep(5)
        clf = ChatClassifier("k", post=hang, timeout_s=0.05, on_usage=charged.append)
        verdict = await clf.classify("Tell me a joke.")
        self.assertEqual(verdict.reason, "timeout")
        self.assertEqual(charged, [FLAT_ESTIMATE_USAGE])
        clf.timeout_s = 5.0
        task = asyncio.ensure_future(clf.classify("Tell me a joke."))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(charged, [FLAT_ESTIMATE_USAGE, FLAT_ESTIMATE_USAGE])


class FastChatOption(unittest.TestCase):
    def test_empty_or_off_disables_and_unset_is_the_default(self):
        from app.live_classifier import CLASSIFIER_MODEL
        from app.voice_runtime import _fast_chat_model
        self.assertEqual(_fast_chat_model(""), CLASSIFIER_MODEL)
        self.assertEqual(_fast_chat_model("off"), "")
        import pathlib
        run_sh = (pathlib.Path(__file__).resolve().parents[1] / "root" / "run.sh").read_text()
        self.assertIn('export LIVE_FAST_CHAT_MODEL="off"', run_sh, "an empty option means off")


if __name__ == "__main__":
    unittest.main()
