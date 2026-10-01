"""Verify connection teardown stops ConnectionRecovery background work."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.device_registry import DeviceConnection
from app.main import SafeRealtimeLLMService
from app.phase_emitter import PhaseEmitter
from app.websocket_handler import ConnectionRecovery, WebSocketHandler
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService


async def main():
    # force_idle is a physical-device recovery command, not merely a logical
    # phase transition. A wake can make the ring spin locally while the
    # emitter's cached phase remains idle (half-open OpenAI socket), so a
    # repeated idle must still be sent to release the device.
    forced_phases = []

    async def capture_phase(value):
        forced_phases.append(value)

    forced_emitter = PhaseEmitter(capture_phase)
    await forced_emitter._emit("idle")
    await forced_emitter.force_idle("test half-open wake")
    assert forced_phases == ["idle", "idle"]

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

    assert refresh_task.cancelled()
    assert recover_task.cancelled()
    assert phase_task.cancelled()
    assert connection.recovery is None
    assert connection.phase_emitter is None

    class FakeRecovery:
        def __init__(self):
            self.reasons = []

        async def force_reconnect(self, reason):
            self.reasons.append(reason)

    handler = WebSocketHandler()
    handler.WEDGE_TIMEOUT_S = 0
    wedge_recovery = FakeRecovery()
    wedge_connection = DeviceConnection("kitchen", object(), recovery=wedge_recovery)
    wedge_phase = PhaseEmitter(None)
    await handler._wedge_check(wedge_connection, wedge_phase, 1.0)
    assert wedge_recovery.reasons == ["wedge: silent after wake"]

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingService:
        async def reset_conversation(self):
            started.set()
            await release.wait()

    live_recovery = ConnectionRecovery(BlockingService())
    live_connection = DeviceConnection("office", object(), recovery=live_recovery)
    wedge_task = asyncio.create_task(
        handler._wedge_check(live_connection, PhaseEmitter(None), 1.0)
    )
    await started.wait()
    await handler._teardown(live_connection)
    try:
        await wedge_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("wedge recovery survived connection teardown")

    # reset_conversation deliberately closes the old receive task. That must
    # not emit a second connection-death ErrorFrame into a stopped processor.
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
    assert errors == []

    # A reconnect creates a fresh OpenAI conversation, so historical tool
    # results in Pipecat's retained context must be marked as already sent.
    # Replaying one causes invalid_tool_call_id and suppresses the next spoken
    # acknowledgement even though the Home Assistant action succeeded.
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
    with patch.object(
        OpenAIRealtimeLLMService, "reset_conversation", new=AsyncMock()
    ):
        await reconnecting.reset_conversation()
    assert reconnecting._completed_tool_calls == {"call_old"}
    assert reconnecting._run_llm_when_api_session_ready is False
    assert reconnecting._llm_needs_conversation_setup is False
    print("ALL ASSERTIONS PASSED")


asyncio.run(main())
