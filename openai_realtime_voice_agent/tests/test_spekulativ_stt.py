"""Bana 0's speech-to-text starts early, on "preend" (raawr US-032): the model still
never hears a hit, and a pause that ends in more speech throws the early text away."""
import asyncio

import pytest

from app import bana0
from app.providers.local_turns import LocalTurns
from tests.test_bana0 import _gemini_koppling, _speak, _transcript, _wyoming, ha_svarar  # noqa: F401


class FakeVad:
    """sherpa's detector: is_speech_detected follows a script, one entry per accepted chunk."""

    def __init__(self, script):
        self.script, self.i = list(script), 0

    def accept_waveform(self, samples):
        self.i += 1

    def empty(self):
        return True

    def is_speech_detected(self):
        return self.script[min(self.i, len(self.script)) - 1]

    def reset(self):
        pass


def _feed(turns, n):
    import numpy as np
    chunk = (np.zeros(512, dtype=np.int16)).tobytes()
    return [turns.feed(chunk) for _ in range(n)]


def test_preend_kommer_fore_end_och_bara_medan_turen_pagar():
    #           speech ... quiet 500 ms (pre) ... quiet 800 ms (main)
    main = FakeVad([False, True, True, True, True, False])
    pre = FakeVad([False, True, True, True, False, False])
    assert _feed(LocalTurns(main, 16000, pre), 6) == [None, "start", None, None, "preend", "end"]


def test_preend_igen_efter_en_paus_som_blev_tal():
    main = FakeVad([False, True, True, True, True, True, True, False])
    pre = FakeVad([False, True, False, True, True, False, False, False])
    handelser = _feed(LocalTurns(main, 16000, pre), 8)
    assert handelser.count("preend") == 2 and handelser[-1] == "end"


def test_utan_pre_ar_allt_som_forut():
    main = FakeVad([False, True, True, False])
    assert _feed(LocalTurns(main, 16000), 4) == [None, "start", None, "end"]


def _spela_in(monkeypatch, connection):
    """(STT calls' audio lengths, the turn's final audio length), to tell early from final."""
    anrop, slutlangd = [], []
    riktig = bana0.transkribera

    async def transkribera(pcm, host, port, timeout):
        anrop.append(len(pcm))
        return await riktig(pcm, host, port, timeout)

    monkeypatch.setattr(bana0, "transkribera", transkribera)
    ta = connection.serializer.take_turn_audio

    def ta_spar():
        pcm = ta()
        slutlangd.append(len(pcm))
        return pcm

    connection.serializer.take_turn_audio = ta_spar
    return anrop, slutlangd


def _stt_korningar(seen):
    return len([1 for t, _, _ in seen if t == "audio-stop"])


@pytest.mark.asyncio
async def test_en_paus_som_inte_blev_tal_ger_en_enda_stt_och_modellen_hor_inget_av_en_traff(ha_svarar, monkeypatch):
    from test_gemini_provider import _kinds

    ha_svarar.append("Släckte i kontoret")
    server, port, seen = await _wyoming(_transcript("släck kontoret"))
    connection, service, google, said, idle = _gemini_koppling(
        ("127.0.0.1", port), [None, "start", "preend", None, "end"])
    anrop, slutlangd = _spela_in(monkeypatch, connection)
    async with server:
        await _speak(connection, service, 5)
        await service._turn_end_task
        await asyncio.sleep(0.05)
    assert _stt_korningar(seen) == 1 and len(anrop) == 1  # the early one is used, not asked again
    assert anrop[0] < slutlangd[0]  # and it was asked on the audio up to "preend", not the whole turn
    assert said == [(bana0.OK_FALLBACK, "kontoret")] and idle == ["bana0"]
    assert _kinds(google) == []  # a hit: Google never heard the order


@pytest.mark.asyncio
async def test_tal_som_fortsatte_efter_pausen_kastar_den_tidiga_texten(ha_svarar, monkeypatch):
    ha_svarar.append(None)
    server, port, seen = await _wyoming(_transcript("vad är klockan i morgon"))
    connection, service, google, said, idle = _gemini_koppling(
        ("127.0.0.1", port), [None, "start", "preend", None, None, None, None, "end"])
    anrop, slutlangd = _spela_in(monkeypatch, connection)
    async with server:
        await _speak(connection, service, 8)
        await service._turn_end_task
    # The turn grew by far more than quiet since "preend": asked again on the whole turn.
    assert len(anrop) == 2 and anrop[0] < anrop[1] == slutlangd[0]


@pytest.mark.asyncio
async def test_utan_preend_fraga_bana_0_som_forut(ha_svarar):
    ha_svarar.append(None)
    server, port, seen = await _wyoming(_transcript("hur varmt är det"))
    connection, service, google, said, idle = _gemini_koppling(
        ("127.0.0.1", port), [None, "start", None, None, "end"])
    async with server:
        await _speak(connection, service, 5)
        await service._turn_end_task
    assert _stt_korningar(seen) == 1
