"""The provider-neutral session controls the device handler uses.

The Realtime adapter must send exactly the client events the handler used to
send inline; the GPT-Live service must be used directly; ConnectionRecovery
must treat either runtime's reader death as a reconnect trigger.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import ErrorFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.session_controls import RealtimeSessionControls, controls_for
from app.websocket_handler import ConnectionRecovery


class FakeRealtime(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._register_event_handler("on_conversation_item_created")
        self.events = []
        self._current_assistant_response = None

    async def send_client_event(self, event):
        self.events.append(event)


class TestRealtimeAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_is_chosen_for_services_without_controls(self):
        service = FakeRealtime()
        controls = controls_for(service)
        self.assertIsInstance(controls, RealtimeSessionControls)
        self.assertIs(controls.service, service)

    async def test_discard_sends_input_buffer_clear(self):
        service = FakeRealtime()
        self.assertTrue(await controls_for(service).discard_pending_input("x"))
        self.assertEqual([type(e).__name__ for e in service.events], ["InputAudioBufferClearEvent"])

    async def test_cancel_only_while_a_response_is_active_unless_forced(self):
        service = FakeRealtime()
        controls = controls_for(service)
        self.assertFalse(controls.response_active)
        self.assertFalse(await controls.cancel_active_response("stop"))
        self.assertEqual(service.events, [])
        service._current_assistant_response = object()
        self.assertTrue(controls.response_active)
        self.assertTrue(await controls.cancel_active_response("stop"))
        service._current_assistant_response = None
        self.assertTrue(await controls.cancel_active_response("racing", force=True))
        self.assertEqual([type(e).__name__ for e in service.events],
                         ["ResponseCancelEvent", "ResponseCancelEvent"])

    async def test_inject_context_is_a_system_item(self):
        service = FakeRealtime()
        await controls_for(service).inject_context("[voice check] matches Alex")
        event = service.events[0]
        self.assertEqual(type(event).__name__, "ConversationItemCreateEvent")
        self.assertEqual(event.item.role, "system")
        self.assertEqual(event.item.content[0].text, "[voice check] matches Alex")

    async def test_assistant_item_callback_filters_roles(self):
        service = FakeRealtime()
        fired = []

        async def cb():
            fired.append(1)

        controls_for(service).on_assistant_response_started(cb)
        await service._call_event_handler("on_conversation_item_created", "i1", type("I", (), {"role": "user"})())
        await service._call_event_handler("on_conversation_item_created", "i2", type("I", (), {"role": "assistant"})())
        await asyncio.sleep(0.01)  # pipecat runs async event handlers as tasks
        self.assertEqual(fired, [1])


class TestLiveServiceIsItsOwnControls(unittest.TestCase):
    def test_live_service_implements_the_protocol(self):
        from app.live_service import OpenAILiveLLMService
        from app.voice_runtime import LiveConfig

        service = OpenAILiveLLMService(api_key="k", config=LiveConfig(), instructions="i",
                                       backend_instructions="b", tools=[])
        self.assertIs(controls_for(service), service)


class TestRecoveryTreatsEitherReaderDeathAlike(unittest.IsolatedAsyncioTestCase):
    async def test_live_reader_death_triggers_reconnect(self):
        resets = []

        class Service:
            _current_assistant_response = None

            async def reset_conversation(self):
                resets.append(1)

        recovery = ConnectionRecovery(Service())
        recovery.push_frame = AsyncMock()
        with patch.object(FrameProcessor, "process_frame", new=AsyncMock()):
            await recovery.process_frame(
                ErrorFrame("OpenAILiveLLMService error: live receive loop died: ConnectionClosed"),
                FrameDirection.UPSTREAM,
            )
        await asyncio.wait_for(recovery._recover_task, 2)
        self.assertEqual(resets, [1])
        await recovery.close()


if __name__ == "__main__":
    unittest.main()
