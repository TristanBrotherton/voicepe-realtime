"""The on-demand GPT-Live session lifecycle.

Every test here is a cost assertion in disguise. The hard invariant is ZERO
GPT-Live session seconds while the device is idle, and the only thing GPT-Live
bills is time, so these tests drive a fake monotonic clock and check what the
gate opened, when it closed, and what the ledger was charged.

The clock is injected and advanced explicitly, so no assertion depends on wall
time; ``_settle`` only waits for the supervisor task to *observe* a clock that
has already moved.
"""
import asyncio
import unittest

from app.live_cost import LiveSpendLedger, session_cost_usd
from app.live_session_gate import CLOSED, OPEN, OPENING, LiveSessionGate, LiveSessionPolicy


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeLiveService:
    """The whole surface LiveSessionGate uses, and nothing else."""

    def __init__(self, *, fail_open=False, auto_ready=True):
        self.opens = 0
        self.closes = []
        self.fail_open = fail_open
        self.auto_ready = auto_ready
        self.session_ready = False
        self.session_id = None
        self.session_expires_at = None
        self.session_seconds = 0.0
        self.response_active = False
        self.backend_work_active = False
        self.open_calls_in_flight = 0
        self.max_concurrent_opens = 0

    async def open_session(self):
        self.open_calls_in_flight += 1
        self.max_concurrent_opens = max(self.max_concurrent_opens, self.open_calls_in_flight)
        try:
            # A real open awaits a TCP+TLS+WebSocket handshake. Yield so a
            # concurrent caller genuinely gets a chance to interleave.
            await asyncio.sleep(0)
            if self.fail_open:
                raise RuntimeError("connect refused")
            self.opens += 1
            self.session_id = f"sess_{self.opens}"
            if self.auto_ready:
                self.session_ready = True
        finally:
            self.open_calls_in_flight -= 1

    async def close_session(self, reason=""):
        self.closes.append(reason)
        self.session_ready = False
        self.session_id = None
        self.response_active = False
        self.backend_work_active = False


class GateCase(unittest.IsolatedAsyncioTestCase):
    """A gate on a fake clock, a fake service and an in-memory ledger."""

    def make(self, *, policy=None, service=None, ledger=None, **ledger_kwargs):
        self.clock = Clock()
        self.wall = Clock(1_700_000_000.0)
        self.service = service or FakeLiveService()
        ledger_kwargs.setdefault("path", None)
        self.ledger = ledger or LiveSpendLedger(clock=self.wall, **ledger_kwargs)
        self.refusals = []
        self.charges = []
        self.gate = LiveSessionGate(
            self.service,
            device_id="kitchen",
            policy=policy or LiveSessionPolicy(
                idle_s=10.0, followup_s=8.0, max_silent_s=45.0,
                max_session_s=300.0, open_timeout_s=10.0, tick_s=0.005,
            ),
            ledger=self.ledger,
            monotonic=self.clock,
            wall_clock=self.wall,
            on_refused=self._refused,
            on_closed=self.charges.append,
        )
        return self.gate

    async def _refused(self, reason):
        self.refusals.append(reason)

    async def _settle(self, predicate, timeout=2.0):
        """Wait for the supervisor to act on a clock the test already moved."""
        async def _wait():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(_wait(), timeout)

    async def advance(self, seconds, *, step=None):
        """Move the clock and let the supervisor see it.

        Advanced in steps no larger than the tightest deadline so the
        supervisor cannot skip over a bound it should have enforced.
        """
        step = step or seconds
        remaining = seconds
        while remaining > 0:
            self.clock.advance(min(step, remaining))
            remaining -= step
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        gate = getattr(self, "gate", None)
        if gate is not None:
            await gate.shutdown("test teardown")


