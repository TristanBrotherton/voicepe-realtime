"""The production pipeline actually wires the on-demand session gate.

``tests/test_live_session_gate.py`` proves the policy and
``tests/test_live_on_demand.py`` proves the service. This module proves the
*wiring*: that real device control frames — wake, stop, follow-up cut-off,
disconnect — reach the gate through ``WebSocketHandler.build_pipeline``, and
that the Realtime runtime gets no gate at all.

Nothing is contacted: the service is a FrameProcessor that records what the
gate asked it to do.
"""
import asyncio
import json
import tempfile
import unittest

from pipecat.frames.frames import ErrorFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.device_registry import DeviceConnection
from app.live_cost import LiveSpendLedger
from app.live_session_gate import CLOSED, OPEN, LiveSessionPolicy
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.turn_timeline import TurnTimeline
from app.wake_events import CaptureConfig, WakeAudioCapture, WakeEventStore
from app.websocket_handler import WebSocketHandler

from tests.test_handler_wiring import FakeTransport


class FakeLiveService(FrameProcessor):
    """A pipeline-shaped stand-in with the on-demand session surface."""

    on_demand = True

    def __init__(self):
        super().__init__()
        self._register_event_handler("on_conversation_item_created")
        self.turn_timeline = None
        self.opens = 0
        self.closes = []
        self.resets = 0
        self.session_ready = False
        self.session_id = None
        self.session_expires_at = None
        self.session_seconds = 0.0
        self.response_active = False
        self._current_assistant_response = None
        # Lifecycle observers the gate installs.
        self.on_input_audio = None
        self.on_audible_output = None
        self.on_backend_activity = None
        self.on_reply_finished = None
        self.on_background_conversation = None
        self.on_discussion = None

        class Socket:
            async def ping(self_inner):
                loop = asyncio.get_running_loop()
                waiter = loop.create_future()
                waiter.set_result(0.0)
                return waiter

        self._websocket = Socket()

    async def open_session(self):
        self.opens += 1
        self.session_ready = True
        self.session_id = f"sess_{self.opens}"

    async def close_session(self, reason=""):
        self.closes.append(reason)
        self.session_ready = False

    async def reset_conversation(self):
        self.resets += 1

    # SessionControls surface used by the device handlers.
    async def send_client_event(self, event):
        pass

    async def discard_pending_input(self, reason=""):
        return False

    async def cancel_active_response(self, reason="", force=False):
        return False

    async def inject_context(self, text):
        pass

    def on_assistant_response_started(self, callback):
        pass

    @property
    def unsafe_to_replay_input(self):
        return False


class GateWiringCase(unittest.IsolatedAsyncioTestCase):
    POLICY = LiveSessionPolicy(idle_s=60.0, followup_s=60.0, max_silent_s=120.0,
                               max_session_s=600.0, tick_s=0.01)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = WakeEventStore(CaptureConfig(mode="metadata", probe_dir=self.tmp.name),
                               instance="w")
        self.handler = WebSocketHandler(wake_events=store)
        self.handler.WEDGE_TIMEOUT_S = 600
        self.refusals = []

        async def refused(connection, reason):
            self.refusals.append((connection.device_id, reason))

        self.handler.budget_refusal_speaker = refused
        self.connections = []

    async def asyncTearDown(self):
        for task in list(self.handler._background):
            task.cancel()
        for connection in self.connections:
            await self.handler._teardown(connection)
        self.tmp.cleanup()

    def build(self, device_id="kitchen", *, on_demand=True, policy=None, ledger=None):
        self.handler.live_session_policy = policy or self.POLICY
        self.handler.live_spend_ledger = ledger or LiveSpendLedger(path=None)
        serializer = RawAudioSerializer(device_id)
        connection = DeviceConnection(device_id=device_id, websocket=object(),
                                      serializer=serializer)
        connection.transport = FakeTransport()
        connection.turn_timeline = TurnTimeline(device_id)
        connection.wake_capture = WakeAudioCapture(self.handler.wake_events)
        serializer.add_audio_tap(connection.wake_capture)
        connection.turn_liveness = TurnLiveness()
        service = FakeLiveService()
        service.on_demand = on_demand
        connection.openai_service = service
        self.handler.build_pipeline(connection)
        self.connections.append(connection)
        return connection

    async def send(self, connection, obj):
        await connection.serializer.deserialize(json.dumps(obj))
        await asyncio.sleep(0.02)

    async def settle(self, predicate, timeout=2.0):
        async def _wait():
            while not predicate():
                await asyncio.sleep(0.002)
        await asyncio.wait_for(_wait(), timeout)


