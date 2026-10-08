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


def _kl(h, m, tz=klockan.TZ, manad=10):
    return datetime(2026, manad, 7, h, m, tzinfo=tz)


@pytest.mark.parametrize("nu, sagt", [
    # The way a Swede says it: twelve-hour dial, to the nearest five minutes.
    (_kl(17, 20), "Hon är tjugo över fem."),
    (_kl(17, 19), "Hon är tjugo över fem."),
    (_kl(17, 25), "Hon är fem i halv sex."),
    (_kl(17, 30), "Hon är halv sex."),
    (_kl(17, 35), "Hon är fem över halv sex."),
    (_kl(17, 45), "Hon är kvart i sex."),
    (_kl(17, 58), "Hon är sex."),
    (_kl(9, 5), "Hon är fem över nio."),
    (_kl(9, 15), "Hon är kvart över nio."),
    (_kl(0, 0), "Hon är tolv."),
    (_kl(12, 40), "Hon är tjugo i ett."),
    (_kl(23, 58), "Hon är tolv."),
    (_kl(11, 30), "Hon är halv tolv."),
    # UTC in, Stockholm out: summer +2, winter +1.
    (_kl(12, 30, timezone.utc), "Hon är halv tre."),
    (_kl(12, 30, timezone.utc, 12), "Hon är halv två."),
])
def test_som_en_svensk_sager_det(nu, sagt):
    assert klockan.delar(nu, med_kommentar=False) == [sagt]


@pytest.mark.parametrize("h, rad", [
    (2, "Gå och lägg dig, för fan."), (7, "Kaffe först, sen allt annat."),
    (13, "Dagen rullar på."), (18, "Snart dags att käka."), (23, "Sängdags snart, va?"),
])
def test_bjorns_kommentar_efter_tiden_pa_dygnet(h, rad):
    assert klockan.delar(_kl(h, 0), med_kommentar=True)[1] == rad


def test_varannan_gang_en_kommentar():
    svar = [klockan.delar(_kl(18, 0)) for _ in range(4)]
    assert [len(d) for d in svar] in ([1, 2, 1, 2], [2, 1, 2, 1])


def test_varje_tid_har_ett_klipp_och_de_narmaste_renderas_forst():
    alla = klockan.alla_delar(_kl(17, 20))
    assert len(alla) == len(set(alla)) == 5 + 12 * 12
    for h in range(24):
        for m in range(60):
            assert set(klockan.delar(_kl(h, m), med_kommentar=True)) <= set(alla)
    assert alla[5:8] == ["Hon är tjugo över fem.", "Hon är fem i halv sex.", "Hon är halv sex."]
    # Every clip is a phrase: Gemini TTS gives no audio for a bare number.
    assert all(len(t.split()) >= 3 for t in alla)


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
        said.extend(klockan.delar(FAST, med_kommentar=False))

    async with server:
        assert await _klocktur(port, service, said, klocka) == "klockan"
    assert said == ["Hon är tjugo över två."]
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
@pytest.mark.parametrize("fel, pa_disk, per_dag, vantat_forvantat", [
    # one clip fails twice and is skipped; the rest go on, each render paced.
    ({"Kaffe först, sen allt annat.": 2}, set(), 60, [8.0, 60, 8.0, 8.0]),
    # three in a row fail twice (quota or engine out): wait for tomorrow, go on.
    ({"Gå och lägg dig, för fan.": 2, "Kaffe först, sen allt annat.": 2, "Dagen rullar på.": 2},
     set(), 60, [60, 8.0, 60, 8.0, 60, 8.0, "imorgon", 8.0]),
    # a clip already on disk costs no request and no wait.
    ({}, {"Gå och lägg dig, för fan.", "Kaffe först, sen allt annat."}, 60, [8.0]),
    # the daily share of Google's 100: then tomorrow.
    ({}, set(), 3, [8.0, 8.0, 8.0, "imorgon", 8.0]),
])
async def test_uppvarmningen_haller_googles_takt(monkeypatch, fel, pa_disk, per_dag, vantat_forvantat):
    """10 TTS requests/min and 100/day (429s on 2026-10-07): a render waits, the
    disk does not, no clip is cached in another voice, the quota is shared."""
    import app.main as main

    vantat, nu = [], [0.0]

    async def sov(s):
        vantat.append("imorgon" if s == 86400 else s)

    monkeypatch.setattr(main.asyncio, "sleep", sov)
    monkeypatch.setattr(main.time, "monotonic", lambda: nu[0])
    monkeypatch.setattr(main, "till_ny_dag", lambda: 86400)
    monkeypatch.setattr(main, "KLOCK_PER_DAG", per_dag)
    monkeypatch.setattr(main, "renderat_idag", lambda provider: 0)
    monkeypatch.setattr(klockan, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: _kl(0, 0))}))

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
    renderade = 5 + 144 - len(pa_disk)
    assert vantat.count(8.0) == renderade  # every clip is reached in the end


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


