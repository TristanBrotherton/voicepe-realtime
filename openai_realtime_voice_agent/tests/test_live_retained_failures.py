"""Regressions for the two retained GPT-Live household failures.

Both are reproduced from the retained turn evidence, not from a guess at what
might go wrong. No provider, no device, no Home Assistant.

**A — a clear household/tool request ends with no required tool and no usable
final reply.** Retained Workshop turn ``c0ffee02-17`` ("Search the web for
today's weather in Lisbon"): one successful ``web_search`` in 3.580 s, 307
output-audio deltas / 1,473,600 bytes produced, nothing admitted to device
playout, ~16 s with no model activity, then the phase emitter's generic
thinking watchdog forced idle with ``outcome: "error"``, ``complete: null``,
no final reply and no second tool call. Retained Kitchen turn ``c0ffee01-18``
("add pancake mix to the shopping list"): heard clearly, no ``todo.add_item``
service call anywhere in Recorder, no item on any list, terminal
``admission_silence`` / ``silent``.

Three independent causes, each pinned below:

1. continuation ownership was decided by how the *provider* chose to label the
   continuation's ``response.created``, while the client registered ownership
   only when it happened to know a delegation id. A delegated continuation
   that came back untagged was rejected outright, so
   ``pending_continuations`` never decremented and ``ready`` could never
   become True again;
2. ``web_search`` could not be classified, so a ``query`` requirement raised
   from the user's own words was unsatisfiable by construction;
3. an unclaimed create vetoed every later create for the life of the session,
   disabling both the silent-tool rescue and every client continuation; and
   nothing in this runtime bounded a held reply, because the only producer of
   the quiet boundary admission needs is a further provider delta.

**B — a successful tool and queued reply audio still produce an
underrun/unconfirmed drain and nothing audible.** Retained Kitchen turn
``c0ffee01-20``: ``todo__HassListAddItem`` ran exactly once with
``item: blueberries``, Home Assistant returned success and ``Blueberries`` was
verified present, the relay logged 2.40 s of reply audio admitted and a device
``audible`` timestamp — and the same turn recorded one underrun and later
``Live reply drain not confirmed``. Nothing was heard.

The admission and the service could disagree about whether a delivered
segment still existed: ``invalidate()`` inside ``feed(busy=True)`` dropped the
enqueued set while ``_drain_batches`` kept supervising the batch, so no
firmware report could match and the drain watchdog was *guaranteed* to call an
admitted, physically played reply unconfirmed. ``cancel_active_response()``
left the same supervision armed over an admission it had just reset.

A revoked batch still must never confirm a drain, arm confirmation or open a
follow-up window — that reviewed rule is asserted here too, so repairing the
diagnosis cannot quietly turn into accepting one.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)

import app.live_service as live_service_module
from app.live_audio import OutputChunk
from app.live_evidence import TurnEvidence, classify_tool_name
from app.live_service import should_rescue_silent_tool_request
from app.phase_emitter import PhaseEmitter
from tests import live_fixtures as fx
from tests.live_harness import LiveHarness

# The retained Workshop utterance. The wording is the retained one; only
# the town is a public placeholder, because this repository is public.
WORKSHOP_REQUEST = "Search the web for today's weather in Lisbon."
# The retained Kitchen utterance that dispatched no tool at all.
KITCHEN_REQUEST = "Add pancake mix to the shopping list."

SEARCH_CALL = "call_web_search_retained"
SECOND_DELEGATION = "delegation_fixture_2"


def envelope(inner, delegation_id, event_id="event_retained"):
    return {"type": "response.event", "event_id": event_id,
            "delegation_id": delegation_id, "event": inner}


def created(delegation_id, response_id):
    return envelope({"type": "response.created", "sequence_number": 0,
                     "response": {"id": response_id, "status": "in_progress",
                                  "output": []}}, delegation_id)


def completed(delegation_id, response_id, text=""):
    content = [{"type": "output_text", "text": text}] if text else []
    return envelope({"type": "response.completed", "sequence_number": 9,
                     "response": {"id": response_id, "status": "completed",
                                  "output": [{"content": content}] if content else [],
                                  "error": None, "incomplete_details": None}},
                    delegation_id)


def call_done(delegation_id, response_id, call_id, name, arguments="{}"):
    return envelope({"type": "response.output_item.done", "output_index": 0,
                     "sequence_number": 4, "response_id": response_id,
                     "item": {"id": "fc_retained", "type": "function_call",
                              "status": "completed", "arguments": arguments,
                              "call_id": call_id, "name": name}}, delegation_id)


def tool_turn(name="web_search", requirement="query", delegation="d1", response="r1"):
    """A delegated tool turn, run up to the point the continuation is due."""
    evidence = TurnEvidence()
    evidence.requirement = requirement
    evidence.delegate(delegation)
    evidence.accept(created(delegation, response))
    evidence.accept(call_done(delegation, response, "c1", name))
    evidence.accept(completed(delegation, response))
    evidence.result("c1")
    evidence.continuation()
    return evidence


# Exactly the fields firmware 1.3.1 sends, from the retained real device wire
# captures: `reason`, never `outcome` or `drain_timeout`.
def firmware_metrics(batch, seq=0, reason="reply", drained=4734):
    report = {"type": "turn_metrics", "turn": "wake_retained", "seq": seq,
              "reason": reason, "fire_to_mic_ms": 2, "wake_to_listening_ms": 1853,
              "listening_to_thinking_ms": 1055, "thinking_to_first_audio_ms": 5852,
              "first_audio_to_audible_ms": 218, "total_ms": 13712,
              "ws_gaps": 0, "ws_gap_max_ms": 0, "underrun": 1,
              "dry_chain_events": 41}
    if drained is not None:
        report["audible_to_drained_ms"] = drained
    if batch is not None:
        report["output_batch_id"] = batch
    return report


class ContinuationOwnership(unittest.TestCase):
    """Failure A, cause 1: the provider's labelling decided the outcome."""

    def test_untagged_continuation_resolves_a_delegated_tool_turn(self):
        evidence = tool_turn()
        self.assertTrue(evidence.request_client_response("ev1", "d1"))
        # The endpoint answers the continuation WITHOUT the delegation tag.
        self.assertTrue(evidence.accept(created("", "r2")))
        self.assertEqual(evidence.pending_continuations, 0,
                         "an untagged continuation must still close the wait")
        self.assertTrue(evidence.accept(completed("", "r2", "It is 68 and sunny.")))
        self.assertTrue(evidence.ready,
                        "retained Workshop c0ffee02-17: a successful web_search "
                        "left ready False forever, so no reply could be admitted")
        self.assertEqual(evidence.kind, "tool_result")

    def test_tagged_continuation_still_resolves_a_delegated_tool_turn(self):
        evidence = tool_turn()
        self.assertTrue(evidence.request_client_response("ev1", "d1"))
        self.assertTrue(evidence.accept(created("d1", "r2")))
        self.assertEqual(evidence.pending_continuations, 0)
        self.assertTrue(evidence.accept(completed("d1", "r2", "It is 68 and sunny.")))
        self.assertTrue(evidence.ready)

    def test_concurrent_delegations_each_get_their_own_create(self):
        evidence = tool_turn()
        evidence.delegate(SECOND_DELEGATION)
        evidence.accept(created(SECOND_DELEGATION, "r9"))
        evidence.accept(call_done(SECOND_DELEGATION, "r9", "c2", "todo__get_items"))
        evidence.accept(completed(SECOND_DELEGATION, "r9"))
        evidence.result("c2")
        evidence.continuation()
        self.assertTrue(evidence.request_client_response("ev1", "d1"))
        self.assertTrue(evidence.request_client_response("ev2", SECOND_DELEGATION),
                        "two distinct delegations are not ambiguous")
        self.assertFalse(evidence.request_client_response("ev3", "d1"),
                         "a second create for the SAME delegation is refused")
        self.assertFalse(evidence.request_client_response("ev4", None),
                         "an untagged create is ambiguous against both")

    def test_an_untagged_response_nobody_asked_for_is_refused(self):
        evidence = tool_turn()
        self.assertFalse(evidence.accept(created("", "r2")),
                         "ownership must still fail closed without a create")
        self.assertFalse(evidence.ready)


