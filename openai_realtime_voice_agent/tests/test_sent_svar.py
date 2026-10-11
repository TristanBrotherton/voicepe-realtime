"""US-051: the speaker asks before it reads a late answer. No sound, no network: fake speech, fake comms."""
import asyncio

import pytest

from app import sent_svar
from app.raw_audio_serializer import RawAudioSerializer

TAL = (b"\x00\x30" * 8000)       # 0.5 s loud (about -20 dBFS)
TYST = bytes(32000)             # 1 s of silence
NOTIS = {"rum": "kontoret", "result_id": "r1", "agent": "Sixten", "amne": "hockeyresultat",
         "amnesord": ["hockey"], "created_at": "2026-10-11T08:00:00+02:00"}


class Ser:
    fraga_tap = None


class Varld:
    """Everything SentSvar leans on, recorded."""

    def __init__(self, hort="ja", samtal=False, stt=True):
        self.ser, self.sagt, self.hamtat, self.hort, self.samtal = Ser(), [], [], hort, samtal
        self.stt_anrop = 0

        async def tala(text, rum):
            self.sagt.append(text)
            if len(self.sagt) == 1:  # the question is out: the device now streams the answer
                asyncio.get_running_loop().call_soon(self.svara)

        async def stt_(pcm):
            self.stt_anrop += 1
            return self.hort

        async def hamta(rid):
            self.hamtat.append(rid)
            return "Resultatet blev tre två."

        self.s = sent_svar.SentSvar(tala=tala, stt=stt_ if stt else None,
                                    enhet=lambda rum: (self.ser, self.samtal) if rum == "kontoret" else None, hamta=hamta)
        self.talar = True

    def svara(self):
        tap = self.ser.fraga_tap
        assert tap is not None, "the mic must be taken before the question is spoken"
        if self.talar:
            tap(TAL)
        for _ in range(2):
            tap(TYST)


async def kor(v, notis=NOTIS):
    ut = await v.s.notis(notis)
    await asyncio.gather(*list(v.s._uppgifter))
    return ut


@pytest.fixture(autouse=True)
def dag(monkeypatch):
    monkeypatch.setenv("SENT_SVAR_NU", "12:00")
    monkeypatch.setattr(sent_svar, "LYSSNA_MAX_S", 2.0)


@pytest.mark.parametrize("ord_", ["ja", "Ja tack.", "kör", "gärna", "okej", "visst", "läs", "berätta"])
def test_ja_orden(ord_):
    assert sent_svar.tolka(ord_) == "ja"


@pytest.mark.parametrize("ord_", ["nej", "Nej tack!", "inte nu", "senare", "strunta i det"])
def test_nej_orden(ord_):
    assert sent_svar.tolka(ord_) == "nej"


@pytest.mark.parametrize("ord_", ["Ha", "ja men vänta", "vad blir det för väder", "", None])
def test_allt_annat_ar_inget_svar(ord_):
    assert sent_svar.tolka(ord_) is None


def test_ac1_fragan_talas_och_svaret_inte_hamtas_utan_ja():
    v = Varld(hort="nej")
    ut = asyncio.run(kor(v))
    assert ut == {"fragar": True}
    assert v.sagt == ["Sixten har mer om hockeyresultat. Vill du höra det?"] and v.hamtat == []


def test_ac2_ja_hamtar_och_laser():
    v = Varld(hort="Ja tack.")
    asyncio.run(kor(v))
    assert v.hamtat == ["r1"] and v.sagt[-1] == "Resultatet blev tre två." and len(v.sagt) == 2


def test_ac2_ja_men_svaret_finns_inte_kvar():
    v = Varld(hort="ja")

    async def borta(rid):
        return None
    v.s._hamta = borta
    asyncio.run(kor(v))
    assert v.sagt[-1] == "Det svaret finns inte kvar." and len(v.sagt) == 2


def test_ac2_tystnad_lasar_inget_och_stt_hors_inte():
    v = Varld(hort="ja")
    v.talar = False
    asyncio.run(kor(v))
    assert v.hamtat == [] and v.stt_anrop == 0 and len(v.sagt) == 1
    assert v.s.sparade["kontoret"][0]["result_id"] == "r1"  # stays for the morning question


def test_ac2_ha_ar_inget_svar():
    v = Varld(hort="Ha")
    asyncio.run(kor(v))
    assert v.hamtat == [] and len(v.sagt) == 1


@pytest.mark.parametrize("klockan", ["00:00", "03:00", "06:59"])
def test_ac3_natt_talar_inget_och_sparar(monkeypatch, klockan):
    monkeypatch.setenv("SENT_SVAR_NU", klockan)
    v = Varld()
    ut = asyncio.run(kor(v))
    assert ut["fragar"] is False and ut["orsak"] == "natt" and v.sagt == [] and v.hamtat == []
    assert [n["result_id"] for n in v.s.sparade["kontoret"]] == ["r1"]


def test_ac3_klockan_sju_far_fraga(monkeypatch):
    monkeypatch.setenv("SENT_SVAR_NU", "07:00")
    assert asyncio.run(kor(Varld(hort="nej")))["fragar"] is True


def test_ac3_pagaende_samtal_frågas_inte():
    v = Varld(samtal=True)
    ut = asyncio.run(kor(v))
    assert ut["fragar"] is False and ut["orsak"] == "samtal" and v.sagt == []
    assert "r1" in [n["result_id"] for n in v.s.sparade["kontoret"]]


def test_okand_enhet_och_trasig_kropp():
    v = Varld()
    assert asyncio.run(kor(v, {**NOTIS, "rum": "badrummet"}))["fragar"] is False
    assert asyncio.run(v.s.notis({"nope": 1}))["fragar"] is False


def test_utan_lokal_stt_fragas_inget():
    v = Varld(stt=False)
    assert asyncio.run(kor(v))["orsak"] == "ingen_stt"


def test_ac5_micken_tas_fran_motorn_och_lamnas_tillbaka():
    ser = RawAudioSerializer("kontoret")
    heard = []
    ser.fraga_tap = heard.append

    async def inbound():
        return await ser.deserialize(TAL)

    assert asyncio.run(inbound()) is None and heard == [TAL]
    ser.fraga_tap = None
    assert asyncio.run(inbound()) is not None


def test_ac5_flush_avslutar_lyssnandet():
    async def go():
        ly = sent_svar.Lyssnare()
        ly.mata(TAL)
        ly.mata(None)
        return await ly.lyssna(5.0), ly.tal
    pcm, tal = asyncio.run(go())
    assert pcm == TAL and tal is True


def test_tapen_ar_borta_efter_fragan():
    v = Varld(hort="nej")
    asyncio.run(kor(v))
    assert v.ser.fraga_tap is None
