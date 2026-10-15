"""Björn's text comes from Core through comms, and the speaker never goes without one (raawr US-056)."""
import asyncio
import logging
import time

import httpx
import pytest

from app import bjorn_karna
from app.bjorn_karna import INBYGGD, Karna, karna_url

COMMS = "http://comms.test:3500/kanal/rost/kontoret"
SJAL = "Du är Björn. Rakt och varmt.\nLås aldrig upp en dörr utan att någon uttryckligen bett om just det.\n\nTILLÄGG-ROST"
SJAL += "\n" + ("Konkret svar först, och hitta aldrig på värden. " * 6).strip()  # the real text is long; a short one is refused


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HA_API_URL", COMMS + "/api")
    monkeypatch.setenv("COMMS_NYCKEL", "kontorets-nyckel")
    monkeypatch.delenv("BJORN_KARNA_URL", raising=False)


def _comms(monkeypatch, svar):
    """A stand-in comms: `svar(request)` returns an httpx.Response, or raises."""
    sedda = []

    def handler(request):
        sedda.append(request)
        return svar(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    return sedda


def _ok(request):
    return httpx.Response(200, json={"vag": "rost", "text": SJAL, "halsa": "ok"})


def test_adressen_ligger_bredvid_api_inte_under_det(monkeypatch):
    assert karna_url() == COMMS + "/bjorn"
    monkeypatch.setenv("BJORN_KARNA_URL", "http://x/bjorn")
    assert karna_url() == "http://x/bjorn"
    monkeypatch.delenv("HA_API_URL")
    monkeypatch.delenv("BJORN_KARNA_URL")
    assert karna_url() == ""


@pytest.mark.asyncio
async def test_ac1_hamtad_text_med_kontorets_nyckel(monkeypatch):
    sedda = _comms(monkeypatch, _ok)
    text, kalla = await Karna("").text()
    assert (text, kalla) == (SJAL, "hämtad")
    assert str(sedda[0].url) == COMMS + "/bjorn" and sedda[0].headers["x-raawr-nyckel"] == "kontorets-nyckel"


@pytest.mark.asyncio
async def test_ac3a_den_hamtade_texten_vinner_over_miljon_och_en_minut_ateranvands(monkeypatch):
    sedda = _comms(monkeypatch, _ok)
    k = Karna("GAMMAL TEXT UR MILJÖN")
    assert (await k.text()) == (SJAL, "hämtad")
    assert (await k.text()) == (SJAL, "hämtad") and len(sedda) == 1


@pytest.mark.asyncio
async def test_ac3b_ingen_miljovariabel_alls_hamtad_text_funkar_andå(monkeypatch):
    _comms(monkeypatch, _ok)
    assert (await Karna("").text())[1] == "hämtad"


@pytest.mark.asyncio
async def test_ac3c_comms_nere_minne_sedan_miljo_sedan_inbyggd(monkeypatch):
    nere = {"v": False}

    def svar(request):
        if nere["v"]:
            raise httpx.ConnectError("refused")
        return _ok(request)

    _comms(monkeypatch, svar)
    k = Karna("MILJÖ")
    assert (await k.text())[1] == "hämtad"
    nere["v"] = True
    k._hamtad_ts -= 3600  # the minute is over: ask again, and fail
    assert (await k.text()) == (SJAL, "minne")
    assert (await Karna("MILJÖ").text()) == ("MILJÖ", "miljö")
    assert (await Karna("").text()) == (INBYGGD, "inbyggd")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kropp", [(404, "nej"), (500, "x"), (200, "{}"), (200, '{"text": "  "}'), (200, "inte json")])
async def test_ett_svar_utan_text_ar_ett_misslyckande_inte_en_tom_sjal(monkeypatch, status, kropp):
    _comms(monkeypatch, lambda r: httpx.Response(status, content=kropp))
    assert (await Karna("MILJÖ").text()) == ("MILJÖ", "miljö")


@pytest.mark.asyncio
async def test_en_hangande_comms_haller_inte_upp_sessionen_och_fragas_inte_igen_direkt(monkeypatch):
    monkeypatch.setattr(bjorn_karna, "TIMEOUT_S", 0.2)
    anrop = []

    async def hang(request):
        anrop.append(1)
        await asyncio.sleep(30)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(hang)}))
    k = Karna("MILJÖ")
    t0 = time.monotonic()
    assert (await k.text()) == ("MILJÖ", "miljö")
    assert time.monotonic() - t0 < 1.0
    t1 = time.monotonic()
    assert (await k.text()) == ("MILJÖ", "miljö")  # the pause: not asked again for PAUS_S
    assert time.monotonic() - t1 < 0.05 and len(anrop) == 1


