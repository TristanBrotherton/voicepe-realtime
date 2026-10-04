"""ConnectionRecovery teardown, wedge handling and reconnect bookkeeping."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.device_registry import DeviceConnection
from app.main import SafeRealtimeLLMService
from app.phase_emitter import PhaseEmitter
from app.websocket_handler import ConnectionRecovery, WebSocketHandler


class TestForcedIdle(unittest.IsolatedAsyncioTestCase):
    async def test_force_idle_retransmits_even_when_cached_idle(self):
        # A wake can make the ring spin locally while the emitter's cached
        # phase is still idle (half-open OpenAI socket), so a repeated idle
        # must still reach the device.
        phases = []

        async def capture(value):
            phases.append(value)

        emitter = PhaseEmitter(capture)
        await emitter._emit("idle")
        await emitter.force_idle("test half-open wake")
        self.assertEqual(phases, ["idle", "idle"])


class TestTeardown(unittest.IsolatedAsyncioTestCase):
    async def test_teardown_cancels_background_tasks(self):
        recovery = ConnectionRecovery(object())
        refresh_task = asyncio.create_task(asyncio.Event().wait())
        recover_task = asyncio.create_task(asyncio.Event().wait())
        recovery._refresh_task = refresh_task
        recovery._recover_task = recover_task
        phase_emitter = PhaseEmitter(None)
        phase_task = asyncio.create_task(asyncio.Event().wait())
        phase_emitter._idle_task = phase_task

        connection = DeviceConnection(
            "kitchen", object(), recovery=recovery, phase_emitter=phase_emitter
        )
        await WebSocketHandler()._teardown(connection)

        self.assertTrue(refresh_task.cancelled())
        self.assertTrue(recover_task.cancelled())
        self.assertTrue(phase_task.cancelled())
        self.assertIsNone(connection.recovery)
        self.assertIsNone(connection.phase_emitter)

    async def test_wedge_recovery_does_not_survive_teardown(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingService:
            async def reset_conversation(self):
                started.set()
                await release.wait()

        handler = WebSocketHandler()
        handler.WEDGE_TIMEOUT_S = 0
        live_recovery = ConnectionRecovery(BlockingService())
        live_connection = DeviceConnection("office", object(), recovery=live_recovery)
        wedge_task = asyncio.create_task(
            handler._wedge_check(live_connection, PhaseEmitter(None), 1.0)
        )
        await asyncio.wait_for(started.wait(), 2)
        await handler._teardown(live_connection)
        with self.assertRaises(asyncio.CancelledError):
            await wedge_task


class TestWedgeCheck(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_wake_forces_reconnect(self):
        class FakeRecovery:
            def __init__(self):
                self.reasons = []

            async def force_reconnect(self, reason):
                self.reasons.append(reason)

        handler = WebSocketHandler()
        handler.WEDGE_TIMEOUT_S = 0
        recovery = FakeRecovery()
        connection = DeviceConnection("kitchen", object(), recovery=recovery)
        await handler._wedge_check(connection, PhaseEmitter(None), 1.0)
        self.assertEqual(recovery.reasons, ["wedge: silent after wake"])


class TestReconnectBookkeeping(unittest.IsolatedAsyncioTestCase):
    async def test_intentional_reset_does_not_report_reader_death(self):
        # reset_conversation deliberately closes the old receive task. That
        # must not emit a second connection-death ErrorFrame.
        service = object.__new__(SafeRealtimeLLMService)
        service._resetting_conversation = True
        errors = []

        async def receive_ended(_service):
            return None

        async def push_error(**kwargs):
            errors.append(kwargs)

        service.push_error = push_error
        with patch.object(OpenAIRealtimeLLMService, "_receive_task_handler", receive_ended):
            await service._receive_task_handler()
        self.assertEqual(errors, [])

    async def test_unexpected_reader_end_reports_death(self):
        service = object.__new__(SafeRealtimeLLMService)
        service._resetting_conversation = False
        errors = []

        async def receive_ended(_service):
            return None

        async def push_error(**kwargs):
            errors.append(kwargs)

        service.push_error = push_error
        with patch.object(OpenAIRealtimeLLMService, "_receive_task_handler", receive_ended):
            await service._receive_task_handler()
        self.assertEqual(len(errors), 1)
        self.assertIn("realtime receive loop", errors[0]["error_msg"])

    async def test_reconnect_marks_historical_tool_results_sent(self):
        # A reconnect creates a fresh OpenAI conversation, so historical tool
        # results in Pipecat's retained context must be marked as already
        # sent; replaying one causes invalid_tool_call_id.
        class FakeContext:
            def get_messages(self):
                return [
                    {"role": "tool", "tool_call_id": "call_old", "content": "done"},
                    {"role": "assistant", "content": "acknowledged"},
                ]

        reconnecting = object.__new__(SafeRealtimeLLMService)
        reconnecting._context = FakeContext()
        reconnecting._completed_tool_calls = set()
        reconnecting._run_llm_when_api_session_ready = True
        reconnecting._llm_needs_conversation_setup = True
        with patch.object(OpenAIRealtimeLLMService, "reset_conversation", new=AsyncMock()):
            await reconnecting.reset_conversation()
        self.assertEqual(reconnecting._completed_tool_calls, {"call_old"})
        self.assertFalse(reconnecting._run_llm_when_api_session_ready)
        self.assertFalse(reconnecting._llm_needs_conversation_setup)


if __name__ == "__main__":
    unittest.main()
