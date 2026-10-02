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

from app.early_ack import ACK_FALLBACK, EARLY_ACK_PHRASES, SLOW_TOOL_HINT, ack_phrase
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
    liveness.woke()  # after a new wake word (a follow-up waits longer, section 4)
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
    app._ack_clips = {}
    app.voice = "cedar"
    app.enrollment_conductor = SimpleNamespace(_tts=AsyncMock(return_value=b"\0" * 4800))
    app._guarded_say = AsyncMock()
    connection = SimpleNamespace(turn_liveness=liveness, device_id="kontoret", provider=OPENAI)
    return app, connection


@pytest.mark.asyncio
async def test_ack_goes_out_of_band_on_the_guarded_lane_at_once():
    import time
    app, connection = _app(TurnLiveness())
    await app._early_ack(connection, time.monotonic())
    app._guarded_say.assert_awaited_once()
    text, device = app._guarded_say.await_args.args
    assert text in EARLY_ACK_PHRASES and device == "kontoret"
    assert app._guarded_say.await_args.kwargs == {"pace": False, "pcm": b"\0" * 4800}


@pytest.mark.asyncio
async def test_ack_dropped_if_the_model_started_while_the_clip_was_fetched():
    import time
    liveness = TurnLiveness()
    app, connection = _app(liveness)
    started = time.monotonic()
    app.enrollment_conductor._tts.side_effect = lambda text, **kw: liveness.bot_started()
    await app._early_ack(connection, started)
    app._guarded_say.assert_not_awaited()


# --- 2b. the ack in the answer's voice (0.23.3) -----------------------------
# Owner 2026-10-02 18:12: "Vänta, jag kollar" came in OpenAI's voice, the
# answer in Gemini's Charon -- two people in the room.

@pytest.mark.asyncio
async def test_ack_voice_follows_the_engine(monkeypatch):
    import time
    gemini = AsyncMock(return_value=b"G" * 4800)
    monkeypatch.setattr("app.main.gemini_tts", gemini)
    app, connection = _app(TurnLiveness())
    app.gemini_api_key, app.gemini_voice = "AIza-test", "Charon"

    connection.provider = GEMINI
    await app._early_ack(connection, time.monotonic())
    assert gemini.await_args.args[1:] == ("AIza-test", "Charon")
    assert app._guarded_say.await_args.kwargs["pcm"] == b"G" * 4800
    app.enrollment_conductor._tts.assert_not_awaited()

    connection.provider = OPENAI
    await app._early_ack(connection, time.monotonic())
    assert app.enrollment_conductor._tts.await_args.kwargs == {"voice": "cedar"}


@pytest.mark.asyncio
async def test_failed_render_falls_back_to_the_old_clip_and_says_so(monkeypatch, caplog):
    import time
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr("app.main.gemini_tts", AsyncMock(side_effect=RuntimeError("429")))
    app, connection = _app(TurnLiveness())
    app.gemini_api_key, app.gemini_voice = "AIza-test", "Charon"
    connection.provider = GEMINI
    await app._early_ack(connection, time.monotonic())
    app.enrollment_conductor._tts.assert_awaited_once()
    assert app.enrollment_conductor._tts.await_args.kwargs == {}  # the conductor's own voice
    assert app._guarded_say.await_args.kwargs["pcm"] == b"\0" * 4800
    assert any("using the old clip" in r.getMessage() for r in caplog.records)


def test_gemini_clip_is_resampled_to_what_the_device_plays():
    import numpy as np
    from app.early_ack import to_clip_rate

    one_second_at_16k = np.zeros(16000, dtype=np.int16).tobytes()
    assert len(to_clip_rate(one_second_at_16k, "audio/L16;codec=pcm;rate=16000")) == 48000
    same = b"\1\0" * 100
    assert to_clip_rate(same, "audio/L16;codec=pcm;rate=24000") is same


def test_phrases_never_ask():
    assert all("?" not in p for p in EARLY_ACK_PHRASES)


# --- 2c. semantic "jag kollar" (0.25.6) ---------------------------------------
# Owner 2026-10-02 23:12: the fixed clips sound mechanical; the agent should
# say what it is about to do. The model says it (slow tools' descriptions);
# the clip names the tool's job and only fills in when the model was silent.

def test_slow_tools_carry_the_hint_fast_tools_do_not(monkeypatch):
    seen = {}
    monkeypatch.setattr("app.providers.openai_realtime.build",
                        lambda options, tools: seen.setdefault("tools", tools))
    tools = [{"type": "function", "name": n, "description": "Gör saken.", "parameters": {}}
             for n in ("web_search", "search_home", "play_media", "script__delegera_till_raawr",
                       "kalender_sok", "kalenderaktivitet", "HassTurnOn", "GetLiveContext",
                       "GetDateTime")]
    build_service(OPENAI, ProviderOptions(api_key="sk-test", model="gpt-realtime-2", voice="cedar",
                                                  instructions="Du är Björn."), tools)
    desc = {t["name"]: t["description"] for t in seen["tools"]}
    for slow in ("web_search", "search_home", "play_media", "script__delegera_till_raawr",
                 "kalender_sok", "kalenderaktivitet"):
        assert desc[slow] == "Gör saken." + SLOW_TOOL_HINT, slow
    for fast in ("HassTurnOn", "GetLiveContext", "GetDateTime"):
        assert desc[fast] == "Gör saken.", fast
    assert tools[0]["description"] == "Gör saken."  # the caller's list is untouched