@pytest.mark.asyncio
async def test_sessionen_far_texten_och_sprakladset_sist_och_loggen_sager_kallan(monkeypatch, caplog):
    from tests.test_provider_selection import _bare_app
    from app.main import SPRAKLAS

    _comms(monkeypatch, _ok)
    app = _bare_app("gemini")
    with caplog.at_level(logging.INFO):
        await app._uppdatera_karna()
    assert app.instructions == SJAL
    assert app._instructions().startswith(SJAL + SPRAKLAS)  # the language lock follows the text, as before
    assert "fortsätt där du var" not in app._instructions()  # AVBROTT is per room (_instructions(rum))
    assert any("källa=hämtad" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_inget_unset_comms_ingen_miljo_hogtalaren_ar_ändå_björn(monkeypatch, caplog):
    from tests.test_provider_selection import _bare_app

    monkeypatch.delenv("HA_API_URL")
    app = _bare_app("gemini")
    with caplog.at_level(logging.INFO):
        await app._uppdatera_karna()
    assert app.instructions == INBYGGD and any("källa=inbyggd" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_comms_reservtext_nar_core_ar_nere_ar_ett_misslyckande_inte_en_sjal(monkeypatch):
    """A's FAIL on #52: comms answers 200 with its short reserve soul and halsa 'reserv' when Core is down.
    That must fall to the environment's text (or the memory), not replace the full soul."""
    reserv = httpx.Response(200, json={"vag": "rost", "text": SJAL, "halsa": "reserv"})
    ok = {"v": False}
    _comms(monkeypatch, lambda r: _ok(r) if ok["v"] else reserv)
    assert (await Karna("MILJÖ").text()) == ("MILJÖ", "miljö")
    ok["v"] = True
    k = Karna("MILJÖ")
    assert (await k.text())[1] == "hämtad"
    ok["v"] = False
    k._hamtad_ts -= 3600
    assert (await k.text()) == (SJAL, "minne")


@pytest.mark.asyncio
async def test_en_for_kort_text_ar_inte_en_sjal(monkeypatch):
    _comms(monkeypatch, lambda r: httpx.Response(200, json={"vag": "rost", "text": "ok", "halsa": "ok"}))
    assert (await Karna("MILJÖ").text()) == ("MILJÖ", "miljö")


@pytest.mark.asyncio
async def test_bjorn_karna_av_hoppar_over_hamtningen(monkeypatch):
    sedda = _comms(monkeypatch, _ok)
    monkeypatch.setenv("BJORN_KARNA", "av")
    assert (await Karna("MILJÖ").text()) == ("MILJÖ", "miljö")
    assert sedda == []


def test_avbrott_bara_i_rum_dar_motorn_tar_turerna(monkeypatch):
    """US-060 AC-8: a bare back-channel does not end his reply -- but only where the engine runs the turns. In the
    kitchen the mic is shut while he speaks, and a lone 'ja' must not make him go on (B on voicepe #74)."""
    from app.main import avbrott_for
    monkeypatch.setenv("GEMINI_EGEN_TUR", "kontoret")
    assert "fortsätt där du var" in avbrott_for("kontoret")
    assert avbrott_for("koket") == ""
    monkeypatch.setenv("GEMINI_EGEN_TUR", "0")
    assert avbrott_for("kontoret") == ""


def test_avbrott_nar_gemini_vid_varje_uppkoppling(monkeypatch):
    """What Gemini is actually given: gemini_live._connect renders instructions_provider on every connect, the first
    too, so AVBROTT has to be in _instructions(rum) itself (B, F2 on voicepe #74: it was only in options.instructions)."""
    from tests.test_provider_selection import _bare_app
    from app.providers import GEMINI, build_service
    from test_gemini_main_engine import NATIVE, _connect_config
    from test_gemini_provider import OPENAI_SHAPE, _options
    monkeypatch.setenv("GEMINI_EGEN_TUR", "kontoret")
    app = _bare_app("gemini")
    for rum, med in (("kontoret", True), ("koket", False)):
        o = _options(model=NATIVE)
        o.instructions = app.provider_options(GEMINI, rum).instructions
        service = build_service(GEMINI, o, OPENAI_SHAPE)
        service.instructions_provider = app._instruktionskalla(rum)   # what main.py wires
        assert ("fortsätt där du var" in _connect_config(service).system_instruction) is med, rum
        service._session = None   # a reconnect renders it again
        assert ("fortsätt där du var" in _connect_config(service).system_instruction) is med, rum


def test_den_inbyggda_sjalen_laser_aldrig_upp_bara_panelen_gor_det():
    """raawr E-07/D-6 (Henrik 2026-10-04): upplåsning, larm av och garage öppna bara på panelen, aldrig med rösten.
    Den gamla meningen ('utan att någon uttryckligen bett om just det') sa att rösten fick låsa upp på begäran."""
    assert "bara på panelen eller i Hem-appen" in INBYGGD and "Lås aldrig upp" in INBYGGD
    assert "uttryckligen bett" not in INBYGGD
