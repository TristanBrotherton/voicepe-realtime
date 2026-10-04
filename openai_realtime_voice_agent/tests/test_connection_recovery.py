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


class FakeRecovery:
    def __init__(self, alive):
        self.alive = alive
        self.reasons = []
        self.reconnecting = False

    async def probe_liveness(self, timeout_s=1.5):
        return self.alive

    async def force_reconnect(self, reason, unstick=True):
        self.reasons.append((reason, unstick))


class TestWedgeCheck(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_wake_on_dead_socket_reconnects(self):
        handler = WebSocketHandler()
        handler.WEDGE_TIMEOUT_S = 0
        recovery = FakeRecovery(alive=False)
        connection = DeviceConnection("kitchen", object(), recovery=recovery)
        await handler._wedge_check(connection, PhaseEmitter(None), 1.0)
        self.assertEqual(recovery.reasons, [("wedge: silent after wake", True)])

    async def test_quiet_wake_on_live_socket_closes_turn_without_reconnect(self):
        # A silent or false wake used to force a reconnect, making the next
        # turn cold. With a live socket it now only ends the turn.
        phases = []

        async def capture(value, **extra):
            phases.append(value)

        handler = WebSocketHandler()
        handler.WEDGE_TIMEOUT_S = 0
        recovery = FakeRecovery(alive=True)
        connection = DeviceConnection("kitchen", object(), recovery=recovery)
        await handler._wedge_check(connection, PhaseEmitter(capture), 1.0)
        self.assertEqual(recovery.reasons, [])
        self.assertEqual(phases, ["idle"])

    async def test_speech_after_wake_skips_the_check(self):
        handler = WebSocketHandler()
        handler.WEDGE_TIMEOUT_S = 0
        recovery = FakeRecovery(alive=False)
        emitter = PhaseEmitter(None)
        emitter.last_vad_mono = 5.0
        connection = DeviceConnection("kitchen", object(), recovery=recovery)
        await handler._wedge_check(connection, emitter, 1.0)
        self.assertEqual(recovery.reasons, [])

    async def test_wake_liveness_reconnects_dead_socket_without_unsticking(self):
        handler = WebSocketHandler()
        recovery = FakeRecovery(alive=False)
        connection = DeviceConnection("kitchen", object(), recovery=recovery)
        await handler._wake_liveness(connection)
        # unstick=False: the device keeps its mic open; the request replays.
        self.assertEqual(recovery.reasons, [("liveness: no pong at wake", False)])

    async def test_wake_liveness_leaves_live_socket_alone(self):
        handler = WebSocketHandler()
        recovery = FakeRecovery(alive=True)
        connection = DeviceConnection("kitchen", object(), recovery=recovery)
        await handler._wake_liveness(connection)
        self.assertEqual(recovery.reasons, [])


class TestLivenessProbe(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_websocket_ping(self):
        class Socket:
            def __init__(self, answer):
                self.answer = answer

            async def ping(self):
                loop = asyncio.get_running_loop()
                waiter = loop.create_future()
                if self.answer:
                    waiter.set_result(0.01)
                return waiter

        class Service:
            _websocket = None

        service = Service()
        recovery = ConnectionRecovery(service)
        self.assertFalse(await recovery.probe_liveness(0.05), "no socket = not alive")
        service._websocket = Socket(answer=True)
        self.assertTrue(await recovery.probe_liveness(0.05))
        service._websocket = Socket(answer=False)
        self.assertFalse(await recovery.probe_liveness(0.05), "half-open socket never pongs")


class TestReplayOnReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_unanswered_request_is_replayed_after_reset(self):
        from app.input_replay import InputReplayBuffer
        from pipecat.frames.frames import InputAudioRawFrame

        pushed = []

        class Replay(InputReplayBuffer):
            async def push_frame(self, frame, direction=None):
                pushed.append(frame)

        replay = Replay()
        frames = [InputAudioRawFrame(audio=bytes([i]) * 480, sample_rate=24000, num_channels=1)
                  for i in range(3)]
        for frame in frames[:2]:
            replay._record(frame)

        class Service:
            async def reset_conversation(self):
                # A frame arrives mid-reconnect: held, not dropped.
                replay._record(frames[2])
                replay._held.append(frames[2])

        recovery = ConnectionRecovery(Service(), replay=replay, should_replay=lambda: True)
        recovery._reconnecting = True
        await recovery._recover("test", unstick=False)
        self.assertEqual(pushed, frames, "each frame replayed exactly once, in order")
        self.assertFalse(replay.holding)

    async def test_answered_request_only_releases_held_frames(self):
        from app.input_replay import InputReplayBuffer
        from pipecat.frames.frames import InputAudioRawFrame

        pushed = []

        class Replay(InputReplayBuffer):
            async def push_frame(self, frame, direction=None):
                pushed.append(frame)

        replay = Replay()
        old = InputAudioRawFrame(audio=b"\x01" * 480, sample_rate=24000, num_channels=1)
        new = InputAudioRawFrame(audio=b"\x02" * 480, sample_rate=24000, num_channels=1)
        replay._record(old)

        class Service:
            async def reset_conversation(self):
                replay._record(new)
                replay._held.append(new)

        recovery = ConnectionRecovery(Service(), replay=replay, should_replay=lambda: False)
        await recovery._recover("test")
        self.assertEqual(pushed, [new])

    async def test_failed_reconnect_still_releases_hold(self):
        from app.input_replay import InputReplayBuffer

        replay = InputReplayBuffer()

        class Service:
            async def reset_conversation(self):
                raise RuntimeError("network down")

        recovery = ConnectionRecovery(Service(), replay=replay, should_replay=lambda: True)
        await recovery._recover("test", unstick=False)
        self.assertFalse(replay.holding, "a failed reconnect must not leave the mic held forever")


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