class TestIdleCostsNothing(GateCase):
    async def test_startup_opens_no_session(self):
        gate = self.make()
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(self.service.opens, 0)
        self.assertEqual(gate.opens, 0)
        self.assertEqual(self.ledger.window_seconds, 0.0)

    async def test_eight_hours_of_idle_bill_zero(self):
        """The invariant, stated as money.

        The previous architecture opened a session on device connect and
        refreshed it before the 60-minute cap: eight idle hours were eight
        billed hours ($24.00). Here they are free.
        """
        gate = self.make()
        await self.advance(8 * 3600, step=300)
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(self.service.opens, 0, "idle must never open a session")
        self.assertEqual(gate.open_seconds, 0.0)
        self.assertEqual(self.ledger.window_seconds, 0.0)
        self.assertEqual(self.ledger.window_usd, 0.0)
        self.assertEqual(gate.snapshot()["spend"]["usd"], 0.0)
        # What the old architecture would have cost over the same wall time.
        self.assertAlmostEqual(session_cost_usd(8 * 3600), 24.0, places=6)

    async def test_activity_notes_while_closed_cannot_open_anything(self):
        gate = self.make()
        gate.note_user_audio()
        gate.note_audible_output()
        gate.note_backend_activity()
        gate.note_reply_finished()
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(self.service.opens, 0)

    async def test_no_reconnect_loop_while_idle(self):
        """A close must never cause a reopen, however many times it happens."""
        gate = self.make()
        for _ in range(20):
            await gate.close("connection lost while idle")
            await self.advance(60, step=30)
        self.assertEqual(self.service.opens, 0)
        self.assertEqual(gate.opens, 0)
        self.assertEqual(self.ledger.window_seconds, 0.0)


class TestOpening(GateCase):
    async def test_wake_opens_exactly_one_session(self):
        gate = self.make()
        self.assertTrue(await gate.open("wake (wake_word)"))
        self.assertEqual(gate.state, OPEN)
        self.assertEqual(self.service.opens, 1)
        self.assertEqual(gate.generation, 1)

    async def test_concurrent_wakes_are_single_flight(self):
        gate = self.make()
        results = await asyncio.gather(*[gate.open(f"wake {i}") for i in range(8)])
        self.assertTrue(all(results))
        self.assertEqual(self.service.opens, 1, "eight wakes bought one session")
        self.assertEqual(self.service.max_concurrent_opens, 1)
        self.assertEqual(gate.generation, 1)

    async def test_a_wake_racing_a_reconnect_is_single_flight(self):
        gate = self.make()
        await gate.open("wake")
        # A reconnect attempt and a fresh wake land together.
        await asyncio.gather(gate.open("wake"), gate.open("reconnect"))
        self.assertEqual(self.service.opens, 1)
        self.assertEqual(gate.generation, 1)

    async def test_a_wake_during_the_handshake_does_not_open_a_second_session(self):
        """The OPENING window is the race the single-flight lock exists for."""
        gate = self.make()
        service = self.service
        handshake = asyncio.Event()
        original = service.open_session

        async def slow_open():
            await handshake.wait()
            await original()

        service.open_session = slow_open
        first = asyncio.create_task(gate.open("wake"))
        await asyncio.sleep(0.01)
        self.assertEqual(gate.state, OPENING)
        self.assertTrue(gate.is_open, "a session is on its way; do not start another")

        second = asyncio.create_task(gate.open("wake"))
        await asyncio.sleep(0.01)
        handshake.set()
        self.assertTrue(all(await asyncio.gather(first, second)))
        self.assertEqual(service.opens, 1)
        self.assertEqual(gate.generation, 1)

    async def test_a_second_wake_only_extends_the_open_session(self):
        gate = self.make()
        await gate.open("wake")
        await self.advance(5)
        await gate.open("wake")
        await self.advance(7, step=1)
        self.assertEqual(gate.state, OPEN, "the second wake pushed the idle deadline out")
        self.assertEqual(self.service.opens, 1)

    async def test_a_failed_open_closes_everything_and_charges_nothing(self):
        gate = self.make(service=FakeLiveService(fail_open=True))
        self.assertFalse(await gate.open("wake"))
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(gate.open_failures, 1)
        self.assertEqual(self.service.closes, ["open failed"], "the socket is released")
        self.assertEqual(self.ledger.window_seconds, 0.0)

    async def test_a_failed_open_does_not_start_a_reopen_loop(self):
        gate = self.make(service=FakeLiveService(fail_open=True))
        for _ in range(5):
            self.assertFalse(await gate.open("wake"))
        await self.advance(600, step=60)
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(self.ledger.window_seconds, 0.0)

    async def test_shutdown_is_permanent(self):
        gate = self.make()
        await gate.shutdown("device disconnected")
        self.assertFalse(await gate.open("wake"))
        self.assertEqual(self.service.opens, 0)