class TestGateIsInstalled(GateWiringCase):
    async def test_a_live_connection_gets_a_gate_and_opens_nothing(self):
        connection = self.build()
        self.assertIsNotNone(connection.session_gate)
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.opens, 0,
                         "building the pipeline must not buy a session")

    async def test_the_realtime_runtime_gets_no_gate(self):
        connection = self.build(on_demand=False)
        self.assertIsNone(connection.session_gate,
                          "Realtime sessions are billed per response; idle is free")

    async def test_without_a_policy_no_gate_is_installed(self):
        self.handler.live_session_policy = None
        connection = self.build()
        self.handler.live_session_policy = None
        self.assertIsNone(self.handler._build_session_gate(connection))

    async def test_the_lifecycle_observers_are_bound(self):
        connection = self.build()
        service, gate = connection.openai_service, connection.session_gate
        self.assertEqual(service.on_input_audio, gate.note_user_audio)
        self.assertEqual(service.on_audible_output, gate.note_audible_output)
        self.assertEqual(service.on_backend_activity, gate.note_backend_activity)
        self.assertEqual(service.on_reply_finished, gate.note_reply_finished)

    async def test_devices_share_one_spend_ledger(self):
        ledger = LiveSpendLedger(path=None)
        kitchen = self.build("kitchen", ledger=ledger)
        workshop = self.build("workshop", ledger=ledger)
        self.assertIs(kitchen.session_gate.ledger, workshop.session_gate.ledger,
                      "the dollar cap is a household cap")


