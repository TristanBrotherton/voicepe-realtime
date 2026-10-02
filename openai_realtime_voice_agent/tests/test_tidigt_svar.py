"""Tool timing, the early "jag kollar" (0.23.1) and the silence ack (0.23.2).

The owner, 2026-10-02 17:02: when the agent has to check something in the
backend it goes quiet; a smart agent says it has understood and needs to
check. Two parts: every tool call logs `⏱ tool <name> <ms> ok|fel`, and a
tool still running after EARLY_ACK_MS gets one short spoken acknowledgement
per turn -- never when the model is already talking.
"""
import asyncio
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.early_ack import EARLY_ACK_PHRASES, pick_early_ack
from app.main import Application
from app.phase_emitter import PhaseEmitter, TurnLiveness
from app.providers import GEMINI, OPENAI, ProviderOptions, build_service

TIMING = re.compile(r"⏱ tool (\S+) (\d+) (ok|fel)$")


def _service(provider):
    if provider == GEMINI:
        opts = ProviderOptions(api_key="AIza-test", model="models/gemini-2.5-flash-native-audio-latest",
                               voice="Charon", instructions="Du är Björn.", language="sv-SE")
    else:
        opts = ProviderOptions(api_key="sk-test", model="gpt-realtime-2", voice="cedar",
                               instructions="Du är Björn.")
    return build_service(provider, opts, [])


async def _call(service, name, handler):
    service.register_function(name, handler)
    results = []

    async def result_callback(result, *a, **k):
        results.append(result)

    params = SimpleNamespace(arguments={}, result_callback=result_callback, function_name=name)
    await service._functions[name].handler(params)
    return results


def _timings(caplog):
    return [m.groups() for r in caplog.records if (m := TIMING.search(r.getMessage()))]


# --- 1. timing -------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [OPENAI, GEMINI])
async def test_every_tool_call_logs_one_timing_line(provider, caplog):
    caplog.set_level(logging.INFO)
    service = _service(provider)

    async def slowish(params):
        await asyncio.sleep(0.05)
        await params.result_callback({"ok": True})

    await _call(service, "web_search", slowish)
    lines = _timings(caplog)
    assert len(lines) == 1
    name, ms, status = lines[0]
    assert (name, status) == ("web_search", "ok")
    assert 40 <= int(ms) < 1000


@pytest.mark.asyncio
async def test_failed_tool_logs_fel(caplog):
    caplog.set_level(logging.INFO)
    service = _service(GEMINI)

    async def errors(params):
        await params.result_callback({"error": "HA svarade inte"})

    async def raises(params):
        raise RuntimeError("boom")

    await _call(service, "play_media", errors)
    with pytest.raises(RuntimeError):
        await _call(service, "search_home", raises)
    assert [(n, s) for n, _, s in _timings(caplog)] == [("play_media", "fel"), ("search_home", "fel")]


# --- 2. early acknowledgement ----------------------------------------------

def _acking_service(provider, monkeypatch, liveness=None):
    monkeypatch.setenv("EARLY_ACK_MS", "50")
    service = _service(provider)
    service.turn_liveness = liveness or TurnLiveness()
    service.early_ack = AsyncMock()
    return service


async def _slow(params):
    await asyncio.sleep(0.2)
    await params.result_callback("svar")


async def _fast(params):
    await params.result_callback("svar")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [OPENAI, GEMINI])
async def test_slow_tool_gets_an_early_ack(provider, monkeypatch):
    service = _acking_service(provider, monkeypatch)
    await _call(service, "web_search", _slow)
    assert service.early_ack.await_count == 1


@pytest.mark.asyncio
async def test_fast_tool_stays_silent(monkeypatch):
    service = _acking_service(GEMINI, monkeypatch)
    await _call(service, "intent__HassTurnOn", _fast)
    await asyncio.sleep(0.1)
    service.early_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_at_most_once_per_turn(monkeypatch):
    liveness = TurnLiveness()
    service = _acking_service(GEMINI, monkeypatch, liveness)
    await _call(service, "web_search", _slow)
    await _call(service, "play_media", _slow)
    assert service.early_ack.await_count == 1
    liveness.turn_over()  # the phase went idle: a new turn
    await _call(service, "web_search", _slow)
    assert service.early_ack.await_count == 2


@pytest.mark.asyncio
async def test_no_ack_when_the_model_already_said_it(monkeypatch):
    """The model said "jag kollar" itself before calling the tool."""
    liveness = TurnLiveness()
    service = _acking_service(GEMINI, monkeypatch, liveness)
    liveness.bot_started()
    liveness.bot_stopped()
    await _call(service, "web_search", _slow)
    service.early_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase_emitter_tells_liveness_the_model_is_talking():
    liveness = TurnLiveness()
    pe = PhaseEmitter(AsyncMock(), idle_debounce_s=0, liveness=liveness)
    pe.push_frame = AsyncMock()
    with patch.object(FrameProcessor, "process_frame", new=AsyncMock()):
        await pe.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert liveness.bot_speaking
        assert not liveness.claim_ack(asyncio.get_running_loop().time())
        await pe.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert not liveness.bot_speaking
    if pe._idle_task:
        await pe._idle_task
    await pe.close()


@pytest.mark.asyncio
async def test_idle_ends_the_turn_for_acks():
    liveness = TurnLiveness()
    liveness.acked = True
    pe = PhaseEmitter(AsyncMock(), idle_debounce_s=0, liveness=liveness)
    await pe._emit("idle")
    assert liveness.acked is False