class TestRewakeGrace(GateCase):
    async def test_default_followup_close_keeps_zero_grace_contract(self):
        gate = self.make()
        await gate.open("wake")
        self.assertFalse(gate.note_followup_closed())
        await gate.close("follow-up cut-off")
        self.assertEqual(self.service.closes, ["follow-up cut-off"])

    async def test_followup_close_waits_while_the_service_is_still_speaking_a_result(self):
        # Rig 2026-10-14: the device's follow-up window closed one word into a
        # background result being spoken. With work in flight the session is
        # kept under the idle bound and closes "idle" once that work is over.
        gate = self.make()
        await gate.open("wake")
        gate.note_reply_finished()
        self.service.backend_work_active = True
        self.assertTrue(gate.note_followup_closed(), "retained: a result is being spoken")
        self.assertEqual(self.service.closes, [])
        await self.advance(9.0)
        self.assertEqual(self.service.closes, [], "still speaking, still open")
        self.service.backend_work_active = False
        await self.advance(11.0)
        await self._settle(lambda: bool(self.service.closes))
        self.assertEqual(self.service.closes, ["idle"])

    async def test_wake_inside_grace_reuses_ready_session(self):
        gate = self.make(policy=LiveSessionPolicy(
            idle_s=10.0, followup_s=8.0, rewake_grace_s=6.0,
            max_silent_s=45.0, max_session_s=300.0,
            open_timeout_s=10.0, tick_s=0.005,
        ))
        await gate.open("wake")
        gate.note_reply_finished()
        self.assertTrue(gate.note_followup_closed())
        await self.advance(4.0)
        self.assertTrue(await gate.open("wake"))
        self.assertEqual(self.service.opens, 1)
        self.assertEqual(self.service.closes, [])

    async def test_grace_expires_and_closes(self):
        gate = self.make(policy=LiveSessionPolicy(
            idle_s=10.0, followup_s=8.0, rewake_grace_s=3.0,
            max_silent_s=45.0, max_session_s=300.0,
            open_timeout_s=10.0, tick_s=0.005,
        ))
        await gate.open("wake")
        gate.note_reply_finished()
        self.assertTrue(gate.note_followup_closed())
        await self.advance(3.1)
        await self._settle(lambda: bool(self.service.closes))
        self.assertEqual(self.service.closes, ["re-wake grace"])


