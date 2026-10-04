"""SafeRealtimeLLMService hooks: socket observer, timeline stamps, silent responses."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.main import SafeRealtimeLLMService
from app.realtime_observer import ObservedSocket, extract_event_type
from app.turn_timeline import TurnTimeline


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for message in self.messages:
            yield message

    async def close(self):
        self.closed = True


class TestObservedSocket(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_types_are_dropped_and_observed(self):
        seen = []
        socket = FakeSocket([
            '{"type":"response.created","event_id":"e1"}',
            '{"type":"brand.new.event","event_id":"e2"}',
            '{"event_id":"e3","type":"response.done","response":{"type":"x"}}',
        ])
        observed = ObservedSocket(socket, on_event=lambda t, m: seen.append(t),
                                  known_types={"response.created", "response.done"})
        passed = [m async for m in observed]
        self.assertEqual(seen, ["response.created", "brand.new.event", "response.done"])
        self.assertEqual(len(passed), 2)
        self.assertEqual(observed.events_seen, 3)
        await observed.close()
        self.assertTrue(socket.closed, "attributes fall through to the real socket")

    async def test_observer_errors_never_break_reading(self):
        def boom(_t, _m):
            raise RuntimeError("observer bug")

        observed = ObservedSocket(FakeSocket(['{"type":"a"}']), on_event=boom)
        self.assertEqual([m async for m in observed], ['{"type":"a"}'])

    def test_extract_event_type(self):
        self.assertEqual(extract_event_type('{"type":"input_audio_buffer.cleared"}'), "input_audio_buffer.cleared")
        self.assertEqual(extract_event_type(b'{"event_id":"x","type":"error"}'), "error")
        self.assertEqual(extract_event_type("not json"), "")


def make_service():
    service = object.__new__(SafeRealtimeLLMService)
    service.turn_timeline = TurnTimeline("kitchen")
    return service


class TestTimelineHooks(unittest.IsolatedAsyncioTestCase):
    async def test_speech_stopped_measures_vad_endpoint_delay(self):
        service = make_service()
        service.turn_timeline.begin("t1")
        with patch.object(OpenAIRealtimeLLMService, "_send_user_audio", new=AsyncMock()), \
             patch.object(OpenAIRealtimeLLMService, "_handle_evt_speech_started", new=AsyncMock()), \
             patch.object(OpenAIRealtimeLLMService, "_handle_evt_speech_stopped", new=AsyncMock()):
            # 2.0 s of 24 kHz PCM16 appended.
            for _ in range(20):
                await service._send_user_audio(SimpleNamespace(audio=b"\x00" * 4800))
            await service._handle_evt_speech_started(SimpleNamespace(audio_start_ms=200))
            await service._handle_evt_speech_stopped(SimpleNamespace(audio_end_ms=1350))
        turn = service.turn_timeline.current
        self.assertEqual(turn.vad_endpoint_delay_ms, 650)
        self.assertEqual(turn.speech_audio_ms, 1150)
        self.assertIn("speech_stopped", turn.stamps)

    async def test_first_audio_delta_per_response_marks_and_notifies_once(self):
        service = make_service()
        service.turn_timeline.begin("t1")
        answered = []
        service.on_response_audio = lambda: answered.append(1)
        with patch.object(OpenAIRealtimeLLMService, "_handle_evt_audio_delta", new=AsyncMock()):
            for _ in range(3):
                await service._handle_evt_audio_delta(SimpleNamespace(response_id="r1"))
        self.assertIn("first_model_audio", service.turn_timeline.current.stamps)
        self.assertEqual(answered, [1])

    async def test_response_created_is_stamped_from_socket_events(self):
        service = make_service()
        service.turn_timeline.begin("t1")
        service._observe_server_event("response.created", "{}")
        self.assertIn("response_created", service.turn_timeline.current.stamps)

    async def test_reconnect_resets_the_input_audio_clock(self):
        service = make_service()
        service._appended_audio_ms = 5000.0
        service._context = None
        service._completed_tool_calls = set()
        with patch.object(OpenAIRealtimeLLMService, "reset_conversation", new=AsyncMock()):
            await service.reset_conversation()
        self.assertEqual(service._appended_audio_ms, 0.0)


def response_done(status="completed", rid="r9", output=()):
    usage = SimpleNamespace(input_token_details=None, output_token_details=None,
                            input_tokens=1, output_tokens=0, total_tokens=1)
    return SimpleNamespace(response=SimpleNamespace(id=rid, status=status, output=list(output), usage=usage))


class TestSilentResponses(unittest.IsolatedAsyncioTestCase):
    async def run_done(self, evt, with_audio=False):
        service = make_service()
        service._responses_with_audio = {"r9"} if with_audio else set()
        fired = []
        service.on_silent_response = lambda: fired.append(1)
        with patch.object(OpenAIRealtimeLLMService, "_handle_evt_response_done", new=AsyncMock()):
            await service._handle_evt_response_done(evt)
        return fired

    async def test_completed_response_without_audio_or_tools_is_silent(self):
        self.assertEqual(await self.run_done(response_done()), [1])

    async def test_audio_tool_call_or_cancel_is_not_silent(self):
        self.assertEqual(await self.run_done(response_done(), with_audio=True), [])
        call = SimpleNamespace(type="function_call", content=None)
        self.assertEqual(await self.run_done(response_done(output=[call])), [])
        self.assertEqual(await self.run_done(response_done(status="cancelled")), [])
        audio_item = SimpleNamespace(type="message", content=[SimpleNamespace(type="output_audio")])
        self.assertEqual(await self.run_done(response_done(output=[audio_item])), [])


if __name__ == "__main__":
    unittest.main()
