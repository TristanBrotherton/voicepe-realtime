"""Gemini good enough to be the main engine (2026-10-02 live findings).

Offline: no Google key here. Each test pins what the code SENDS or DOES; what
Google then does with it is the live test's job.
"""
import asyncio
import time

import pytest
from google.genai.types import Blob, Content, LiveServerContent, LiveServerMessage, Part
from pipecat.frames.frames import (
    LLMTextFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)

from app.providers import GEMINI, build_service
from app.providers import gemini_live
from test_gemini_provider import (  # noqa: E402 -- shared fakes, same directory
    OPENAI_SHAPE,
    _ScriptedTurns as ScriptedTurns,
    _frame as frame,
    _kinds as kinds,
    _options as options,
    _wire as wire,
)

NATIVE = "models/gemini-2.5-flash-native-audio-latest"


def _pushed(service):
    out = []

    async def push(f, direction=None):
        out.append(f)

    service.push_frame = push
    return out


# --- 1. Tools on native-audio: thinking off ----------------------------------


def _connect_config(service):
    """Run pipecat's _connect and hand back the LiveConnectConfig it built."""
    seen = []
    service.sover = False  # awake: these tests read what a real connect sends
    service._connection_task_handler = lambda config: config
    service.create_task = lambda c: seen.append(c)
    asyncio.run(service._connect())
    return seen[0]


def test_native_audio_session_asks_for_no_thinking():
    """Live 14:35:46: the thought text named intent__HassTurnOff, then the
    model said "Släckt i kontoret" and sent no toolCall. pipecat sends no
    thinking_config unless told, so the model thought on its default budget."""
    config = _connect_config(build_service(GEMINI, options(model=NATIVE), OPENAI_SHAPE))
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    assert config.thinking_config.include_thoughts is False
    assert config.tools and config.tools[0]["function_declarations"]


def test_the_31_preview_model_keeps_its_own_thinking():
    config = _connect_config(build_service(GEMINI, options(), OPENAI_SHAPE))
    assert config.thinking_config is None


def _model_turn(*parts):
    return LiveServerMessage(
        server_content=LiveServerContent(model_turn=Content(role="model", parts=list(parts)))
    )


def test_a_thought_is_never_the_answer_and_the_audio_behind_it_still_plays():
    """The assistant transcript read "**Analyzing Command Execution** I've
    determined..." before the reply. pipecat pushed that text as the answer,
    and read only parts[0], so audio behind a thought was lost."""
    service = build_service(GEMINI, options(model=NATIVE), OPENAI_SHAPE)
    service._sample_rate = 24000
    pushed = _pushed(service)

    async def noop(*a, **k):
        return None

    service.stop_ttfb_metrics = noop
    msg = _model_turn(
        Part(text="**Analyzing Command Execution**\n\nI've determined...", thought=True),
        Part(inline_data=Blob(data=b"\x01\x00" * 480, mime_type="audio/pcm;rate=24000")),
    )
    asyncio.run(service._handle_msg_model_turn(msg))

    assert not [f for f in pushed if isinstance(f, LLMTextFrame)]
    assert [f for f in pushed if isinstance(f, TTSAudioRawFrame)]
    assert service._bot_text_buffer == ""


# --- 2. End of turn ------------------------------------------------------------


def _silence_used(monkeypatch, **over):
    used = []
    monkeypatch.setattr(
        gemini_live.LocalTurns, "create",
        classmethod(lambda cls, silence_ms, *a, **k: used.append(silence_ms)),
    )
    build_service(GEMINI, options(**over), OPENAI_SHAPE)
    return used[0]


def test_a_pause_of_a_second_does_not_end_his_turn(monkeypatch):
    """800 ms cut him off mid-question at every natural pause."""
    assert _silence_used(monkeypatch) == 1200
    assert _silence_used(monkeypatch, gemini_turn_silence_ms=1500) == 1500
    # Google's own VAD knob no longer decides the local turn end.
    assert _silence_used(monkeypatch, gemini_vad_silence_duration_ms=300) == 1200


def test_the_turn_silence_is_an_add_on_setting():
    from app.main import Application

    app = Application()
    assert app.gemini_turn_silence_ms == 1200
    app.gemini_turn_silence_ms = 900
    for name, value in dict(
        instructions="x", gemini_api_key="k", gemini_model="", gemini_voice="",
        max_output_tokens=None, transcription_language="", gemini_vad_start_sensitivity="high",
        gemini_vad_end_sensitivity="low", gemini_vad_prefix_padding_ms=300,
        gemini_vad_silence_duration_ms=800, gemini_proactive_audio=False,
        gemini_affective_dialog=False,
    ).items():
        setattr(app, name, value)
    assert app.provider_options(GEMINI).gemini_turn_silence_ms == 900


@pytest.mark.asyncio
async def test_our_vad_tells_the_device_he_is_speaking():
    """The device closes the follow-up window unless it hears "listening".
    On Gemini that came only from the input transcript, which can land after
    the window already cut him off."""
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    wire(service, ScriptedTurns([None, "start", None, "end"]))
    pushed = _pushed(service)
    for b in range(4):
        await service._send_user_audio(frame(b))
    speaking = [type(f) for f in pushed if isinstance(f, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame))]
    assert speaking == [UserStartedSpeakingFrame, UserStoppedSpeakingFrame]


