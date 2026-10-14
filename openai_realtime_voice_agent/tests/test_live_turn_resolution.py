"""Second-iteration regressions: every stuck turn must resolve on a local bound.

Found by auditing the A4 repair (`REPLY_RESOLUTION_S`) rather than from a new
household failure, so these are pinned before any device test rather than
after one.

The A4 bound defers while ``backend_work_active`` is true, which is correct for
genuine work in flight: a delegation or a busy tool ledger still owes the user
an answer and is itself bounded by ``DELEGATION_TIMEOUT_S``.
``_unresolved_continuation`` is **not** work in flight. It is a terminal dead
end for the turn — set when the server rejects a continuation this client
issued, or when single-flight refuses to issue one — and nothing clears it
except a new utterance or a disconnect. Counting it as live work made the
resolver defer forever on exactly the symptom A4 exists to remove: no reply, no
named reason, and the turn recorded as a bare ``outcome: "error"`` by the phase
emitter's generic 15 s thinking watchdog.

Fail-closed is unchanged by this: an unresolved continuation still admits
nothing and still never replays the tool. It simply closes the turn as silent
with the evidence state named in ``failure_reason``, inside the bound, instead
of hanging mutely.
"""
import asyncio
import unittest
from unittest.mock import patch

from pipecat.frames.frames import LLMFullResponseEndFrame, TTSAudioRawFrame

import app.live_service as live_service_module
from app.live_audio import OutputChunk
from app.live_service import KNOWN_DRAIN_REASONS
from tests import live_fixtures as fx
from tests.live_harness import LiveHarness
from tests.test_live_retained_failures import firmware_metrics


