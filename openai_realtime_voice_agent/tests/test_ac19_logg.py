"""AC-19 (US-060, 2026-10-17): the log lines the next test needs to say why a follow-up question after an interruption went unanswered."""
import asyncio
import logging

import pytest
from pipecat.frames.frames import InputAudioRawFrame

from test_gemini_egen_tur import _bygg


def _ram(i=0):
    return InputAudioRawFrame(audio=(8000).to_bytes(2, "little", signed=True) * 320, sample_rate=16000, num_channels=1)


@pytest.mark.asyncio
async def test_the_local_detector_and_the_gate_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    service, sent, pushed = _bygg(monkeypatch, ["start", "end"])
    gate = [False, False, True, True]
    service.slapp_ljud = lambda: gate.pop(0)
    for i in range(4):
        await service._send_user_audio(_ram(i))
        await asyncio.sleep(0)
    text = caplog.text
    assert "audio gate was closed: 2 frames" in text           # two frames were held back, then the gate opened
    assert "local detector: speech end" in text and "engine generating=" in text
    assert service._tal_slut_t > 0                             # the end of speech is remembered for the timing of the answer


@pytest.mark.asyncio
async def test_what_an_interrupted_reply_had_said_is_logged_once(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    service, sent, pushed = _bygg(monkeypatch, [None])

    async def inget():
        return None
    service._handle_interruption = inget
    service._svar_text = "En kolv är en rörlig del i en motor som"
    await service._motor_avbrott()
    assert "Björn sa (avbruten, motorn): En kolv är en rörlig del" in caplog.text
    assert service._svar_text == ""
    caplog.clear()
    await service._motor_avbrott()                             # nothing left to say: no second line
    assert "Björn sa (avbruten" not in caplog.text


@pytest.mark.asyncio
async def test_a_follow_up_after_an_engine_cut_reaches_the_engine_and_is_framed(monkeypatch):
    """AC-19, the 2026-10-17 11:01 timeline as a test: the engine cuts him on the interrupting sentence while the local
    detector hears nothing (so the 'thinking' that follows is the AVBROTT_UTAN_TAL_S timeout, not a speech end); then a
    short question is heard (start, end). Every frame goes to the engine, and the device is told listening/thinking twice.
    What the engine then answers is the engine's; this pins what is ours."""
    import time
    from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame
    service, sent, pushed = _bygg(monkeypatch, [None] * 5 + ["start", None, "end", None])

    async def inget():
        return None
    service._handle_interruption = inget
    service.svarar = lambda: True
    await service._motor_avbrott()                           # the engine cut him; the local detector heard nothing
    n0 = len(sent)
    for i in range(3):
        await service._send_user_audio(_ram(i))
    service._avbrott_t = time.monotonic() - 2.0              # 1.2 s without local speech have passed
    await service._send_user_audio(_ram(3))                  # -> thinking (timeout), UserStopped
    await service._send_user_audio(_ram(4))
    stoppade_efter_timeout = sum(isinstance(f, UserStoppedSpeakingFrame) for f in pushed)
    service.svarar = lambda: False                           # his 1.7 s reply is over: the follow-up question
    for i in range(4):
        await service._send_user_audio(_ram(5 + i))          # start, None, end, None
    started = sum(isinstance(f, UserStartedSpeakingFrame) for f in pushed)
    stopped = sum(isinstance(f, UserStoppedSpeakingFrame) for f in pushed)
    assert len(sent) - n0 == 9                               # nothing was held back from the engine
    assert stoppade_efter_timeout == 1
    assert started == 2 and stopped == 2                     # the cut's own start + the question's start; the timeout's stop + the question's stop
