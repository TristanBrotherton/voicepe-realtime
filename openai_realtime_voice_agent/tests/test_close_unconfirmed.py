"""A close nobody confirmed: charged in full, a short hold, never a day-long
"usage limit", and never a socket left open behind it."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import main as main_module
from app.live_cost import CLOSE_UNCONFIRMED_HOLD_S, LiveSpendLedger, is_close_unconfirmed
from app.live_session_gate import LiveSessionGate
from app.spoken_prompts import PROMPTS
from tests.live_harness import LiveHarness
from tests.test_live_session_gate import Clock, FakeLiveService, GateCase


class FailingCloseService(FakeLiveService):
    def __init__(self, released=False, **kw):
        super().__init__(**kw)
        self.socket_released = released

    async def close_session(self, reason=""):
        await super().close_session(reason)
        raise RuntimeError("synthetic close failure")


class GateHold(GateCase):
    async def test_unconfirmed_close_holds_then_recovers_after_the_hold(self):
        gate = self.make(service=FailingCloseService(released=False),
                         max_session_minutes_per_day=0, max_usd_per_day=0)
        self.assertTrue(await gate.open("wake"))
        reserved = gate._reserved_seconds
        await gate.close("idle")
        self.assertGreaterEqual(self.charges[-1].seconds, reserved, "charged in full")
        self.assertFalse(await gate.open("next wake"))
        self.assertTrue(is_close_unconfirmed(self.refusals[-1]))
        self.assertEqual(self.ledger.persistence_error, "")
        self.wall.advance(CLOSE_UNCONFIRMED_HOLD_S + 1)
        self.assertTrue(await gate.open("later wake"))
        await gate.shutdown()

    async def test_hold_lifts_once_the_socket_is_shown_closed(self):
        service = FailingCloseService(released=False)
        gate = self.make(service=service)
        self.assertTrue(await gate.open("wake"))
        await gate.close("idle")
        self.assertTrue(is_close_unconfirmed(self.ledger.refusal))
        service.socket_released = True
        self.assertTrue(await gate.open("next wake"))
        self.assertEqual(self.ledger.refusal, "")
        await gate.shutdown()

    async def test_failed_close_with_a_closed_socket_does_not_hold(self):
        gate = self.make(service=FailingCloseService(released=True))
        self.assertTrue(await gate.open("wake"))
        reserved = gate._reserved_seconds
        await gate.close("idle")
        self.assertGreaterEqual(self.charges[-1].seconds, reserved)
        self.assertEqual(self.ledger.refusal, "")
        self.assertTrue(await gate.open("next wake"))
        await gate.shutdown()

    async def test_another_gate_cannot_lift_a_hold_it_did_not_set(self):
        failing = FailingCloseService(released=False)
        gate = self.make(service=failing)
        other = LiveSessionGate(FailingCloseService(released=True), ledger=self.ledger,
                                monotonic=self.clock, wall_clock=self.wall)
        self.assertTrue(await gate.open("wake"))
        await gate.close("idle")
        self.assertFalse(await other.open("wake elsewhere"))
        await gate.shutdown()
        await other.shutdown()

    async def test_ledger_file_failure_still_latches(self):
        gate = self.make()
        self.ledger.persistence_error = "Live spend persistence failed: OSError"
        self.wall.advance(CLOSE_UNCONFIRMED_HOLD_S * 10)
        self.assertFalse(await gate.open("wake"))
        self.assertFalse(is_close_unconfirmed(self.refusals[-1]))


class FakeTransport:
    def __init__(self):
        self.aborted = False

    def is_closing(self):
        return self.aborted

    def abort(self):
        self.aborted = True


class ServiceCloseBounds(unittest.IsolatedAsyncioTestCase):
    async def _open(self, h):
        await h.open_session()
        await h.start_session()
        h.socket.transport = FakeTransport()

    async def test_hanging_socket_close_is_aborted_within_bounds(self):
        async with LiveHarness(self, on_demand=True) as h:
            await self._open(h)
            socket = h.socket

            async def hang():
                await asyncio.Event().wait()
            socket.close = hang
            with patch("app.live_service.SESSION_CLOSE_TIMEOUT_S", .01), \
                    patch("app.live_service.SOCKET_CLOSE_TIMEOUT_S", .05):
                await asyncio.wait_for(h.service.close_session("idle"), 1)
            self.assertTrue(socket.transport.aborted)
            self.assertTrue(h.service.socket_released)
            self.assertIsNone(h.service._receive_task)

    async def test_hanging_close_send_is_bounded(self):
        async with LiveHarness(self, on_demand=True) as h:
            await self._open(h)
            socket = h.socket
            original = socket.send

            async def send(data):
                if '"session.close"' in data:
                    await asyncio.Event().wait()
                await original(data)
            socket.send = send
            with patch("app.live_service.CLOSE_SEND_TIMEOUT_S", .05):
                await asyncio.wait_for(h.service.close_session("idle"), 1)
            self.assertTrue(h.service.socket_released)

    async def test_cancel_during_socket_close_leaves_no_session_reader(self):
        """#43: the gate's limit cancelling the close inside socket.close()
        must still abort the connection and stop the receive task."""
        async with LiveHarness(self, on_demand=True) as h:
            await self._open(h)
            socket = h.socket
            closing = asyncio.Event()

            async def hang():
                closing.set()
                await asyncio.Event().wait()
            socket.close = hang
            receive = h.service._receive_task
            with patch("app.live_service.SESSION_CLOSE_TIMEOUT_S", .01):
                task = asyncio.create_task(h.service.close_session("idle"))
                await asyncio.wait_for(closing.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(socket.transport.aborted)
            self.assertIsNone(h.service._websocket)
            self.assertIsNone(h.service._receive_task)
            self.assertTrue(receive.done())
            self.assertTrue(h.service.socket_released)

    async def test_gate_reopens_at_once_after_an_aborted_close(self):
        async with LiveHarness(self, on_demand=True) as h:
            ledger = LiveSpendLedger(path=None)
            gate = LiveSessionGate(h.service, ledger=ledger)
            await gate.open("wake")
            await h.start_session()
            h.socket.transport = FakeTransport()

            async def fail():
                raise OSError("synthetic close failure")
            h.socket.close = fail
            with patch("app.live_service.SESSION_CLOSE_TIMEOUT_S", .001):
                await gate.close("idle")
            self.assertEqual(ledger.refusal, "")
            self.assertTrue(await gate.open("next wake"))
            await gate.shutdown()

    async def test_connects_bound_the_closing_handshake(self):
        seen = []

        async def fake_connect(uri, additional_headers=None, **kwargs):
            seen.append(kwargs)
            raise OSError("offline")
        async with LiveHarness(self, on_demand=True) as h:
            with patch("app.live_service.websocket_connect", fake_connect):
                with self.assertRaises(OSError):
                    await h.service.open_session()
        from app.live_service import SOCKET_CLOSE_TIMEOUT_S
        self.assertEqual(seen[0]["close_timeout"], SOCKET_CLOSE_TIMEOUT_S)


class SpokenRefusal(unittest.IsolatedAsyncioTestCase):
    async def _say(self, reason):
        said = []

        class Prompts:
            async def say(self, key, device_id):
                said.append(key)
                return True
        app = SimpleNamespace(speak_errors=True, spoken_prompts=Prompts())
        connection = SimpleNamespace(device_id="dev", phase_emitter=None)
        await main_module.Application._speak_budget_refusal(app, connection, reason)
        return said

    async def test_close_hold_is_not_a_usage_limit(self):
        ledger = LiveSpendLedger(path=None)
        ledger.hold_after_unconfirmed_close("close failed")
        self.assertEqual(await self._say(ledger.refusal), ["error_close_pending"])
        self.assertNotIn("limit", PROMPTS["error_close_pending"])

    async def test_cap_is_still_a_usage_limit(self):
        ledger = LiveSpendLedger(path=None, max_session_minutes_per_day=0.0001, max_usd_per_day=0)
        verdict = ledger.check(open_seconds=60)
        self.assertEqual(await self._say(verdict.reason), ["error_budget"])


if __name__ == "__main__":
    unittest.main()