def _app(liveness):
    app = object.__new__(Application)
    app._last_early_ack = None
    app.enrollment_conductor = SimpleNamespace(_tts=AsyncMock(return_value=b"\0" * 4800))
    app._guarded_say = AsyncMock()
    connection = SimpleNamespace(turn_liveness=liveness, device_id="kontoret")
    return app, connection


@pytest.mark.asyncio
async def test_ack_goes_out_of_band_on_the_guarded_lane_at_once():
    import time
    app, connection = _app(TurnLiveness())
    await app._early_ack(connection, time.monotonic())
    app._guarded_say.assert_awaited_once()
    text, device = app._guarded_say.await_args.args
    assert text in EARLY_ACK_PHRASES and device == "kontoret"
    assert app._guarded_say.await_args.kwargs == {"pace": False}


@pytest.mark.asyncio
async def test_ack_dropped_if_the_model_started_while_the_clip_was_fetched():
    import time
    liveness = TurnLiveness()
    app, connection = _app(liveness)
    started = time.monotonic()
    app.enrollment_conductor._tts.side_effect = lambda text: liveness.bot_started()
    await app._early_ack(connection, started)
    app._guarded_say.assert_not_awaited()


def test_phrases_vary_and_never_ask():
    assert all("?" not in p for p in EARLY_ACK_PHRASES)
    last = None
    for _ in range(50):
        p = pick_early_ack(last)
        assert p != last
        last = p


# --- 3. silence acknowledgement (0.23.2) -----------------------------------
# Live 2026-10-02 15:19:44, Gemini, "vad blir det för väder i helgen":
# activityEnd 44.05, function call 47.97, tool done 48.18 (0.2 s), first
# audio 49.99. The tool was fast, so the tool ack never fired: 6 s of nothing.

from pipecat.frames.frames import InputAudioRawFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.providers import bana0_hit, bana0_miss


def _silent_service(provider, monkeypatch, liveness=None):
    monkeypatch.setenv("EARLY_ACK_SILENCE_MS", "50")
    service = _acking_service(provider, monkeypatch, liveness)
    service.send_client_event = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_gemini_silent_after_activity_end_gets_one_ack(monkeypatch):
    service = _silent_service(GEMINI, monkeypatch)
    await service._end_activity()
    await asyncio.sleep(0.15)
    service.early_ack.assert_awaited_once()
    # Recheck before speaking counts only audio since the model was asked.
    assert service.early_ack.await_args.args[1] == 0.0


@pytest.mark.asyncio
async def test_no_silence_ack_when_the_model_answers_in_time(monkeypatch):
    liveness = TurnLiveness()
    service = _silent_service(GEMINI, monkeypatch, liveness)
    await service._end_activity()
    liveness.bot_started()
    await asyncio.sleep(0.15)
    service.early_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_silence_ack_over_a_new_utterance(monkeypatch):
    liveness = TurnLiveness()
    service = _silent_service(GEMINI, monkeypatch, liveness)
    await service._end_activity()
    liveness.user_started()
    await asyncio.sleep(0.15)
    service.early_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_bana0_hit_never_acks_a_miss_does(monkeypatch):
    service = _silent_service(GEMINI, monkeypatch)
    held = [InputAudioRawFrame(audio=b"", sample_rate=16000, num_channels=1)]
    service._held = list(held)
    await bana0_hit(GEMINI, service, "Släckt i kontoret")
    await asyncio.sleep(0.15)
    service.early_ack.assert_not_awaited()
    service._held = list(held)
    await bana0_miss(GEMINI, service)
    await asyncio.sleep(0.15)
    service.early_ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_openai_bana0_miss_arms_it_and_a_hit_does_not(monkeypatch):
    service = _silent_service(OPENAI, monkeypatch)
    await bana0_hit(OPENAI, service, "Släckt i kontoret")
    await asyncio.sleep(0.15)
    service.early_ack.assert_not_awaited()
    await bana0_miss(OPENAI, service)
    await asyncio.sleep(0.15)
    service.early_ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_openai_speech_stopped_arms_it_only_without_bana0(monkeypatch):
    with patch.object(OpenAIRealtimeLLMService, "_handle_evt_speech_stopped", new=AsyncMock()):
        service = _silent_service(OPENAI, monkeypatch)
        await service._handle_evt_speech_stopped(None)
        await asyncio.sleep(0.15)
        service.early_ack.assert_awaited_once()

        held = _silent_service(OPENAI, monkeypatch)
        held.on_user_turn_end = AsyncMock()  # bana 0 decides first
        await held._handle_evt_speech_stopped(None)
        await asyncio.sleep(0.15)
        held.early_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_ack_and_tool_ack_share_once_per_turn(monkeypatch):
    liveness = TurnLiveness()
    service = _silent_service(GEMINI, monkeypatch, liveness)
    await service._end_activity()
    await asyncio.sleep(0.15)
    await _call(service, "script__vaderprognos", _slow)
    assert service.early_ack.await_count == 1


def test_dangling_stop_is_not_a_turn_to_ack():
    liveness = TurnLiveness()
    import time
    asked = time.monotonic()
    liveness.no_ack()
    assert not liveness.claim_silence_ack(asked)
    liveness.user_started()  # a real utterance opens a new turn
    assert liveness.claim_silence_ack(time.monotonic())


@pytest.mark.asyncio
async def test_phase_emitter_marks_user_speech_and_dangling_stops():
    liveness = TurnLiveness()
    pe = PhaseEmitter(AsyncMock(), idle_debounce_s=0, liveness=liveness)
    pe.push_frame = AsyncMock()
    pe.note_wake()
    with patch.object(FrameProcessor, "process_frame", new=AsyncMock()):
        await pe.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert liveness.acked  # dangling: no ack for it
        await pe.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert not liveness.acked and liveness.user_started_at > float("-inf")
    await pe.close()