class SessionLatch(unittest.TestCase):
    """Failure A, cause 3: one unclaimed create silenced the whole session."""

    def test_an_unclaimed_create_does_not_veto_the_session(self):
        evidence = TurnEvidence()
        self.assertTrue(evidence.request_client_response("old"))
        evidence.next_turn()
        self.assertFalse(evidence.request_client_response("blocked"),
                         "the turn it superseded can still be answered")
        self.assertTrue(evidence.has_outstanding_request())
        evidence.next_turn()
        self.assertTrue(evidence.request_client_response("allowed"),
                        "retained Kitchen c0ffee01-18: this stayed refused for "
                        "the rest of the session, so no tool was ever dispatched")

    def test_an_aged_out_create_is_not_claimed_by_the_current_turn(self):
        evidence = TurnEvidence()
        evidence.request_client_response("old")
        evidence.next_turn()
        evidence.next_turn()
        self.assertTrue(evidence.request_client_response("current"))
        # The long-late create for the retired turn must consume its OWN owner
        # and be rejected, never be read as the current turn's answer.
        self.assertFalse(evidence.accept(created("", "r_late")))
        self.assertTrue(evidence.has_outstanding_request(),
                        "the current turn's create is still outstanding")
        self.assertTrue(evidence.accept(created("", "r_current")))