@pytest.mark.parametrize("nu, start", [
    (datetime(2026, 10, 8, 6, 40, tzinfo=timezone.utc), datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)),
    (datetime(2026, 10, 8, 7, 1, tzinfo=timezone.utc), datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)),
    (datetime(2026, 12, 8, 7, 59, tzinfo=timezone.utc), datetime(2026, 12, 7, 8, 0, tzinfo=timezone.utc)),  # winter: PST
])
def test_googles_kvotdygn_borjar_vid_midnatt_i_pacific(nu, start):
    import app.main as main

    assert main.kvotdygn_start(nu) == start


@pytest.mark.parametrize("nu, sekunder", [
    (datetime(2026, 10, 8, 6, 40, tzinfo=timezone.utc), 25 * 60),         # 07:05 UTC
    (datetime(2026, 10, 8, 7, 3, tzinfo=timezone.utc), 2 * 60),  # first minutes of the new day: wait for 07:05, not a day
    (datetime(2026, 10, 8, 14, 28, tzinfo=timezone.utc), (16 * 60 + 37) * 60),
])
def test_nasta_dag_ar_fem_minuter_in_i_nasta_kvotdygn(nu, sekunder):
    import app.main as main

    assert main.till_ny_dag(nu) == sekunder


def test_dagens_renderingar_raknas_fran_disken(tmp_path):
    """A restart on the same day keeps counting (G: 60 + 60 > Google's 100)."""
    import os
    import time as _time
    import app.main as main

    for i in range(3):
        (tmp_path / f"gemini_{i}.pcm").write_bytes(b"x")
    gammal = tmp_path / "gemini_igar.pcm"
    gammal.write_bytes(b"x")
    os.utime(gammal, (_time.time() - 2 * 86400,) * 2)
    (tmp_path / "xai_0.pcm").write_bytes(b"x")
    assert main.renderat_idag("gemini", str(tmp_path)) == 3


@pytest.mark.asyncio
async def test_omstart_samma_dag_fortsatter_rakna(monkeypatch):
    import app.main as main

    vantat = []

    async def sov(s):
        vantat.append("imorgon" if s == 86400 else s)

    tid = [0.0]
    monkeypatch.setattr(main.asyncio, "sleep", sov)
    monkeypatch.setattr(main.time, "monotonic", lambda: tid[0])
    monkeypatch.setattr(main, "till_ny_dag", lambda: 86400)
    monkeypatch.setattr(main, "renderat_idag", lambda provider: 59)

    class Agent:
        async def _ack_clip(self, provider, text, fallback=True):
            tid[0] += 1.0
            return b"pcm"

    await main.Application._warm_klockan(Agent(), "gemini", takt_s=8.0)
    assert vantat[:2] == [8.0, "imorgon"]  # 59 already today: one more, then tomorrow