def test_phrase_is_chosen_by_tool():
    assert ack_phrase("vaderprognos") == "Jag kollar vädret."
    assert ack_phrase("web_search") == "Jag söker på nätet."
    assert ack_phrase("script__kalender_sok") == "Jag tittar i kalendern."
    assert ack_phrase("kalenderaktivitet") == "Jag tittar i kalendern."
    assert ack_phrase("play_media") == ack_phrase("search_home") == "Jag letar fram det."
    assert ack_phrase("delegera_till_raawr") == "Jag ber Raawr ta det."
    assert ack_phrase(None) == ack_phrase("HassTurnOn") == ACK_FALLBACK


@pytest.mark.asyncio
async def test_slow_tool_ack_says_what_the_tool_does(monkeypatch):
    import time
    service = _acking_service(GEMINI, monkeypatch)
    await _call(service, "web_search", _slow)
    assert service.early_ack.await_args.kwargs == {"tool": "web_search"}
    app, connection = _app(TurnLiveness())
    await app._early_ack(connection, time.monotonic(), tool="web_search")
    assert app._guarded_say.await_args.args[0] == "Jag söker på nätet."
    await app._early_ack(connection, time.monotonic(), 0.0)  # the silence trigger
    assert app._guarded_say.await_args.args[0] == ACK_FALLBACK


@pytest.mark.asyncio
async def test_no_clip_when_the_model_spoke_earlier_this_turn(monkeypatch):
    """It said "Jag söker på nätet efter det" 3 s before the slow call (a tool chain)."""
    import time
    liveness = TurnLiveness()
    now = time.monotonic()
    liveness.user_started_at = now - 4.0
    liveness.bot_started_at, liveness.bot_stopped_at = now - 3.5, now - 3.0  # past RECENT_BOT_S
    service = _acking_service(GEMINI, monkeypatch, liveness)
    await _call(service, "web_search", _slow)
    service.early_ack.assert_not_awaited()
    # The clip itself checks again: nothing over the model's own words.
    app, connection = _app(liveness)
    await app._early_ack(connection, time.monotonic(), tool="web_search")
    app._guarded_say.assert_not_awaited()
    # A new turn, the model silent: the clip plays.
    liveness.turn_over()
    liveness.woke()
    await _call(service, "web_search", _slow)
    assert service.early_ack.await_count == 1


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


# --- 4. follow-ups wait longer (0.24.1) ------------------------------------
# The owner, 2026-10-02 21:16: "vänta, jag kollar" is good on the first
# question after the wake word, not on follow-ups in the same conversation
# (the post-reply follow-up window, no new wake) unless the wait is long.

from app.providers.tool_registration import ack_delay_ms


def _followup_liveness():
    """A woken turn that was answered: the next utterance is a follow-up."""
    liveness = TurnLiveness()
    liveness.woke()
    liveness.turn_over()
    return liveness


def test_wake_turn_acks_at_900_followup_at_3000(monkeypatch):
    monkeypatch.delenv("EARLY_ACK_FOLLOWUP_MS", raising=False)
    liveness = TurnLiveness()
    assert ack_delay_ms(900, liveness) == 900  # first turn of the connection
    liveness.turn_over()
    assert ack_delay_ms(900, liveness) == 3000  # follow-up window, no wake
    assert ack_delay_ms(500, liveness) == 3000  # slow-tool trigger too
    liveness.woke()
    assert ack_delay_ms(900, liveness) == 900  # a new wake word
    monkeypatch.setenv("EARLY_ACK_FOLLOWUP_MS", "0")
    assert ack_delay_ms(900, _followup_liveness()) == 0
    assert ack_delay_ms(0, liveness) == 0  # the wake value 0 is still "off"


def test_mic_flush_is_not_a_wake():
    """Only the device's wake marks a woken turn; the flush that closes an
    unused follow-up window must not."""
    emitter = PhaseEmitter(send_phase=None, liveness=_followup_liveness())
    emitter.note_wake()  # what _on_device_mic_flush calls
    assert not emitter._liveness.from_wake()


@pytest.mark.asyncio
async def test_followup_silence_ack_waits_for_the_followup_threshold(monkeypatch):
    monkeypatch.setenv("EARLY_ACK_FOLLOWUP_MS", "300")
    woken = TurnLiveness()
    woken.woke()
    service = _silent_service(GEMINI, monkeypatch, woken)
    await service._end_activity()
    await asyncio.sleep(0.15)
    service.early_ack.assert_awaited_once()  # wake turn: 50 ms

    service = _silent_service(GEMINI, monkeypatch, _followup_liveness())
    await service._end_activity()
    await asyncio.sleep(0.15)
    service.early_ack.assert_not_awaited()  # follow-up: not at the wake threshold
    await asyncio.sleep(0.3)
    service.early_ack.assert_awaited_once()  # but after a really long wait


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [OPENAI, GEMINI])
async def test_followup_zero_never_acks(provider, monkeypatch):
    monkeypatch.setenv("EARLY_ACK_FOLLOWUP_MS", "0")
    service = _silent_service(provider, monkeypatch, _followup_liveness())
    if provider == GEMINI:
        await service._end_activity()
    else:
        service.arm_silence_ack()
    await _call(service, "web_search", _slow)
    await asyncio.sleep(0.3)
    service.early_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_slow_tool_acks_only_past_the_followup_threshold(monkeypatch):
    monkeypatch.setenv("EARLY_ACK_FOLLOWUP_MS", "100")
    service = _acking_service(GEMINI, monkeypatch, _followup_liveness())
    await _call(service, "web_search", _slow)  # 200 ms tool
    assert service.early_ack.await_count == 1
    monkeypatch.setenv("EARLY_ACK_FOLLOWUP_MS", "400")
    service = _acking_service(GEMINI, monkeypatch, _followup_liveness())
    await _call(service, "web_search", _slow)
    assert service.early_ack.await_count == 0