class ToolClassification(unittest.TestCase):
    """Failure A, cause 2 — and a safety hole found while fixing it."""

    def test_web_search_can_satisfy_the_query_requirement_it_raises(self):
        self.assertTrue(should_rescue_silent_tool_request(WORKSHOP_REQUEST))
        self.assertEqual(classify_tool_name("web_search"), "query")
        evidence = tool_turn(name="web_search", requirement="query")
        self.assertTrue(evidence.requirement_met(),
                        "retained Workshop c0ffee02-17: a successful web_search "
                        "could never satisfy the requirement its own transcript set")

    def test_ask_openclaw_cannot_satisfy_an_action_requirement(self):
        # "ask_openclaw" contains the substring "open", and the previous
        # substring match therefore reported an external-agent LOOKUP as proof
        # that the house had changed - so "open the garage" could have been
        # answered by a result that opened nothing.
        self.assertEqual(classify_tool_name("ask_openclaw"), "query")
        evidence = tool_turn(name="ask_openclaw", requirement="action")
        self.assertFalse(evidence.requirement_met())
        self.assertEqual(evidence.result_kinds, {"query"},
                         "an external-agent lookup is a query and nothing "
                         "else; an 'action' here is the substring match back")

    def test_a_query_requirement_needs_some_tool_result(self):
        evidence = TurnEvidence()
        evidence.requirement = "query"
        self.assertFalse(evidence.requirement_met(),
                         "prose alone never satisfies a lookup")

    def test_action_and_clock_requirements_stay_strict(self):
        self.assertFalse(tool_turn(name="web_search", requirement="action").requirement_met())
        self.assertFalse(tool_turn(name="web_search", requirement="clock").requirement_met())
        self.assertFalse(tool_turn(name="todo__get_items", requirement="action").requirement_met())
        self.assertTrue(tool_turn(name="todo__HassListAddItem",
                                  requirement="action").requirement_met())
        self.assertTrue(tool_turn(name="llm__GetDateTime", requirement="clock").requirement_met())

    def test_a_search_that_also_changes_the_house_classifies_as_an_action(self):
        self.assertEqual(
            classify_tool_name("media_player__HassMediaSearchAndPlay"), "action")