class TestClosing(GateCase):
    async def test_normal_completion_plus_follow_up_quiet_closes(self):
        gate = self.make()
        await gate.open("wake")
        gate.note_user_audio()
        gate.note_audible_output()
        gate.note_reply_finished()
        # Inside the follow-up window the session is still there for a
        # follow-up question.
        await self.advance(5, step=1)
        self.assertEqual(gate.state, OPEN)
        # Then the window expires and it closes on its own.
        await self.advance(5, step=1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["follow-up quiet"])

    async def test_pre_tool_ack_does_not_start_follow_up_quiet(self):
        gate = self.make()
        await gate.open("wake")
        self.service.response_active = True
        self.service.backend_work_active = True

        gate.note_reply_finished()  # the local "one moment" acknowledgement

        self.assertTrue(gate.turn_in_flight)
        self.assertEqual(gate._idle_reason, "idle")
        await self.advance(9, step=1)  # past the normal follow-up window
        self.assertEqual(gate.state, OPEN)

        self.service.response_active = False
        self.service.backend_work_active = False
        gate.note_reply_finished()  # the actual answer drained on the device
        self.assertFalse(gate.turn_in_flight)
        await self.advance(9, step=1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["follow-up quiet"])

    async def test_backend_activity_reopens_a_premature_follow_up_window(self):
        gate = self.make()
        await gate.open("wake")
        gate.note_reply_finished()
        self.assertEqual(gate._idle_reason, "follow-up quiet")

        self.service.response_active = True
        self.service.backend_work_active = True
        gate.note_backend_activity()

        self.assertTrue(gate.turn_in_flight)
        self.assertEqual(gate._idle_reason, "idle")
        await self.advance(9, step=1)
        self.assertEqual(gate.state, OPEN)

    async def test_expiry_cannot_close_follow_up_while_backend_is_active(self):
        gate = self.make()
        await gate.open("wake")
        gate.note_reply_finished()
        self.service.response_active = True
        self.service.backend_work_active = True

        await self.advance(9, step=1)

        self.assertEqual(gate.state, OPEN)
        self.assertTrue(gate.turn_in_flight)
        self.assertEqual(gate._idle_reason, "idle")

    async def test_a_follow_up_inside_the_window_reuses_the_session(self):
        gate = self.make()
        await gate.open("wake")
        gate.note_reply_finished()
        await self.advance(5, step=1)
        gate.note_user_utterance()      # actual speech, not an open quiet mic
        await self.advance(9, step=1)   # past the original follow-up deadline
        self.assertEqual(gate.state, OPEN)
        self.assertEqual(self.service.opens, 1, "no second handshake was paid for")

    async def test_wake_with_no_speech_closes_on_idle(self):
        gate = self.make()
        await gate.open("wake")
        await self.advance(11, step=1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["idle"])

    async def test_max_lifetime_closes_even_while_busy(self):
        policy = LiveSessionPolicy(idle_s=10.0, followup_s=8.0, max_silent_s=30.0,
                                   max_session_s=60.0, tick_s=0.005)
        gate = self.make(policy=policy)
        await gate.open("wake")
        self.service.response_active = True
        # Keep every softer deadline permanently fresh, so only the absolute
        # lifetime can end this.
        for _ in range(14):
            gate.note_user_audio()
            gate.note_audible_output()
            await self.advance(5)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["max lifetime"])
        self.assertLessEqual(gate.last_charge.seconds, 70.0)

    async def test_inaudible_output_cannot_keep_a_session_alive(self):
        """The anti-silence backstop.

        GPT-Live streams continuous digital silence between replies and has no
        output-audio-done event, so ``response_active`` can stay true forever.
        Only audible output may extend a billed session.
        """
        policy = LiveSessionPolicy(idle_s=5.0, followup_s=5.0, max_silent_s=20.0,
                                   max_session_s=600.0, tick_s=0.005)
        gate = self.make(policy=policy)
        await gate.open("wake")
        self.service.response_active = True  # "speaking", but nothing audible
        for _ in range(10):
            gate.note_backend_activity()     # extends idle, never silent
            await self.advance(3)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["silent"])
        self.assertLess(gate.last_charge.seconds, 40.0)

    async def test_audible_output_does_keep_it_alive(self):
        policy = LiveSessionPolicy(idle_s=5.0, followup_s=5.0, max_silent_s=20.0,
                                   max_session_s=600.0, tick_s=0.005)
        gate = self.make(policy=policy)
        await gate.open("wake")
        for _ in range(10):
            gate.note_audible_output()
            await self.advance(3)
        self.assertEqual(gate.state, OPEN, "a long real answer is not cut off")

    async def test_backend_work_defers_idle_but_not_the_silent_bound(self):
        policy = LiveSessionPolicy(idle_s=5.0, followup_s=5.0, max_silent_s=30.0,
                                   max_session_s=600.0, tick_s=0.005)
        gate = self.make(policy=policy)
        await gate.open("wake")
        self.service.response_active = True
        await self.advance(10, step=1)
        self.assertEqual(gate.state, OPEN, "a tool in flight is not an idle session")
        await self.advance(25, step=1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["silent"])

    async def test_a_routed_request_still_running_is_not_closed_as_silent(self):
        """A slow agent question (router mode) keeps the session past the
        silent bound; max lifetime still ends it."""
        policy = LiveSessionPolicy(idle_s=5.0, followup_s=5.0, max_silent_s=20.0,
                                   max_session_s=90.0, tick_s=0.005)
        gate = self.make(policy=policy)
        await gate.open("wake")
        self.service.response_active = True
        self.service.router_pending = True
        await self.advance(60, step=1)
        self.assertEqual(gate.state, OPEN, "never closed under a pending request")
        await self.advance(40, step=1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["max lifetime"])

    async def test_handshake_timeout_closes(self):
        policy = LiveSessionPolicy(idle_s=10.0, followup_s=8.0, max_silent_s=45.0,
                                   max_session_s=300.0, open_timeout_s=5.0, tick_s=0.005)
        gate = self.make(policy=policy, service=FakeLiveService(auto_ready=False))
        await gate.open("wake")
        self.assertEqual(gate.state, OPEN)
        await self.advance(6, step=1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["handshake timeout"])

    async def test_server_expiry_closes_ahead_of_the_server(self):
        gate = self.make()
        await gate.open("wake")
        self.service.session_expires_at = self.wall() + 20.0
        gate.note_audible_output()
        self.wall.advance(6.0)
        await self.advance(1)
        self.assertEqual(gate.state, OPEN)
        self.wall.advance(1.0)   # now inside the 15 s margin
        await self.advance(1)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["server expiry"])

    async def test_explicit_closes_are_recorded_by_reason(self):
        for reason in ("device stop", "follow-up cut-off", "device disconnected",
                       "connection lost while idle"):
            gate = self.make()
            await gate.open("wake")
            self.assertTrue(await gate.close(reason))
            self.assertEqual(gate.state, CLOSED)
            self.assertEqual(self.service.closes, [reason])
            self.assertEqual(gate.closes, {reason: 1})
            await gate.shutdown()

    async def test_close_is_idempotent(self):
        gate = self.make()
        await gate.open("wake")
        self.assertTrue(await gate.close("device stop"))
        self.assertFalse(await gate.close("device stop"))
        self.assertEqual(self.service.closes, ["device stop"])

    async def test_a_stale_close_cannot_kill_the_current_session(self):
        """Generation fencing: the race-safe ownership requirement."""
        gate = self.make()
        await gate.open("wake")
        stale = gate.generation
        await gate.close("follow-up cut-off")
        await gate.open("wake")                       # a new turn, generation 2
        self.assertFalse(await gate.close("idle", generation=stale))
        self.assertEqual(gate.state, OPEN)
        self.assertEqual(self.service.closes, ["follow-up cut-off"])

    async def test_a_closed_session_stops_accruing_cost(self):
        gate = self.make()
        await gate.open("wake")
        await self.advance(4)
        await gate.close("device stop")
        before = self.ledger.window_seconds
        await self.advance(3600, step=300)
        self.assertEqual(self.ledger.window_seconds, before)
        self.assertEqual(gate.open_seconds, 0.0)


