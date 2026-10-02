"""xAI Grok Voice as a third engine (0.25.0).

tests/fixtures/xai_events.jsonl is raw events from xAI's realtime socket,
recorded against the live key on 2026-10-02 (transcripts shortened). Every
pipecat refusal below was seen there first.
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.services.openai.realtime import events as E

from app.providers import (
    XAI,
    ProviderOptions,
    build_service,
    input_sample_rate,
    self_heals,
    supports_client_events,
)
from app.providers.xai_realtime import (
    XAI_REALTIME_URL,
    XaiRealtimeLLMService,
    _XaiSocket,
    translate_server_event,
    xai_session,
)

RAW = [line for line in (Path(__file__).parent / "fixtures" / "xai_events.jsonl").read_text().splitlines() if line]
SESSION_UPDATED = json.dumps({
    "type": "session.updated", "event_id": "e1",
    "session": {"voice": "rex", "audio": {"input": {"transcription": {"language_hint": "sv"}}}},
})


def _raw(kind, name=None):
    for line in RAW:
        evt = json.loads(line)
        if evt["type"] == kind and (name is None or evt.get("name") == name):
            return line
    raise LookupError(kind)


def _options(**over):
    base = dict(api_key="xai-test", model="grok-voice-latest", voice="rex", instructions="Du är Björn.",
                max_output_tokens=1024, turn_detection_type="server_vad", vad_silence_duration_ms=800,
                transcription_language="sv")
    base.update(over)
    return ProviderOptions(**base)


def test_xai_is_an_openai_protocol_engine():
    assert input_sample_rate(XAI) == 24000
    assert self_heals(XAI) is False
    assert supports_client_events(XAI) is True


def test_build_points_pipecat_at_xai():
    service = build_service(XAI, _options(), [])
    assert isinstance(service, XaiRealtimeLLMService)
    assert service.base_url == f"{XAI_REALTIME_URL}?model=grok-voice-latest"
    td = service._session_properties.audio.input.turn_detection
    assert td.type == "server_vad" and td.silence_duration_ms == 800


def test_every_recorded_xai_event_parses_or_is_dropped_on_purpose():
    # Without the translation pipecat raises on most of these and the reader
    # dies: the device goes deaf after its first answer.
    dropped = []
    for line in RAW + [SESSION_UPDATED]:
        out = translate_server_event(line)
        if out is None:
            dropped.append((json.loads(line)["type"], json.loads(line).get("name")))
            continue
        E.parse_server_event(out)  # raises on anything pipecat cannot read
    assert sorted(dropped) == [
        ("ping", None),
        ("response.function_call_arguments.done", "web_search"),
    ]


def test_response_done_with_empty_usage_reaches_pipecat():
    # xAI sends "usage": {} -- pipecat's model requires the token counts and
    # its handler reads them. A dropped response.done is a reply that never ends.
    evt = E.parse_server_event(translate_server_event(_raw("response.done")))
    assert evt.response.usage.total_tokens == 0


def test_session_updated_is_never_dropped():
    # It is what makes pipecat call the session ready; dropping it = deaf.
    evt = E.parse_server_event(translate_server_event(SESSION_UPDATED))
    assert evt.type == "session.updated"


def test_server_side_search_report_is_not_answered_but_house_tools_are():
    # xAI already spoke the answer; answering the report makes it talk again.
    assert translate_server_event(_raw("response.function_call_arguments.done", "web_search")) is None
    house = _raw("response.function_call_arguments.done", "intent__HassTurnOn")
    assert E.parse_server_event(translate_server_event(house)).call_id
    # With xAI's search off, our own web_search function is a real request.
    assert translate_server_event(_raw("response.function_call_arguments.done", "web_search"), set()) is not None


def test_audio_delta_alias_is_renamed():
    out = translate_server_event(json.dumps({"type": "response.audio.delta", "event_id": "e", "response_id": "r",
                                             "item_id": "i", "output_index": 0, "content_index": 0, "delta": "AAAA"}))
    assert json.loads(out)["type"] == "response.output_audio.delta"


def test_session_update_is_rewritten_into_xai_shape():
    tools = [{"type": "function", "name": "web_search", "parameters": {}},
             {"type": "function", "name": "intent__HassTurnOn", "parameters": {}}]
    service = build_service(XAI, _options(semantic_vad_create_response=False), tools)
    payload = E.SessionUpdateEvent(session=service._session_properties).model_dump(exclude_none=True)
    xai_session(payload, service._language, service._server_search, service._xai_create_response)
    session = payload["session"]
    assert "truncation" not in session
    assert session["audio"]["input"]["transcription"] == {"language_hint": "sv"}
    assert session["audio"]["input"]["turn_detection"]["create_response"] is False  # bana 0
    assert [t.get("name", t["type"]) for t in session["tools"]] == ["intent__HassTurnOn", "web_search"]
    assert session["tools"][-1] == {"type": "web_search"}


class _FakeWs:
    def __init__(self, lines):
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_the_reader_survives_a_ping_and_marks_the_session_ready():
    service = build_service(XAI, _options(), [])
    service._websocket = _FakeWs([_raw("ping"), SESSION_UPDATED])
    service.push_error = AsyncMock()
    await service._receive_task_handler()
    assert isinstance(service._websocket, _XaiSocket)
    assert service._api_session_ready is True
    errors = [c.kwargs.get("error_msg", "") for c in service.push_error.await_args_list]
    assert not any("died" in e for e in errors), errors


def test_router_accepts_xai(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "xai")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "gemini")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "30")
    r = build_router()
    assert (r.primary, r.backup) == ("xai", "gemini")


def test_provider_options_for_xai(monkeypatch):
    from app.main import Application
    app = object.__new__(Application)
    app.instructions = "Bas."
    app.idag = SimpleNamespace(block=lambda: "")
    app.xai_api_key, app.xai_model, app.xai_voice = "xai-key", "grok-voice-think-fast-2.0", "helios"
    app.max_output_tokens = None
    app.vad_threshold, app.vad_prefix_padding_ms, app.vad_silence_duration_ms = 0.6, 200, 900
    app.bana0_stt = ("127.0.0.1", 10300)
    app.transcription_language = "sv"
    monkeypatch.setattr("app.main.memory_instructions", lambda: "")
    o = app.provider_options(XAI)
    assert (o.api_key, o.model, o.voice) == ("xai-key", "grok-voice-think-fast-2.0", "helios")
    assert o.vad_silence_duration_ms == 900 and o.semantic_vad_create_response is False


@pytest.mark.asyncio
async def test_ack_comes_in_the_xai_voice(monkeypatch):
    from app.main import Application
    from app.phase_emitter import TurnLiveness
    xai = AsyncMock(return_value=b"X" * 4800)
    monkeypatch.setattr("app.main.xai_tts", xai)
    app = object.__new__(Application)
    app._last_early_ack, app._ack_clips = None, {}
    app.voice = "cedar"
    app.xai_api_key, app.xai_voice = "xai-key", "castor"
    app.enrollment_conductor = SimpleNamespace(_tts=AsyncMock(return_value=b"\0" * 4800))
    app._guarded_say = AsyncMock()
    connection = SimpleNamespace(turn_liveness=TurnLiveness(), device_id="kontoret", provider=XAI)
    await app._early_ack(connection, time.monotonic())
    assert xai.await_args.args[1:] == ("xai-key", "castor")
    assert app._guarded_say.await_args.kwargs["pcm"] == b"X" * 4800
    app.enrollment_conductor._tts.assert_not_awaited()
