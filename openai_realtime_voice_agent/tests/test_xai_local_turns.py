"""xAI turn end decided locally (0.25.3).

Live 2026-10-02 20:29:42, xai, server_vad 800 ms: wake, "Vad är det för väder
i helgen?", and "thinking" only at 20:29:55.9 -- 13 s later. Later turns sat
5-9 s. Music and room noise kept xAI's VAD from hearing the silence, and no
"jag kollar" could fire, because its clock starts at the turn end.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.services.openai.realtime import events as E

from app.phase_emitter import PhaseEmitter, TurnLiveness
from app.providers import XAI, ProviderOptions, build_service, drop_pending_input_audio


def _options(**over):
    base = dict(api_key="xai-test", model="grok-voice-latest", voice="rex", instructions="Du är Björn.",
                turn_detection_type="server_vad", vad_silence_duration_ms=800,
                transcription_language="sv", semantic_vad_create_response=False)
    base.update(over)
    return ProviderOptions(**base)


class _ScriptedTurns:
    def __init__(self, events):
        self.events = list(events)
        self.rates = []
        self.resets = 0

    def feed(self, pcm16, sample_rate=None):
        self.rates.append(sample_rate)
        return self.events.pop(0)

    def reset(self):
        self.resets += 1


def _frame(byte):
    return InputAudioRawFrame(audio=bytes([byte, 0]) * 480, sample_rate=24000, num_channels=1)


def _wired(events, monkeypatch, **over):
    monkeypatch.setenv("EARLY_ACK_SILENCE_MS", "50")
    service = build_service(XAI, _options(**over), [])
    assert service._turns is not None, "Silero (pipecat) + sherpa-onnx must load"
    service._turns = _ScriptedTurns(events)
    sent, pushed = [], []
    service._ws_send = AsyncMock(side_effect=lambda payload: sent.append(payload))
    service.push_frame = AsyncMock(side_effect=lambda frame, *a: pushed.append(type(frame)))
    service.start_ttfb_metrics = service.start_processing_metrics = AsyncMock()
    service.turn_liveness = TurnLiveness()
    service.early_ack = AsyncMock()
    return service, sent, pushed


async def _session_update(service):
    sent = []
    service._ws_send = AsyncMock(side_effect=lambda payload: sent.append(payload))
    await service.send_client_event(E.SessionUpdateEvent(session=service._session_properties))
    return sent[0]["session"]["audio"]["input"]


@pytest.mark.asyncio
async def test_the_session_turns_xais_vad_off_by_default():
    service = build_service(XAI, _options(), [])
    audio_in = await _session_update(service)
    assert "turn_detection" in audio_in and audio_in["turn_detection"] is None
    assert audio_in["transcription"] == {"language_hint": "sv"}


@pytest.mark.asyncio
async def test_xai_turn_detection_server_keeps_server_vad():
    service = build_service(XAI, _options(xai_turn_detection="server"), [])
    assert service._turns is None
    audio_in = await _session_update(service)
    assert audio_in["turn_detection"]["type"] == "server_vad"
    assert audio_in["turn_detection"]["create_response"] is False


@pytest.mark.asyncio
async def test_a_local_turn_is_committed_and_answered(monkeypatch):
    """Nothing reaches xAI before speech; then the pre-roll, the speech, and at
    the local silence: commit + response.create, phase frames, and the
    silence ack armed (it fires: the model said nothing)."""
    service, sent, pushed = _wired([None, None, "start", None, "end"], monkeypatch)
    for b in range(5):
        await service._send_user_audio(_frame(b))

    kinds = [p["type"] for p in sent]
    assert kinds == ["input_audio_buffer.append"] * 4 + ["input_audio_buffer.commit", "response.create"]
    import base64
    assert base64.b64decode(sent[0]["audio"]) == _frame(0).audio + _frame(1).audio
    assert pushed == [UserStartedSpeakingFrame, UserStoppedSpeakingFrame]
    assert service._turns.rates == [24000] * 5
    await asyncio.sleep(0.15)
    service.early_ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_with_bana0_the_turn_is_committed_and_bana0_decides(monkeypatch):
    service, sent, _ = _wired(["start", "end"], monkeypatch)
    started, decided = [], asyncio.Event()

    async def turn_end():
        decided.set()

    service.on_user_turn_start = lambda: started.append(1)
    service.on_user_turn_end = turn_end
    await service._send_user_audio(_frame(1))
    await service._send_user_audio(_frame(2))
    await asyncio.wait_for(decided.wait(), 1)
    assert started == [1]
    assert [p["type"] for p in sent][-1] == "input_audio_buffer.commit"  # no response.create


@pytest.mark.asyncio
async def test_follow_up_cut_off_mid_utterance_is_answered(monkeypatch):
    service, sent, _ = _wired(["start"], monkeypatch)
    await service._send_user_audio(_frame(1))
    assert await drop_pending_input_audio(XAI, service, keep_speech=True) == "input_audio_buffer.commit"
    assert [p["type"] for p in sent][-2:] == ["input_audio_buffer.commit", "response.create"]
    assert service._turns.resets == 1


@pytest.mark.asyncio
async def test_a_stop_drops_the_open_turn(monkeypatch):
    service, sent, _ = _wired(["start", None, None], monkeypatch)
    await service._send_user_audio(_frame(1))
    assert await drop_pending_input_audio(XAI, service) == "input_audio_buffer.clear"
    assert not service._turn_open
    sent.clear()
    await service._send_user_audio(_frame(2))  # quiet after the stop: held, not sent
    assert sent == []


def test_silero_gets_16k_from_xais_24k():
    from app.providers.local_turns import LocalTurns

    class Vad:
        got = 0

        def accept_waveform(self, samples):
            Vad.got = len(samples)

        def empty(self):
            return True

        def is_speech_detected(self):
            return False

    turns = LocalTurns(Vad())
    turns.feed(b"\0\0" * 480, 24000)
    assert Vad.got == 320


# --- the early ack --------------------------------------------------------------
# Live 20:29:56.165 bana 0 missed (the silence ack is armed); 20:29:57.038 the
# user transcript arrived and pipecat emulated "user started speaking" for it.
# That stamp called off the ack 0.6 s before its 1.5 s were up -- every turn.

def test_the_late_transcript_does_not_call_off_the_silence_ack():
    liveness = TurnLiveness()
    liveness.user_started()           # the turn itself (local VAD / speech_started)
    asked = time.monotonic()          # the model is asked
    liveness.user_started(emulated=True)  # its transcript, ~1 s later
    assert liveness.claim_silence_ack(asked)


def test_an_emulated_start_still_opens_a_turn_nothing_else_opened():
    liveness = TurnLiveness()
    liveness.turn_over()
    liveness.user_started(emulated=True)
    assert liveness.user_started_at > liveness.turn_over_at


@pytest.mark.asyncio
async def test_phase_emitter_passes_the_emulated_flag():
    liveness = TurnLiveness()
    pe = PhaseEmitter(AsyncMock(), liveness=liveness)
    pe.push_frame = AsyncMock()
    from pipecat.processors.frame_processor import FrameDirection
    await pe.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    first = liveness.user_started_at
    liveness.acked = True
    await pe.process_frame(UserStartedSpeakingFrame(emulated=True), FrameDirection.DOWNSTREAM)
    assert liveness.user_started_at == first and liveness.acked


# --- the 900 s idle close -------------------------------------------------------

IDLE = json.dumps({"type": "error", "event_id": "87e0", "error": {
    "type": "server_error", "code": "timeout",
    "message": "Conversation timed out after 900.0 seconds due to inactivity"}})


@pytest.mark.asyncio
async def test_the_idle_close_sleeps_never_reconnects_never_a_strike():
    """raawr INKAST 2026-10-04: reconnecting each 900 s idle close all night
    cost ~45 dollars (xAI bills per connected minute). Now it goes to sleep."""
    service = build_service(XAI, _options(), [])
    service.sover = False  # awake, mid-session
    closed = asyncio.Event()

    class Ws:
        async def __aiter__(self):
            yield IDLE
            await closed.wait()  # our own disconnect ends it

        async def close(self):
            closed.set()

    service._websocket = Ws()
    service.push_error = AsyncMock()
    service._handle_evt_error = AsyncMock()
    await asyncio.wait_for(service._receive_task_handler(), 1)
    service._handle_evt_error.assert_not_awaited()  # no ErrorFrame carrying the timeout
    assert service.push_error.await_args_list == []  # nothing for the recovery to reconnect
    assert service.sover is True