class TestTelemetry(GateCase):
    async def test_session_duration_and_cost_are_correct(self):
        gate = self.make()
        await gate.open("wake")
        gate.note_audible_output()
        await self.advance(30)
        await gate.close("device stop")
        charge = gate.last_charge
        self.assertAlmostEqual(charge.seconds, 30.0, places=3)
        self.assertAlmostEqual(charge.usd, 30 * 0.05 / 60, places=8)
        self.assertEqual(charge.device_id, "kitchen")
        self.assertEqual(charge.reason, "device stop")
        self.assertAlmostEqual(self.ledger.window_seconds, 30.0, places=3)

    async def test_the_server_count_wins_when_it_is_larger(self):
        """usage.seconds is authoritative; it just arrives once a minute."""
        gate = self.make()
        await gate.open("wake")
        await self.advance(10)
        self.service.session_seconds = 42.0
        self.assertAlmostEqual(gate.open_seconds, 42.0, places=3)
        await gate.close("device stop")
        self.assertAlmostEqual(gate.last_charge.seconds, 42.0, places=3)

    async def test_several_sessions_accumulate(self):
        gate = self.make()
        for _ in range(3):
            await gate.open("wake")
            await self.advance(20)
            await gate.close("follow-up cut-off")
        self.assertEqual(gate.opens, 3)
        self.assertEqual(gate.closes, {"follow-up cut-off": 3})
        self.assertAlmostEqual(self.ledger.window_seconds, 60.0, places=2)
        self.assertAlmostEqual(self.ledger.window_usd, 0.05, places=5)
        self.assertEqual(len(self.charges), 3)

    async def test_snapshot_reports_the_open_session_too(self):
        gate = self.make()
        await gate.open("wake")
        await self.advance(60)
        snapshot = gate.snapshot()
        self.assertEqual(snapshot["state"], OPEN)
        self.assertAlmostEqual(snapshot["open_session_seconds"], 60.0, places=2)
        self.assertAlmostEqual(snapshot["open_session_usd"], 0.05, places=5)
        self.assertAlmostEqual(snapshot["spend"]["session_minutes"], 1.0, places=3)

    async def test_hours_of_idle_project_zero_cost(self):
        gate = self.make()
        await self.advance(6 * 3600, step=600)
        snapshot = gate.snapshot()
        self.assertEqual(snapshot["opens"], 0)
        self.assertEqual(snapshot["open_session_seconds"], 0.0)
        self.assertEqual(snapshot["spend"]["session_seconds"], 0.0)
        self.assertEqual(snapshot["spend"]["usd"], 0.0)


