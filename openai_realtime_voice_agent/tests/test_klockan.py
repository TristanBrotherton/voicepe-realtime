"""The clock without the cloud (raawr US-032 AC-7): local STT text -> cached clips,
neither comms nor the model asked."""
from datetime import datetime, timezone

import httpx
import pytest

from app import bana0, klockan
from tests.test_bana0 import FakeService, _transcript, _wyoming, comms  # noqa: F401

FEM_SATT = [
    "Vad är klockan?",
    "Hur mycket är klockan?",
    "Var är klockan?",  # how Whisper heard it live, 2026-10-02
    "Vet du vad klockan är?",
    "Hej Björn, hur dags är det nu?",
]


@pytest.mark.parametrize("text", FEM_SATT + [
    "vad e klockan", "Vad är klockan just nu?", "Vad är klockan nu då?", "Björn, vad är klockan?",
    "Vad är klockan, Björn?", "Vad är tiden?",
])
def test_klockfragor_kanns_igen(text):
    assert klockan.ar_klockfraga(text)


@pytest.mark.parametrize("text", [
    None, "", "Klockan.", "klockan sju", "ställ klockan på sju", "väck mig klockan sex",
    "tänd lampan i kontoret",
    "vad är klockan i New York", "sätt en timer på tio minuter",
])
def test_annat_ar_ingen_klockfraga(text):
    assert not klockan.ar_klockfraga(text)


@pytest.mark.parametrize("nu, sagt", [
    (datetime(2026, 10, 7, 14, 22, tzinfo=klockan.TZ), ["Klockan är fjorton", "och tjugotvå minuter"]),
    (datetime(2026, 10, 7, 9, 5, tzinfo=klockan.TZ), ["Klockan är nio", "och fem minuter"]),
    (datetime(2026, 10, 7, 0, 0, tzinfo=klockan.TZ), ["Klockan är noll"]),
    (datetime(2026, 10, 7, 7, 1, tzinfo=klockan.TZ), ["Klockan är sju", "och en minut"]),
    (datetime(2026, 10, 7, 23, 59, tzinfo=klockan.TZ), ["Klockan är tjugotre", "och femtionio minuter"]),
    # UTC in, Stockholm out: summer +2, winter +1.
    (datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc), ["Klockan är fjorton", "och trettio minuter"]),
    (datetime(2026, 12, 7, 12, 30, tzinfo=timezone.utc), ["Klockan är tretton", "och trettio minuter"]),
])
def test_ratt_timme_och_minut_i_stockholm(nu, sagt):
    assert klockan.delar(nu) == sagt


def test_varje_minut_har_ett_forrenderat_klipp():
    alla = set(klockan.alla_delar())
    assert len(alla) == 24 + 59
    for h in range(24):
        for m in range(60):
            assert set(klockan.delar(datetime(2026, 1, 1, h, m, tzinfo=klockan.TZ))) <= alla


FAST = datetime(2026, 10, 7, 14, 22, tzinfo=klockan.TZ)


async def _klocktur(port, service, said, klocka):
    async def stt(pcm, timeout):
        return await bana0.transkribera(pcm, "127.0.0.1", port, timeout)
    return await bana0.tur(
        b"\x00" * 3200, stt=stt, timeout_stt=1.0, timeout_comms=4.0,
        skicka_svar_till_modellen=lambda t: bana0.be_om_bekraftelse(service, t),
        skapa_svar=lambda: bana0.be_om_svar(service),
        efter_miss=lambda: said.append("miss"),
        klockan=klocka,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fraga", FEM_SATT)
async def test_klockan_svarar_utan_comms_och_modell(comms, fraga):
    server, port, _ = await _wyoming(_transcript(fraga))
    service, said = FakeService(), []

    async def klocka(text):
        said.extend(klockan.delar(FAST))

    async with server:
        assert await _klocktur(port, service, said, klocka) == "klockan"
    assert said == ["Klockan är fjorton", "och tjugotvå minuter"]
    assert comms.seen == [] and service.events == []


@pytest.mark.asyncio
async def test_annan_fraga_gar_vidare_som_i_dag(comms):
    comms.svar = httpx.Response(204)
    server, port, _ = await _wyoming(_transcript("hur varmt är det ute"))
    service, said = FakeService(), []

    async def klocka(text):
        said.append("klocka")

    async with server:
        assert await _klocktur(port, service, said, klocka) == "modell"
    assert said == ["miss"]
    assert len(comms.seen) == 1 and service.typer() == ["response.create"]


@pytest.mark.asyncio
async def test_klocka_som_fallerar_ger_vanlig_tur(comms):
    comms.svar = httpx.Response(204)
    server, port, _ = await _wyoming(_transcript("vad är klockan"))
    service, said = FakeService(), []

    async def klocka(text):
        raise RuntimeError("no clip")

    async with server:
        assert await _klocktur(port, service, said, klocka) == "modell"
    assert said == ["miss"] and service.typer() == ["response.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fel, pa_disk, vantat_forvantat", [
    # tre fails twice and is skipped; the rest go on, each render paced.
    ({"Klockan är tre": 2}, set(), [8.0, 8.0, 8.0, 60, 8.0]),
    # three clips in a row fail twice: the engine is down, stop.
    ({"Klockan är noll": 2, "Klockan är ett": 2, "Klockan är två": 2}, set(), [60, 8.0, 60, 8.0, 60]),
    # a clip already on disk costs no request and no wait.
    ({}, {"Klockan är noll", "Klockan är ett"}, [8.0]),
])
async def test_uppvarmningen_haller_googles_takt(monkeypatch, fel, pa_disk, vantat_forvantat):
    """10 TTS requests/min (429 on 2026-10-07): a render waits, the disk does not,
    and no clip is cached in another voice."""
    import app.main as main

    vantat, nu = [], [0.0]

    async def sov(s):
        vantat.append(s)

    monkeypatch.setattr(main.asyncio, "sleep", sov)
    monkeypatch.setattr(main.time, "monotonic", lambda: nu[0])

    class Agent:
        async def _ack_clip(self, provider, text, fallback=True):
            assert fallback is False
            if text in pa_disk:
                return b"pcm"
            nu[0] += 1.0  # a render takes a second
            if fel.get(text):
                fel[text] -= 1
                raise RuntimeError("no audio")
            return b"pcm"

    await main.Application._warm_klockan(Agent(), "gemini", takt_s=8.0)
    assert vantat[:len(vantat_forvantat)] == vantat_forvantat
    if len(fel) == 3:
        assert vantat == vantat_forvantat  # stopped after the third
    else:
        renderade = 24 + 59 - len(pa_disk)
        assert len(vantat) == renderade + (1 if fel else 0)  # plus the one minute wait


@pytest.mark.asyncio
async def test_klockan_varms_for_varje_motor_aven_nar_en_replik_fallerar():
    """The clock warms for every engine with a key, also when an engine's early
    acks fail (OpenAI 401 on core, 2026-10-07)."""
    import app.main as main

    klockor = []

    class Agent:
        gemini_api_key, openai_api_key, xai_api_key = "g", "o", ""

        async def _ack_clip(self, provider, text, fallback=True):
            if provider == "openai":
                raise RuntimeError("401")
            return b"pcm"

        async def _warm_klockan(self, provider):
            klockor.append(provider)

    await main.Application._warm_early_acks(Agent())
    assert klockor == ["gemini", "openai"]