@pytest.mark.asyncio
async def test_a_follow_up_closing_mid_sentence_answers_it_instead_of_dropping_it():
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    sent = wire(service, ScriptedTurns(["start", None]))
    _pushed(service)
    await service._send_user_audio(frame(1))
    await service._send_user_audio(frame(2))
    await service.end_audio_stream(keep_speech=True)
    assert kinds(sent) == ["start", "audio", "audio", "end"]


@pytest.mark.asyncio
async def test_a_stop_word_still_drops_the_turn():
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    sent = wire(service, ScriptedTurns(["start", None]))
    _pushed(service)
    await service._send_user_audio(frame(1))
    await service.end_audio_stream()
    assert kinds(sent) == ["start", "audio"]


# --- 4. 1011 mid-turn ----------------------------------------------------------


async def _drop_socket(service, **state):
    lost = []

    async def on_lost():
        lost.append(True)

    async def no_reconnect_error(*a, **k):
        return None

    service.on_turn_lost = on_lost
    service.push_error = no_reconnect_error
    service._connection_start_time = time.time() - 5
    for k, v in state.items():
        setattr(service, k, v)
    await service._handle_connection_error(Exception("1011 None. Internal error encountered."))
    await asyncio.sleep(0)
    return lost


@pytest.mark.asyncio
async def test_a_1011_while_he_speaks_is_said_out_loud():
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    assert await _drop_socket(service, _activity_open=True) == [True]


@pytest.mark.asyncio
async def test_a_1011_while_he_waits_for_the_answer_is_said_out_loud():
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    assert await _drop_socket(service, _reply_awaited_at=time.monotonic() - 3) == [True]


@pytest.mark.asyncio
async def test_an_idle_hangup_stays_quiet():
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    assert await _drop_socket(service) == []
    # An answer that finished long ago is not a lost turn either.
    assert await _drop_socket(service, _reply_awaited_at=time.monotonic() - 600) == []


@pytest.mark.asyncio
async def test_a_finished_reply_is_not_lost():
    service = build_service(GEMINI, options(), OPENAI_SHAPE)
    wire(service, ScriptedTurns(["start", "end"]))
    _pushed(service)
    await service._send_user_audio(frame(1))
    await service._send_user_audio(frame(2))
    assert service._reply_awaited_at is not None

    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
    import unittest.mock as um

    async def _noop(self, message):
        return None

    with um.patch.object(GeminiLiveLLMService, "_handle_msg_turn_complete", _noop):
        await service._handle_msg_turn_complete(None)
    assert await _drop_socket(service) == []


# --- 5. 3.8-live: an empty turn closes the tool step ---------------------------


def _tool_call():
    from google.genai.types import FunctionCall, LiveServerToolCall

    return LiveServerMessage(tool_call=LiveServerToolCall(
        function_calls=[FunctionCall(id="c1", name="search_home", args={})]))


def _turn_complete():
    return LiveServerMessage(server_content=LiveServerContent(turn_complete=True))


def _audio():
    return _model_turn(Part(inline_data=Blob(data=b"\x01\x00" * 480, mime_type="audio/pcm;rate=24000")))


@pytest.mark.asyncio
async def test_the_empty_turn_after_a_tool_call_is_not_the_end_of_the_reply():
    """Probed on gemini-3.8-live 2026-10-02: toolCall, our response, then an
    EMPTY turn_complete 0.01 s later, and the spoken answer in a new turn.
    The empty one must not end the reply: the device stays in its phase, the
    lost-turn clock keeps running, and the reply ends once, after the audio."""
    from app.phase_emitter import PhaseEmitter

    phases = []

    async def send_phase(value):
        phases.append(value)

    emitter = PhaseEmitter(send_phase, idle_debounce_s=0.05)
    service = build_service(GEMINI, options(model="models/gemini-3.8-live"), OPENAI_SHAPE)
    service._sample_rate = 24000
    pushed = _pushed(service)

    async def noop(*a, **k):
        return None

    service.stop_ttfb_metrics = noop
    service.run_function_calls = noop
    service.set_turn_complete_handler(emitter.note_engine_turn_complete)
    # A previous turn ended properly, and this one began with a spoken
    # preamble whose audio has stopped: the debounce is now running.
    emitter._seen_engine_end = True
    emitter._model_turn_open = True
    service._reply_awaited_at = time.monotonic()

    await service._handle_msg_tool_call(_tool_call())
    await service._handle_msg_turn_complete(_turn_complete())
    await emitter._emit_idle_after_debounce.__wrapped__(emitter) if hasattr(
        emitter._emit_idle_after_debounce, "__wrapped__") else None
    idle = asyncio.ensure_future(emitter._emit_idle_after_debounce())
    await asyncio.sleep(0.3)
    assert "idle" not in phases
    assert service._reply_awaited_at is not None

    await service._handle_msg_model_turn(_audio())
    await service._handle_msg_turn_complete(_turn_complete())
    await asyncio.wait_for(idle, 1)
    assert phases == ["idle"]
    assert service._reply_awaited_at is None