class TestSpendCutoff(GateCase):
    async def test_the_cutoff_refuses_a_new_session(self):
        gate = self.make(max_session_minutes_per_day=1.0, max_usd_per_day=0.0)
        await gate.open("wake")
        await self.advance(65)
        await gate.close("device stop")
        self.assertFalse(await gate.open("wake"), "over budget")
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(gate.refusals, 1)
        self.assertEqual(self.service.opens, 1)
        self.assertEqual(len(self.refusals), 1)
        self.assertIn("cutoff reached", self.refusals[0])

    async def test_the_cutoff_closes_a_session_already_running(self):
        gate = self.make(max_session_minutes_per_day=1.0, max_usd_per_day=0.0)
        await gate.open("wake")
        for _ in range(14):
            gate.note_audible_output()
            await self.advance(5)
        await self._settle(lambda: gate.state == CLOSED)
        self.assertEqual(self.service.closes, ["spend cutoff"])

    async def test_budget_recovers_when_the_window_rolls(self):
        gate = self.make(max_session_minutes_per_day=1.0, max_usd_per_day=0.0)
        await gate.open("wake")
        await self.advance(65)
        await gate.close("device stop")
        self.assertFalse(await gate.open("wake"))
        self.wall.advance(25 * 3600)
        self.assertTrue(await gate.open("wake"))
        self.assertEqual(self.service.opens, 2)

    async def test_a_refused_turn_is_not_left_in_flight(self):
        gate = self.make(max_session_minutes_per_day=1.0, max_usd_per_day=0.0)
        await gate.open("wake")
        await self.advance(65)
        await gate.close("device stop")
        self.assertFalse(await gate.open("wake"))
        self.assertFalse(gate.turn_in_flight, "nothing to reconnect for")