class UnresolvedContinuationResolves(unittest.IsolatedAsyncioTestCase):
    """A dead-end continuation must close its turn on the local bound."""

    def setUp(self):
        # Same shortening the A4 regressions use: the real bound is 6 s.
        patcher = patch.object(live_service_module, "REPLY_RESOLUTION_S", .05)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _held_with_dead_end(self, h):
        """A held reply whose continuation the server rejected."""
        await h.start_session()
        s = h.service
        s.turn_timeline.begin("wake_resolution")
        s._user_transcript_seen_for_response = True
        h.socket.push({"type": "session.output_transcript.delta",
                       "delta": "I looked that up for you.",
                       "start_ms": 100, "end_ms": 400})
        await asyncio.sleep(.01)
        # The exact state `_on_error` leaves behind when the endpoint rejects a
        # continuation: the tool result may have been accepted, the answer is
        # unresolved, and it must never be replayed. The replay latch is armed
        # here because the tool dispatch that produced the delegation armed it
        # (`_unsafe_to_replay_input = True` before any handler side effect) and
        # nothing on this path clears it — no reply audio was ever heard.
        s._unresolved_continuation = True
        s._unsafe_to_replay_input = True
        self.assertFalse(s._delegations, "no work may be in flight")
        self.assertFalse(s.ledger.busy, "no work may be in flight")
        self.assertTrue(s.backend_work_active,
                        "the latch is what makes the resolver defer")
        h.socket.push_all(fx.output_audio_deltas(fx.tone(400)))
        await asyncio.sleep(.01)
        self.assertTrue(s._reply_candidate_held(),
                        "the fixture must leave a candidate held")
        self.assertIsNotNone(s._reply_resolution_task,
                             "a held reply must be supervised")
        return s

    async def test_a_rejected_continuation_closes_its_turn_on_the_bound(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            silent = []
            s = await self._held_with_dead_end(h)
            s.on_silent_response = lambda: silent.append(True)

            await asyncio.wait_for(self._until(lambda: silent), 3)

            self.assertTrue(s.reply_admission.terminal,
                            "the turn must fail closed, not wait for the "
                            "generic thinking watchdog")
            self.assertIn("continuations awaiting a response",
                          s.reply_admission.failure_reason)

    async def test_a_rejected_continuation_still_admits_nothing(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            silent = []
            s = await self._held_with_dead_end(h)
            s.on_silent_response = lambda: silent.append(True)

            await asyncio.wait_for(self._until(lambda: silent), 3)

            # The answer was never resolved, so no speech may reach the device.
            self.assertEqual(h.sink.of(LLMFullResponseEndFrame), [])
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            # And the close must leave replay safety armed. A tool may already
            # have changed the house, so resolving the turn as silent is not
            # permission to feed the same audio to a fresh model session; this
            # fails if the new local bound clears the latch on its way out.
            self.assertIs(s._unsafe_to_replay_input, True,
                          "closing a dead-end turn must not make its input "
                          "replayable")

    async def test_real_work_in_flight_still_defers(self):
        """The deferral that matters must survive this change."""
        async with LiveHarness(self, admit_complete_replies=True) as h:
            s = await self._held_with_dead_end(h)
            s._delegations["still-running"] = (
                asyncio.get_running_loop().create_future())

            await asyncio.sleep(.3)

            self.assertFalse(s.reply_admission.terminal,
                             "a tool still owes the user an answer")
            self.assertEqual(h.sink.of(LLMFullResponseEndFrame), [])
            s._delegations.clear()

    @staticmethod
    async def _until(predicate):
        while not predicate():
            await asyncio.sleep(.005)


class UnknownDrainReasonIsDiagnosable(unittest.IsolatedAsyncioTestCase):
    """`reason` became a rejection signal from a 12-report sample.

    Only "reply" may confirm a drain, and that stays true here. But a benign
    firmware value from outside that sample would withhold every
    otherwise-good drain, and a bare ``return False`` is indistinguishable
    from a report that never arrived — which over a multi-day soak surfaces
    only as a generic "drain not confirmed" warning. The unknown must name
    itself, exactly once per distinct value.
    """

    async def _delivered_reply(self, h):
        s = h.service
        s.turn_timeline.begin("wake_resolution")
        s._user_transcript_seen_for_response = True
        admission = s.reply_admission
        admission.transcript("Added blueberries to your shopping list.",
                             end_ms=400)
        admission.feed(OutputChunk(pcm=fx.tone(2400), audible=True), False,
                       end_ms=400)
        reply = admission.feed(
            OutputChunk(reason="idle run exceeded max_silence_ms"), False,
            end_ms=400)
        self.assertIsNotNone(reply)
        await s._deliver_reply(reply)
        return s, next(iter(s._drain_batches))

    async def test_an_unknown_reason_is_rejected_and_named_once(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            armed, finished = [], []
            s._arm_confirmation_prompts = lambda: armed.append(True)
            s.on_reply_finished = lambda: finished.append(True)
            s.note_output_drained(batch)

            with self.assertLogs("app.live_service", level="ERROR") as logs:
                self.assertFalse(
                    s.note_device_drain("wake_resolution", firmware_metrics(
                        batch, reason="reply_complete")),
                    "an unrecognised disposition must still fail closed")
            self.assertEqual((armed, finished), ([], []),
                             "nothing may be armed by a rejected report")
            self.assertIn("UNRECOGNISED", "".join(logs.output))
            self.assertIn("reply_complete", "".join(logs.output))

            # Same value again on a later turn: already reported, stays quiet.
            self.assertIn("reply_complete", s._unknown_drain_reasons)

    async def test_the_observed_abandoned_reason_is_not_reported_as_unknown(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            s.note_output_drained(batch)

            self.assertIn("superseded", KNOWN_DRAIN_REASONS)
            self.assertFalse(s.note_device_drain(
                "wake_resolution", firmware_metrics(batch, reason="superseded")))
            self.assertEqual(s._unknown_drain_reasons, set(),
                             "a disposition this house has observed is not an "
                             "unknown; only 'reply' confirms, and that is "
                             "asserted in the retained-failure regressions")

    async def test_a_plain_reply_still_confirms(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            s, batch = await self._delivered_reply(h)
            finished = []
            s.on_reply_finished = lambda: finished.append(True)
            s.note_output_drained(batch)

            self.assertTrue(s.note_device_drain(
                "wake_resolution", firmware_metrics(batch)))
            self.assertEqual(finished, [True])


if __name__ == "__main__":
    unittest.main()
