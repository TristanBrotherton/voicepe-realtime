"""Bana 0 must not touch the house on a command the speech-to-text made up.

The bridge gives Whisper a hint of common house phrases ("Vad är klockan? Tänd i köket. Björn.", HEN-362) and Whisper
hands it back on noise and on his own voice from the speaker. Written of his echo 2026-10-12/13: 'Tänd i köket.' /
'Tänd i követ.' four times (three in replays, one live), 'Vad är klockan?' twice, 'Björn.' once; and live 2026-10-12
21:40 'Tänd i köket.' -> 'Slog på lampan'."""
import httpx
import pytest

from app import bana0
from tests.test_bana0 import FakeService, PCM, _ha, comms  # noqa: F401 -- the fake comms fixture


@pytest.mark.parametrize("text", ["Tänd i köket.", "tänd i köket", "Tänd i követ.", "Vad är klockan?", "Björn.",
                                   "Björn. Tänd i köket.", "Vad är klockan? Tänd i köket. Björn."])
def test_the_hint_coming_back_is_no_proof_anyone_said_it(text):
    assert bana0.ur_ledtraden(text)


@pytest.mark.parametrize("text", ["Tänd i köket och hallen.", "Släck i köket.", "Tänd lampan i kontoret", "Vad är klockan i Tokyo?",
                                   "Björn, spela P3", "Tänd i sovrummet.", "", None, "Tack."])
def test_what_a_person_says_beyond_the_hint_is_theirs(text):
    assert not bana0.ur_ledtraden(text)


async def _tur(text, service, med_text=None):
    async def stt(pcm, timeout):
        return text

    async def skapa_svar():
        service.events.append("svar_fran_ljud")

    return await bana0.tur(
        PCM, stt=stt, timeout_stt=1.0, timeout_comms=4.0,
        skicka_svar_till_modellen=lambda t: bana0.be_om_bekraftelse(service, t),
        skapa_svar=skapa_svar, skapa_svar_med_text=med_text)


@pytest.mark.asyncio
async def test_an_invented_command_never_reaches_the_house_and_the_model_gets_the_audio_not_the_text(comms):
    comms.svar = _ha("Slog på lampan")          # the house WOULD do it, if it were asked
    service, texter = FakeService(), []

    async def med_text(t):
        texter.append(t)

    assert await _tur("Tänd i köket.", service, med_text) == "modell"
    assert comms.seen == []                     # the house was never asked
    assert texter == []                         # and the invented words were not handed to the model as the user's
    assert service.events == ["svar_fran_ljud"]


@pytest.mark.asyncio
async def test_a_command_that_says_more_than_the_hint_still_goes_the_fast_way(comms):
    comms.svar = _ha("Tände i köket och hallen")
    service = FakeService()
    assert await _tur("Tänd i köket och hallen.", service) == "bana0"
    assert len(comms.seen) == 1


@pytest.mark.asyncio
async def test_the_hint_follows_the_bridge_setting(monkeypatch):
    monkeypatch.setattr(bana0, "STT_LEDTRAD", "Släck allt.")
    assert bana0.ur_ledtraden("Släck allt.") and not bana0.ur_ledtraden("Tänd i köket.")
    monkeypatch.setattr(bana0, "STT_LEDTRAD", "")
    assert not bana0.ur_ledtraden("Tänd i köket.")