class TestReconnectOwnership(GateCase):
    async def test_turn_in_flight_only_while_a_request_is_unresolved(self):
        gate = self.make()
        self.assertFalse(gate.turn_in_flight, "closed: never worth reconnecting")
        await gate.open("wake")
        self.assertTrue(gate.turn_in_flight)
        gate.note_reply_finished()
        self.assertFalse(gate.turn_in_flight, "answered: a drop is just a close")
        await gate.close("follow-up quiet")
        self.assertFalse(gate.turn_in_flight)

    async def test_note_turn_resolved_clears_it(self):
        gate = self.make()
        await gate.open("wake")
        gate.note_turn_resolved()
        self.assertFalse(gate.turn_in_flight)

    async def test_opening_without_a_turn_does_not_arm_reconnection(self):
        gate = self.make()
        await gate.open("maintenance", turn=False)
        self.assertEqual(gate.state, OPEN)
        self.assertFalse(gate.turn_in_flight)


class TestPolicySanity(unittest.TestCase):
    def test_clamps_keep_the_invariant(self):
        policy = LiveSessionPolicy(
            idle_s=0.0, followup_s=-5.0, max_silent_s=1.0, max_session_s=1.0,
            open_timeout_s=0.0, expiry_margin_s=-1.0, tick_s=99.0,
        ).sanity_checked()
        self.assertEqual(policy.idle_s, 1.0)
        self.assertEqual(policy.followup_s, 0.0)
        self.assertGreaterEqual(policy.max_silent_s, policy.idle_s)
        self.assertGreater(policy.max_session_s, policy.max_silent_s)
        self.assertEqual(policy.open_timeout_s, 1.0)
        self.assertEqual(policy.expiry_margin_s, 0.0)
        self.assertLessEqual(policy.tick_s, 1.0)

    def test_defaults_bound_one_session_to_pocket_change(self):
        policy = LiveSessionPolicy()
        self.assertLessEqual(session_cost_usd(policy.max_session_s), 0.25)


if __name__ == "__main__":
    unittest.main()


class TestDiscussionBounds(GateCase):
    """Discussions by voice: an open discussion runs on longer bounds, inside
    the daily caps, until the session closes."""

    def policy(self):
        return LiveSessionPolicy(idle_s=10.0, followup_s=8.0, max_silent_s=45.0,
                                 max_session_s=90.0, tick_s=0.005)

    async def test_a_discussion_outlives_the_normal_maximum(self):
        gate = self.make(policy=self.policy(), max_session_minutes_per_day=60.0, max_usd_per_day=10.0)
        await gate.open("wake")
        self.assertTrue(gate.begin_discussion(900.0, 30.0))
        self.service.response_active = True
        for _ in range(30):  # 150 s, past the normal 90 s
            gate.note_user_audio()
            gate.note_audible_output()
            await self.advance(5)
        self.assertEqual(gate.state, OPEN, "the discussion bound applies, not 90 s")
        self.assertEqual(gate.policy.idle_s, 30.0)

    async def test_the_daily_cap_still_bounds_a_discussion(self):
        gate = self.make(policy=self.policy(), max_session_minutes_per_day=3.0, max_usd_per_day=10.0)
        await gate.open("wake")
        gate.begin_discussion(900.0, 30.0)
        self.assertLessEqual(gate.policy.max_session_s, 180.0)
        self.service.response_active = True
        for _ in range(60):
            gate.note_user_audio()
            gate.note_audible_output()
            await self.advance(5)
            if gate.state == CLOSED:
                break
        await self._settle(lambda: gate.state == CLOSED)
        self.assertLessEqual(gate.last_charge.seconds, 180.0)

    async def test_the_operators_policy_returns_at_close(self):
        gate = self.make(policy=self.policy(), max_session_minutes_per_day=60.0, max_usd_per_day=10.0)
        await gate.open("wake")
        gate.begin_discussion(900.0, 30.0)
        await gate.close("test")
        self.assertEqual(gate.policy.max_session_s, 90.0)
        self.assertEqual(gate.policy.idle_s, 10.0)
        self.assertFalse(gate.discussion_active)

    async def test_no_discussion_without_an_open_session(self):
        gate = self.make(policy=self.policy())
        self.assertFalse(gate.begin_discussion(900.0, 30.0))