class RetainedWorkshopTurn(unittest.IsolatedAsyncioTestCase):
    """Failure A, end to end: the retained web_search turn must answer."""

    async def _run_tool_turn(self, h, tag_continuation):
        s = h.service
        s.spoken_prompts = None
        s.turn_timeline.begin("wake_retained")
        executed = []

        async def handler(params):
            executed.append(dict(params.arguments or {}))
            await params.result_callback({"results": "68F and sunny in Lisbon"})

        s.register_function("web_search", handler)
        # The user's own words, as the runtime sees them.
        for event in fx.transcript_in(WORKSHOP_REQUEST):
            await s._on_transcript_delta("user", event)
        self.assertTrue(s._requires_backend_for_reply)
        self.assertEqual(s._evidence.requirement, "query")

        h.socket.push(fx.delegation_created("d1"))
        h.socket.push(created("d1", "r1"))
        h.socket.push(call_done("d1", "r1", SEARCH_CALL, "web_search",
                                '{"query":"weather Lisbon today"}'))
        h.socket.push(completed("d1", "r1"))
        await h.socket.wait_for_sent("response.create")
        self.assertEqual(len(executed), 1, "web_search runs exactly once")

        # The continuation comes back labelled the way the endpoint chose.
        tag = "d1" if tag_continuation else ""
        h.socket.push(created(tag, "r2"))
        h.socket.push(completed(tag, "r2", "It's 68 and sunny in Lisbon."))
        await asyncio.sleep(.03)

        h.socket.push({"type": "session.output_transcript.delta",
                       "delta": "It's 68 and sunny in Lisbon.",
                       "start_ms": 100, "end_ms": 400})
        h.socket.push_all(fx.output_audio_deltas(fx.tone(400) + fx.silence(900)))
        await h.sink.wait_for(LLMFullResponseEndFrame, timeout=3)
        self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)],
                         ["It's 68 and sunny in Lisbon."])
        self.assertTrue(h.sink.of(TTSAudioRawFrame), "the reply must reach playout")
        self.assertEqual(h.socket.types().count("response.create"), 1,
                         "the tool is continued exactly once, never replayed")
        self.assertEqual(len(executed), 1, "no consequential tool is replayed")

    async def test_untagged_continuation_still_produces_an_audible_reply(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            await self._run_tool_turn(h, tag_continuation=False)

    async def test_tagged_continuation_still_produces_an_audible_reply(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            await self._run_tool_turn(h, tag_continuation=True)


class ReplyResolutionBound(unittest.TestCase):
    def test_the_real_bound_sits_inside_the_thinking_watchdog(self):
        # The runtime that knows WHY a reply is stuck has to be the one that
        # closes the turn; otherwise the phase emitter forces idle first and
        # the turn is recorded as a bare `error`, as c0ffee02-17 was.
        self.assertLess(live_service_module.REPLY_RESOLUTION_S,
                        PhaseEmitter.THINKING_TIMEOUT_S)
        self.assertGreater(live_service_module.REPLY_RESOLUTION_S, 1.0)

    def test_the_silent_close_sits_inside_the_thinking_watchdog(self):
        """candidate-v25's rejection: 14 s silent window + 3 s grace = 17 s ran
        past the 15 s watchdog, so a genuinely silent turn was force-idled as
        a bare `error` (the c0ffee02-17 shape) and the device opened its
        follow-up mic window after an accidental wake. The service that knows
        the turn is silent must close it first, as `silent`, with no follow-up.
        Fails on v25's bytes (17 vs 15)."""
        self.assertLess(live_service_module.SILENT_RESPONSE_S + PhaseEmitter.SILENT_GRACE_S,
                        PhaseEmitter.THINKING_TIMEOUT_S)


class StalledOutputStream(unittest.IsolatedAsyncioTestCase):
    """Failure A: a held reply had nothing bounding it.

    GPT-Live has no output-audio-done event, so the quiet boundary complete
    admission needs can only be produced by a further provider delta. When the
    stream stopped, the assembled reply sat there: the delegation path cancels
    the silent-turn watchdog and nothing re-arms it, and the alignment
    watchdog arms only when a timestamped transcript leads its PCM.
    """

    def setUp(self):
        # The real bound is 6 s, chosen to sit inside PhaseEmitter's thinking
        # timeout. Shortened here so the stall is reached without
        # sleeping through it; nothing else about the path changes.
        patcher = patch.object(live_service_module, "REPLY_RESOLUTION_S", .05)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _held(self, h, text="It's 68 and sunny in Lisbon.", transcript=True,
                    before_audio=None):
        await h.start_session()
        s = h.service
        s.turn_timeline.begin("wake_retained")
        s._user_transcript_seen_for_response = True
        if transcript:
            h.socket.push({"type": "session.output_transcript.delta", "delta": text,
                           "start_ms": 100, "end_ms": 400})
            await asyncio.sleep(.01)
        if before_audio is not None:
            before_audio(s)
        h.socket.push_all(fx.output_audio_deltas(fx.tone(400)))
        await asyncio.sleep(.01)
        self.assertTrue(s._reply_candidate_held(),
                        "the fixture must leave a candidate held")
        self.assertIsNotNone(s._reply_resolution_task,
                             "a held reply must be supervised")
        return s

    async def test_a_resolvable_held_reply_is_admitted_when_output_stops(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            s = await self._held(h)
            # No further provider delta will ever arrive.
            await h.sink.wait_for(LLMFullResponseEndFrame, timeout=3)
            self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)],
                             ["It's 68 and sunny in Lisbon."])
            self.assertTrue(h.sink.of(TTSAudioRawFrame))
            self.assertFalse(s.reply_admission.terminal)

    async def test_an_untranscribed_held_segment_is_never_admitted(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            s = await self._held(h, transcript=False)
            await asyncio.sleep(.3)
            self.assertEqual(h.sink.of(LLMFullResponseEndFrame), [],
                             "untranscribed audio must never reach the speaker")
            self.assertEqual(h.sink.of(TTSTextFrame), [])
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])

    async def test_an_unsatisfied_requirement_fails_closed_as_silent(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            silent = []

            def unsatisfiable(s):
                # A clock question answered only by a web lookup: the model may
                # not answer it from prose, at any timeout.
                s._requires_backend_for_reply = True
                s._evidence.requirement = "clock"
                s._evidence.calls["c1"] = s._evidence.generation
                s._evidence.call_names["c1"] = "web_search"
                s._evidence.result("c1")
                s.on_silent_response = lambda: silent.append(True)

            s = await self._held(h, before_audio=unsatisfiable)
            await asyncio.wait_for(self._until(lambda: silent), 3)
            self.assertEqual(h.sink.of(LLMFullResponseEndFrame), [])
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            self.assertTrue(s.reply_admission.terminal,
                            "the admission must fail closed, not keep waiting")
            self.assertIn("requirement clock", s.reply_admission.failure_reason)

    async def test_backend_work_in_flight_defers_resolution(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            s = await self._held(
                h, before_audio=lambda svc: svc._delegations.__setitem__(
                    "still-running", asyncio.get_running_loop().create_future()))
            await asyncio.sleep(.3)
            self.assertEqual(h.sink.of(LLMFullResponseEndFrame), [],
                             "a tool still owes the user an answer; do not resolve")
            self.assertFalse(s.reply_admission.terminal)
            s._delegations.clear()

    @staticmethod
    async def _until(predicate):
        while not predicate():
            await asyncio.sleep(.005)


class RetainedKitchenDrain(unittest.IsolatedAsyncioTestCase):
    """Failure B: an admitted, played reply reported as an unconfirmed drain."""

    async def _delivered_reply(self, h, text="Added blueberries to your shopping list."):
        s = h.service
        s.turn_timeline.begin("wake_retained")
        s._user_transcript_seen_for_response = True
        admission = s.reply_admission
        admission.transcript(text, end_ms=400)
        admission.feed(OutputChunk(pcm=fx.tone(2400), audible=True), False, end_ms=400)
        reply = admission.feed(
            OutputChunk(reason="idle run exceeded max_silence_ms"), False, end_ms=400)
        self.assertIsNotNone(reply)
        await s._deliver_reply(reply)
        batch = next(iter(s._drain_batches))
        self.assertTrue(s._drain_batches[batch]["enqueued"])
        return s, batch

    async def test_a_served_reply_confirms_on_the_real_firmware_report(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            finished = []
            s.on_reply_finished = lambda: finished.append(True)
            s.note_output_drained(batch)
            self.assertTrue(s.note_device_drain("wake_retained", firmware_metrics(batch)),
                            "the retained real firmware report shape must confirm; "
                            "underrun 1 / dry_chain_events 41 accompanied an "
                            "accepted, audible turn in the retained captures")
            self.assertEqual(finished, [True])
            self.assertIsNone(s._drain_pending)

    async def test_a_device_abandoned_turn_never_confirms_a_drain(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            armed, finished = [], []
            s._arm_confirmation_prompts = lambda: armed.append(True)
            s.on_reply_finished = lambda: finished.append(True)
            s.note_output_drained(batch)
            # `reason: "superseded"` was observed once in the retained real
            # captures. The guard used to read only `outcome`/`drain_timeout`,
            # which firmware 1.3.1 never sends, and defaulted both to "pass".
            self.assertFalse(s.note_device_drain(
                "wake_retained", firmware_metrics(batch, reason="superseded")))
            self.assertEqual((armed, finished), ([], []))

    async def test_a_report_without_a_completed_drain_never_confirms(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            s.note_output_drained(batch)
            self.assertFalse(s.note_device_drain(
                "wake_retained", firmware_metrics(batch, drained=None)))

    async def test_a_revoked_batch_releases_supervision_exactly_once(self):
        """The retained c0ffee01-20 signature, reproduced and bounded.

        A server-rejected continuation sets ``_unresolved_continuation``; the
        next provider delta then reaches ``invalidate()`` inside ``feed()``,
        which drops the enqueued segment. The service used to keep supervising
        the batch, so the firmware report could not match and the drain
        watchdog was certain to call a reply that really had been admitted and
        played "not confirmed".
        """
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            armed, finished, silent = [], [], []
            s._arm_confirmation_prompts = lambda: armed.append(True)
            s.on_reply_finished = lambda: finished.append(True)
            s.on_silent_response = lambda: silent.append(True)
            s.note_output_drained(batch)

            s._unresolved_continuation = True
            h.socket.push_all(fx.output_audio_deltas(fx.tone(100)))
            await asyncio.wait_for(StalledOutputStream._until(lambda: silent), 3)

            self.assertEqual(silent, [True], "reported once, not once per delta")
            self.assertIsNone(s._drain_pending)
            self.assertEqual(s._drain_batches, {})
            self.assertEqual((armed, finished), ([], []),
                             "a revoked batch arms nothing and opens no window")
            self.assertFalse(s.note_device_drain("wake_retained", firmware_metrics(batch)),
                             "a revoked batch still cannot be confirmed")
            h.socket.push_all(fx.output_audio_deltas(fx.tone(100)))
            await asyncio.sleep(.05)
            self.assertEqual(silent, [True])

    async def test_an_interrupted_reply_is_not_reported_as_an_unconfirmed_drain(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            s._confirmation_prompts_pending.add("confirm-1")
            armed, silent = [], []
            s._arm_confirmation_prompts = lambda: armed.append(True)
            s.on_silent_response = lambda: silent.append(True)
            s.note_output_drained(batch)

            self.assertTrue(await s.cancel_active_response("device stop", force=True))
            self.assertIsNone(s._drain_pending)
            self.assertEqual(s._drain_batches, {})
            self.assertEqual(s._confirmation_prompts_pending, set(),
                             "an interrupted prompt must never arm an action")
            self.assertIsNone(s._drain_timeout_task)
            await asyncio.sleep(.05)
            self.assertEqual((armed, silent), ([], []),
                             "an interruption is not an unconfirmed drain")
            self.assertFalse(s.note_device_drain("wake_retained", firmware_metrics(batch)))

    async def test_a_drain_that_never_arrives_is_still_reported(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            armed, silent = [], []
            s._arm_confirmation_prompts = lambda: armed.append(True)
            s.on_silent_response = lambda: silent.append(True)
            await s._drain_watch(s._drain_pending, 0)
            self.assertEqual((armed, silent), ([], [True]),
                             "a genuinely missing firmware report must still "
                             "leave confirmation unarmed and close the turn")


class ShortReplyBatchOrdering(unittest.IsolatedAsyncioTestCase):
    """Output pacing vs batch boundaries, through the real device path.

    ``OutputLeadBuffer`` holds the first ``lead_ms`` of a reply so the device's
    audio chain starts with a playout lead (600 ms under Live). A reply shorter
    than the lead is still being held when the service pushes its ordered
    ``output_batch_end`` marker, and every frame type the lead buffer does not
    recognise is passed straight downstream — so the marker overtook its own
    PCM. A compatible device associates a marker with the PCM that preceded
    it, so an early marker either never confirms (a short reply can then never
    complete a turn) or confirms before the audio has drained, which is false
    delivery evidence.

    This is the in-tree home of the marker-ordering property. The retained
    packet captures check the same thing from outside: the capture verifier
    annotates each marker with ``preceding_pcm_bytes``, the audio bytes it had
    already seen on that stream, and compares it to the marker's own
    ``pcm_bytes``. That count is the *verifier's* measurement and appears in
    no ``turn_metrics`` report, so the only place the ordering can be asserted
    against this runtime is here.
    """

    async def test_a_short_reply_sends_its_marker_after_all_of_its_pcm(self):
        from tests.test_live_device_audio import DevicePath

        async with DevicePath(lead_ms=600, admit_complete_replies=True) as p:
            s = p.service
            s._user_transcript_seen_for_response = True
            s.turn_timeline.begin("wake_retained")
            admission = s.reply_admission
            # ~300 ms of speech: half the playout lead, so the lead is still
            # held when the batch marker is pushed.
            admission.transcript("Done.", end_ms=300)
            admission.feed(OutputChunk(pcm=fx.tone(300), audible=True), False, end_ms=300)
            reply = admission.feed(
                OutputChunk(reason="idle run exceeded max_silence_ms"), False, end_ms=300)
            self.assertIsNotNone(reply)
            await s._deliver_reply(reply)

            async def marker_arrived():
                while not p.device.text:
                    await asyncio.sleep(.005)
            await asyncio.wait_for(marker_arrived(), 3)

            marker = json.loads(p.device.text[-1])
            self.assertEqual(marker["type"], "output_batch_end")
            self.assertGreaterEqual(
                len(p.device.pcm), marker["pcm_bytes"],
                "the marker must never reach the device ahead of its own PCM: "
                f"{len(p.device.pcm)} of {marker['pcm_bytes']} bytes had arrived")


# Last in the file on purpose: everything above it is a class, and a
# ``unittest.main()`` placed mid-file makes every class below it unreachable
# when the file is run directly, which is how the short-reply marker-ordering
# regression silently dropped out of `python tests/test_live_retained_failures.py`
# while staying green under discovery.
if __name__ == "__main__":  # pragma: no cover
    unittest.main()