class TestDeviceFramesDriveTheGate(GateWiringCase):
    async def test_a_wake_opens_exactly_one_session(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        self.assertEqual(connection.openai_service.opens, 1)

    async def test_several_wakes_in_a_row_open_one_session(self):
        connection = self.build()
        for i in range(5):
            await self.send(connection, {"type": "wake", "turn": f"k{i}", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await asyncio.sleep(0.05)
        self.assertEqual(connection.openai_service.opens, 1)

    async def test_mic_audio_opens_a_session_when_the_wake_frame_is_lost(self):
        """The backstop: a dropped wake message must not cost the user a turn."""
        connection = self.build()
        await connection.serializer.deserialize(b"\x00\x00" * 160)
        await self.settle(lambda: connection.session_gate.state == OPEN)
        self.assertEqual(connection.openai_service.opens, 1)

    async def test_continuing_mic_audio_does_not_reopen_anything(self):
        connection = self.build()
        for _ in range(50):
            await connection.serializer.deserialize(b"\x00\x00" * 160)
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await asyncio.sleep(0.05)
        self.assertEqual(connection.openai_service.opens, 1)

    async def test_our_own_announcement_audio_opens_no_session(self):
        """Echo audio is dropped before it can look like a turn."""
        import time
        connection = self.build()
        connection.serializer.suppress_inbound_until = time.monotonic() + 1.0
        for _ in range(20):
            await connection.serializer.deserialize(b"\x00\x00" * 160)
        await asyncio.sleep(0.05)
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.opens, 0)

    async def test_a_stop_closes_the_session(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.send(connection, {"type": "interrupt"})
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.closes, ["device stop"])

    async def test_a_follow_up_cut_off_closes_the_session(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.send(connection, {"type": "flush"})
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.closes, ["follow-up cut-off"])

    async def test_every_gate_close_forces_the_device_idle(self):
        """Regression: model session closed while Kitchen's ring kept spinning."""
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        connection.phase_emitter._current = "replying"
        phases = []

        async def capture(value, **extra):
            phases.append((value, extra))

        connection.phase_emitter._send_phase = capture
        connection.last_wake_mono = 0.0  # the wake was long ago
        await connection.session_gate.close("idle")
        await self.settle(lambda: bool(phases))
        self.assertEqual(phases[-1], ("idle", {}))

    async def test_a_close_right_after_a_new_wake_does_not_force_idle(self):
        """Rig 2026-10-16: a wake landed 1 ms after a session closed; the
        close's forced idle followed it and stopped the new turn's mic."""
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        connection.phase_emitter._current = "replying"
        phases = []

        async def capture(value, **extra):
            phases.append((value, extra))

        connection.phase_emitter._send_phase = capture
        import time as _time
        connection.last_wake_mono = _time.monotonic()  # the next turn's wake, just now
        await connection.session_gate.close("follow-up cut-off")
        await asyncio.sleep(0.05)
        self.assertNotIn(("idle", {}), phases)

    async def test_a_new_wake_after_a_stop_opens_a_fresh_session(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await self.send(connection, {"type": "interrupt"})
        await self.send(connection, {"type": "wake", "turn": "k2", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        self.assertEqual(connection.openai_service.opens, 2)
        self.assertEqual(connection.session_gate.generation, 2)

    async def test_after_a_background_conversation_only_a_wake_reopens(self):
        """Rig 2026-10-16: the follow-up window streamed the same room talk
        into a fresh session 3 s after a background-conversation close."""
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        await connection.openai_service.on_background_conversation()
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.closes, ["background conversation"])
        await asyncio.sleep(0.6)  # past the mic-open gap
        for _ in range(20):
            await connection.serializer.deserialize(b"\x00\x00" * 160)
        await asyncio.sleep(0.05)
        self.assertEqual(connection.session_gate.state, CLOSED, "mic audio alone reopens nothing")
        await self.send(connection, {"type": "wake", "turn": "k2", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        self.assertEqual(connection.openai_service.opens, 2)

    async def test_a_discussion_raises_the_bounds_and_the_device_follow_up(self):
        """Discussions by voice: the gate runs on discussion bounds and the
        device keeps listening longer after each reply, until the close."""
        connection = self.build(policy=LiveSessionPolicy(
            idle_s=10.0, followup_s=8.0, max_silent_s=45.0, max_session_s=90.0, tick_s=0.01),
            ledger=LiveSpendLedger(path=None, max_session_minutes_per_day=60.0, max_usd_per_day=10.0))
        sent = []

        async def capture(obj):
            sent.append(obj)
            return True

        connection.send_json = capture
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        connection.openai_service.on_discussion()
        await asyncio.sleep(0.02)
        self.assertTrue(connection.session_gate.discussion_active)
        self.assertGreater(connection.session_gate.policy.max_session_s, 90.0)
        hellos = [m for m in sent if m.get("type") == "hello"]
        self.assertEqual(hellos[-1]["follow_up_ms"], 10000)
        await connection.session_gate.close("test")
        await asyncio.sleep(0.02)
        hellos = [m for m in sent if m.get("type") == "hello"]
        self.assertEqual(hellos[-1]["follow_up_ms"], self.handler.follow_up_ms, "restored at close")
        self.assertEqual(connection.session_gate.policy.max_session_s, 90.0)

    async def test_a_self_wake_during_an_announcement_opens_nothing(self):
        """The two repairs compose: an echo wake buys no session either."""
        import time
        connection = self.build()
        connection.serializer.suppress_inbound_until = time.monotonic() + 1.0
        await self.send(connection, {"type": "wake", "turn": "echo", "src": "wake_word"})
        await asyncio.sleep(0.05)
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(connection.openai_service.opens, 0)

    async def test_teardown_closes_the_session_and_charges_it(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        gate, service = connection.session_gate, connection.openai_service
        await self.handler._teardown(connection)
        self.connections.remove(connection)
        self.assertEqual(service.closes, ["device disconnected"])
        self.assertEqual(gate.state, CLOSED)
        self.assertEqual(gate.ledger.sessions_total, 1)
        self.assertIsNone(connection.session_gate)

    async def test_a_wake_after_teardown_opens_nothing(self):
        connection = self.build()
        gate, service = connection.session_gate, connection.openai_service
        await self.handler._teardown(connection)
        self.connections.remove(connection)
        self.assertFalse(await gate.open("late wake"))
        self.assertEqual(service.opens, 0)


class TestRecoveryDoesNotReopenIdleSessions(GateWiringCase):
    async def error(self, connection, message):
        await connection.recovery.process_frame(
            ErrorFrame(error=message), FrameDirection.UPSTREAM
        )
        await asyncio.sleep(0.05)

    DEAD = "Error sending client event: sent 1011 (internal error) keepalive ping timeout"

    async def test_no_proactive_refresh_loop_runs_under_a_gate(self):
        connection = self.build()
        await connection.recovery.process_frame(
            ErrorFrame(error="harmless"), FrameDirection.UPSTREAM
        )
        self.assertIsNone(connection.recovery._refresh_task,
                          "a 55-minute refresh would reconnect during quiet")

    async def test_a_realtime_connection_still_gets_its_refresh_loop(self):
        connection = self.build(on_demand=False)
        await connection.recovery.process_frame(
            ErrorFrame(error="harmless"), FrameDirection.UPSTREAM
        )
        self.assertIsNotNone(connection.recovery._refresh_task)

    async def test_a_dead_socket_while_idle_closes_instead_of_reconnecting(self):
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        connection.session_gate.note_turn_resolved()
        await self.error(connection, self.DEAD)
        await self.settle(lambda: connection.session_gate.state == CLOSED)
        self.assertEqual(connection.openai_service.resets, 0, "no new session was bought")
        self.assertEqual(connection.openai_service.closes, ["connection lost while idle"])

    async def test_an_error_flood_while_idle_does_not_loop(self):
        connection = self.build()
        for _ in range(30):
            await self.error(connection, self.DEAD)
        self.assertEqual(connection.openai_service.opens, 0)
        self.assertEqual(connection.openai_service.resets, 0)

    async def test_a_drop_with_a_turn_in_flight_is_still_recovered(self):
        """Cost safety must not cost the user an answer they are waiting for."""
        connection = self.build()
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: connection.session_gate.state == OPEN)
        self.assertTrue(connection.session_gate.turn_in_flight)
        await self.error(connection, self.DEAD)
        await self.settle(lambda: connection.openai_service.opens == 2)
        self.assertEqual(connection.openai_service.resets, 0)
        self.assertEqual(connection.session_gate.state, OPEN)

    async def test_a_liveness_probe_never_opens_a_session(self):
        connection = self.build()
        connection.openai_service._websocket = None   # no socket: no pong
        await connection.recovery.force_reconnect("liveness: no pong at wake")
        self.assertEqual(connection.openai_service.resets, 0)
        self.assertEqual(connection.session_gate.state, CLOSED)


class TestBudgetRefusalReachesTheDevice(GateWiringCase):
    async def test_an_exhausted_budget_refuses_the_turn_out_loud(self):
        ledger = LiveSpendLedger(path=None, max_session_minutes_per_day=0.001,
                                 max_usd_per_day=0.0)
        from app.live_cost import SessionCharge
        import time
        ledger.record(SessionCharge(device_id="kitchen", session_id="s", seconds=60.0,
                                    reason="idle", closed_at=time.time()))
        connection = self.build(ledger=ledger)
        await self.send(connection, {"type": "wake", "turn": "k1", "src": "wake_word"})
        await self.settle(lambda: self.refusals)
        self.assertEqual(connection.openai_service.opens, 0)
        self.assertEqual(connection.session_gate.state, CLOSED)
        self.assertEqual(self.refusals[0][0], "kitchen")
        self.assertIn("cutoff reached", self.refusals[0][1])


if __name__ == "__main__":
    unittest.main()
