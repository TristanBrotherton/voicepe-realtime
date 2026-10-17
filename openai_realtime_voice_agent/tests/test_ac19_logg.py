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
