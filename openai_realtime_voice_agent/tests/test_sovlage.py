"""Sleep mode (raawr INKAST 2026-10-04): connected to the cloud only during a conversation.

The cost: xAI bills per connected minute, and the agent reconnected a quiet
session every 900 s, all night. Each test here fails on 0.25.6.
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import ErrorFrame
from pipecat.processors.frame_processor import FrameDirection

from app.providers import GEMINI, OPENAI, XAI, ProviderOptions, build_service
from app.providers.sovlage import Budget, SovlageMixin
from app.websocket_handler import ConnectionRecovery


@pytest.fixture(autouse=True)
def egen_budget(tmp_path, monkeypatch):
    """Never the real ledger on the box the tests run on."""
    b = Budget(path=str(tmp_path / "moln.json"), today=lambda: "2026-10-04")
    monkeypatch.setattr(SovlageMixin, "budget", b)
    return b


def _service(provider):
    opts = ProviderOptions(api_key="k", model="gpt-realtime-2", voice="cedar", instructions="Du är Björn.")
    if provider == GEMINI:
        opts = ProviderOptions(api_key="k", model="gemini-live-2.5-flash-preview", voice="Charon",
                               instructions="Du är Björn.")
    return build_service(provider, opts, [])


@pytest.mark.parametrize("provider", [OPENAI, XAI, GEMINI])
def test_varje_motor_borjar_sovande_och_kopplar_inte_upp(provider, monkeypatch):
    service = _service(provider)
    assert isinstance(service, SovlageMixin)
    assert service.sover is True
    connected = []
    base = type(service).__mro__[type(service).__mro__.index(SovlageMixin) + 1]
    monkeypatch.setattr(base, "_connect", lambda self, *a, **k: connected.append(1), raising=False)
    asyncio.run(service._connect())  # what pipecat's start() calls
    assert connected == []


def test_av_med_moln_sovlage_0(monkeypatch):
    monkeypatch.setenv("MOLN_SOVLAGE", "0")
    assert _service(XAI).sover is False


class Fake(SovlageMixin):
    def __init__(self):
        self.sover = True
        self.calls = []

    async def _ateranslut(self, forut):
        self.calls.append(("upp", forut))

    async def _disconnect(self):
        self.calls.append("ner")


@pytest.mark.asyncio
async def test_vakna_kopplar_upp_en_gang_och_sova_kopplar_ner():
    s = Fake()
    assert await s.vakna() is True
    assert await s.vakna() is False  # already awake: no second connect
    assert await s.sova("tyst") is True
    assert await s.sova("tyst") is False
    assert await s.vakna() is True
    # The second wake keeps the conversation (OpenAI: reset_conversation re-seeds it).
    assert s.calls == [("upp", False), "ner", ("upp", True)]


def _recovery(service, phase="idle"):
    class Phase:
        pass

    p = Phase()
    p.phase = phase
    r = ConnectionRecovery(service, phase_emitter=p, provider=XAI)
    return r


@pytest.mark.asyncio
async def test_fel_medan_den_sover_rapporteras_aldrig_och_ateransluts_aldrig():
    s = Fake()
    r = _recovery(s)
    r._route_error = AsyncMock()
    r.push_frame = AsyncMock()
    r._refresh_task = r._sov_task = object()  # no background loops in this test
    await r.process_frame(ErrorFrame(error="realtime receive loop ended — connection closed"),
                          FrameDirection.UPSTREAM)
    r._route_error.assert_not_awaited()
    await r.force_reconnect("wedge")
    assert r._recover_task is None


def test_tyst_nog_forst_efter_sov_efter_s(monkeypatch):
    monkeypatch.setenv("SOV_EFTER_S", "30")
    s = Fake()
    s.sover = False
    r = _recovery(s)
    now = time.monotonic()
    r._last_input_audio = r._last_wake = now - 31
    assert r._tyst_nog(now) is True
    r._last_input_audio = now - 5  # he spoke 5 s ago
    assert r._tyst_nog(now) is False
    r._last_input_audio = now - 31
    r._last_wake = now - 5  # woke 5 s ago
    assert r._tyst_nog(now) is False
    r._last_wake = now - 31
    r._phase_emitter.phase = "replying"  # still answering
    assert r._tyst_nog(now) is False


@pytest.mark.asyncio
async def test_sovloopen_kopplar_ner_efter_samtalet(monkeypatch):
    monkeypatch.setenv("SOV_EFTER_S", "5")
    s = Fake()
    s.sover = False
    r = _recovery(s)
    r.SOV_CHECK_S = 0.01
    r._last_input_audio = r._last_wake = time.monotonic() - 10
    task = asyncio.create_task(r._sov_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    assert s.sover is True and s.calls == ["ner"]


@pytest.mark.asyncio
async def test_vakningen_vacker_motorn():
    s = Fake()
    r = _recovery(s)
    await r.vakna()
    assert s.sover is False and s.calls == [("upp", False)]


@pytest.mark.asyncio
async def test_openai_lasloopens_slut_nar_den_sover_ar_inget_fel():
    service = _service(OPENAI)
    service.sover = True
    service.push_error = AsyncMock()

    class Ws:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    service._websocket = Ws()
    await asyncio.wait_for(service._receive_task_handler(), 1)
    service.push_error.assert_not_awaited()


# --- the real engines' reconnect and the wiring the review asked for ---

@pytest.mark.asyncio
async def test_openai_forsta_vakningen_ansluter_senare_aterskapar_samtalet():
    service = _service(OPENAI)
    service._connect = AsyncMock()
    service.reset_conversation = AsyncMock()
    await service._ateranslut(False)
    service._connect.assert_awaited_once()
    service.reset_conversation.assert_not_awaited()
    await service._ateranslut(True)
    service.reset_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_gemini_senare_vakning_ateruptar_med_handtaget():
    service = _service(GEMINI)
    service._connect = AsyncMock()
    service._session_resumption_handle = "h1"
    await service._ateranslut(False)
    service._connect.assert_awaited_with(None)
    await service._ateranslut(True)
    service._connect.assert_awaited_with("h1")


class _Klar:
    """A finished connection task, as pipecat leaves it after a refused connect."""
    def done(self):
        return True


@pytest.mark.asyncio
async def test_gemini_avvisat_handtag_ger_ett_nytt_samtal_i_samma_vakning():
    """Live 2026-10-07 12:16: Google answered the resume with 1011 in 0.9 s.
    The wake was lost, and the handle stayed for the next wake too."""
    service = _service(GEMINI)
    service._session_resumption_handle = "gammalt"
    anrop = []

    async def connect(handle):
        anrop.append(handle)
        if handle is None:
            service._session = object()
        else:
            service._connection_task = _Klar()

    service._connect = connect
    await service._ateranslut(True)
    assert anrop == ["gammalt", None]
    assert service._session_resumption_handle is None


@pytest.mark.asyncio
async def test_gemini_langsam_aterupptagning_provas_inte_om():
    """No answer at all (the net, not Google) keeps the one attempt: a second
    connect would only add another wait to the wake."""
    service = _service(GEMINI)
    service._session_resumption_handle = "h1"
    service._connect = AsyncMock()
    service._connection_task = None
    service._ar_uppkopplad = AsyncMock(return_value=False)
    await service._ateranslut(True)
    service._connect.assert_awaited_once_with("h1")


@pytest.mark.asyncio
async def test_vakning_fran_enheten_vacker_motorn_genom_ledningen():
    from test_bana0 import _koppling
    import json

    handler, connection, service, sent = _koppling(None)
    connection.recovery.vakna = AsyncMock()
    await connection.serializer.deserialize(json.dumps({"type": "wake"}))
    connection.recovery.vakna.assert_awaited_once()


@pytest.mark.asyncio
async def test_forsta_ramen_startar_sovloopen():
    s = Fake()
    r = _recovery(s)
    r.push_frame = AsyncMock()
    r._refresh_task = object()
    await r.process_frame(ErrorFrame(error="x"), FrameDirection.UPSTREAM)
    assert r._sov_task is not None
    r._sov_task.cancel()


@pytest.mark.asyncio
async def test_vakning_som_hanger_haller_inte_enheten():
    class Hang(Fake):
        async def vakna(self):
            await asyncio.sleep(10)

    r = _recovery(Hang())
    r.VAKNA_TIMEOUT_S = 0.05
    t0 = time.monotonic()
    await r.vakna()
    assert time.monotonic() - t0 < 1.0


@pytest.mark.asyncio
async def test_ateranslutning_mitt_i_samtal_vacker_nya_motorn():
    from pipecat.frames.frames import StartFrame

    s = Fake()
    r = _recovery(s)
    r.push_frame = AsyncMock()
    r._refresh_task = r._sov_task = object()
    r.vakna_vid_start = True
    from unittest.mock import patch
    with patch("pipecat.processors.frame_processor.FrameProcessor.process_frame", AsyncMock()):
        await r.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.7)
    assert s.sover is False


# --- the daily budget (0.26.1): a bug elsewhere costs at most N minutes a day ---

@pytest.mark.asyncio
async def test_sova_raknar_uppkopplad_tid(egen_budget):
    s = Fake()
    await s.vakna()
    s._uppkopplad_sedan -= 90  # 90 s connected
    await s.sova("tyst")
    assert 89 < egen_budget.anvant() < 95


@pytest.mark.asyncio
async def test_slut_budget_vaknar_inte(egen_budget, monkeypatch):
    monkeypatch.setenv("MOLN_MAX_MINUTER_PER_DAG", "1")
    egen_budget.lagg_till(60)
    s = Fake()
    assert await s.vakna() is False
    assert s.sover is True and s.calls == []


def test_budgeten_overlever_omstart_och_nollas_nasta_dag(tmp_path):
    path = str(tmp_path / "m.json")
    Budget(path=path, today=lambda: "2026-10-04").lagg_till(120)
    assert Budget(path=path, today=lambda: "2026-10-04").anvant() == 120
    assert Budget(path=path, today=lambda: "2026-10-05").anvant() == 0


@pytest.mark.parametrize("fel", ["borta", "trasig"])
def test_gardagens_kand_foljer_inte_med_over_midnatt(tmp_path, fel):
    """Same Budget across midnight. A missing or torn ledger must not reuse
    yesterday's in-memory total: that total has no date of its own."""
    dag = {"idag": "2026-10-04"}
    path = tmp_path / "m.json"
    b = Budget(path=str(path), today=lambda: dag["idag"])
    b.lagg_till(3000)
    dag["idag"] = "2026-10-05"
    if fel == "borta":
        path.unlink()
    else:
        path.write_text("{trasig")
    assert b.anvant() == 0


@pytest.mark.asyncio
async def test_sovloopen_kopplar_ner_nar_budgeten_tar_slut(egen_budget, monkeypatch):
    monkeypatch.setenv("MOLN_MAX_MINUTER_PER_DAG", "1")
    s = Fake()
    await s.vakna()
    egen_budget.lagg_till(60)  # the other speaker used the rest
    r = _recovery(s)
    r.SOV_CHECK_S = 0.01
    r._last_input_audio = r._last_wake = time.monotonic()  # mid-conversation
    task = asyncio.create_task(r._sov_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    assert s.sover is True


# --- a hard cap per session (0.26.3, Henrik 2026-10-04) ---

@pytest.mark.asyncio
async def test_maxtid_kopplar_ner_aven_nar_ljud_fortsatter(monkeypatch):
    monkeypatch.setenv("VOICE_SESSION_MAX_SECONDS", "60")
    s = Fake()
    await s.vakna()
    s._uppkopplad_sedan -= 61  # connected for 61 s
    r = _recovery(s, phase="replying")
    r.SOV_CHECK_S = 0.01
    r._last_input_audio = r._last_wake = time.monotonic()  # audio still flowing
    task = asyncio.create_task(r._sov_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    assert s.sover is True and s.calls[-1] == "ner"
    assert s.calls.count(("upp", False)) == 1  # nothing reconnected it


def test_maxtid_standard_tio_minuter(monkeypatch):
    from app.providers.sovlage import max_sekunder_per_samtal
    monkeypatch.delenv("VOICE_SESSION_MAX_SECONDS", raising=False)
    assert max_sekunder_per_samtal() == 600
    monkeypatch.setenv("VOICE_SESSION_MAX_SECONDS", "x")
    assert max_sekunder_per_samtal() == 600


@pytest.mark.asyncio
async def test_misslyckad_uppkoppling_somnar_igen_och_nasta_vakning_forsoker():
    """Live 2026-10-04 (US-018 AC-3): offline connect failed, the service
    counted as awake, and the question after the net came back went nowhere."""
    class Nere(Fake):
        uppe = False

        async def _ar_uppkopplad(self):
            return self.uppe

    s = Nere()
    assert await s.vakna() is False
    assert s.sover is True
    s.uppe = True  # the net is back
    assert await s.vakna() is True
    # "ner": the failed wake tears down its unfinished connect (0.27.6).
    assert s.calls == [("upp", False), "ner", ("upp", False)]


@pytest.mark.asyncio
async def test_gemini_vantar_pa_sessionen_eller_dess_fel():
    service = _service(GEMINI)
    service._session = None

    class Klar:
        def done(self):
            return True

    service._connection_task = Klar()
    assert await service._ar_uppkopplad(timeout=1) is False
    service._session = object()
    assert await service._ar_uppkopplad(timeout=1) is True


async def _kor_sovloopen(r):
    r.SOV_CHECK_S = 0.01
    task = asyncio.create_task(r._sov_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_vakning_utan_tal_somnar_aven_om_fasen_fastnat():
    """A wake nobody answers must drop the engine even if the phase stuck.

    The device streams the open mic after a wake, silence included, so a
    fresh audio timestamp is not speech. Speech is a real utterance since
    the wake (_speech_since_wake).
    """
    s = Fake()
    s.sover = False
    r = _recovery(s, phase="listening")
    r._phase_emitter._speech_since_wake = False
    now = time.monotonic()
    r._last_wake = now - (r.VAKNA_TIMEOUT_S + 1)
    r._last_input_audio = now  # mic open, nobody spoke
    await _kor_sovloopen(r)
    assert s.sover is True and s.calls == ["ner"]


@pytest.mark.asyncio
async def test_pagande_samtal_bryts_inte_av_vakningstaket():
    """The user spoke after the wake: a stuck reply is still their conversation."""
    s = Fake()
    s.sover = False
    r = _recovery(s, phase="replying")
    r._phase_emitter._speech_since_wake = True
    now = time.monotonic()
    r._last_wake = now - (r.VAKNA_TIMEOUT_S + 1)
    r._last_input_audio = now
    await _kor_sovloopen(r)
    assert s.sover is False and s.calls == []


@pytest.mark.asyncio
async def test_foljdfonstret_som_stangs_ar_ingen_vakning_utan_tal():
    """Live 2026-10-07 (satellite stand-in on core): a question, a reply, and
    the follow-up window closing (device 'flush') put the engine to sleep 3 s
    later as 'wake without speech', not after 30 s of quiet. The flush clears
    the dangling-VAD flag; it is not a wake, and the user did speak."""
    from pipecat.frames.frames import UserStartedSpeakingFrame

    from app.phase_emitter import PhaseEmitter

    async def tyst(_value):
        pass

    async def ingen_vidare(*_a, **_k):
        pass

    pe = PhaseEmitter(tyst, idle_debounce_s=0)
    pe.push_frame = ingen_vidare
    s = Fake()
    s.sover = False
    r = ConnectionRecovery(s, phase_emitter=pe, provider=GEMINI)

    pe.note_device_wake()  # the wake
    await pe.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    pe.note_wake()  # the follow-up window closed: flush

    now = time.monotonic()
    r._last_wake = now - (r.VAKNA_TIMEOUT_S + 1)
    assert r._vakning_utan_tal(now) is False

    pe.note_device_wake()  # a new wake nobody answers still counts
    assert r._vakning_utan_tal(now) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("handtag", ["gammalt", None])
async def test_en_handskakning_som_inte_hann_klart_rivs_vid_vakningens_slut(handtag, monkeypatch):
    """G's review of 0.27.6 (fynd 1): a handshake still running when the wake
    gives up came up afterwards as a session outside every cap - asleep by the
    books, never booked, never put to sleep. Same for a slow first handshake
    (handtag None: no resume, the net is slow). The wake must tear it down."""
    monkeypatch.setenv("MOLN_LEDGER", "/dev/null/ingen")
    service = _service(GEMINI)
    service.budget = Budget(path=str(__import__("tempfile").mktemp()))
    service._session_resumption_handle = handtag
    service._vaknat_forut = True
    anrop = []

    async def kommer_sent():
        await asyncio.sleep(10)
        service._session = object()

    class Klar:
        def done(self):
            return True

    async def connect(h):
        anrop.append(h)
        if h is not None:
            service._connection_task = Klar()  # Google said no at once
        else:
            service._connection_task = asyncio.create_task(kommer_sent())

    async def disconnect():
        task = service._connection_task
        if task is not None and hasattr(task, "cancel"):
            task.cancel()
        service._connection_task = None
        service._session = None

    service._connect = connect
    service._disconnect = disconnect
    t0 = time.monotonic()
    assert await service.vakna() is False
    assert time.monotonic() - t0 < 4.6  # inside ConnectionRecovery.VAKNA_TIMEOUT_S
    await asyncio.sleep(0.05)
    assert service._connection_task is None
    assert service._session is None and service.sover is True


# --- US-032 AC-4: the ledger counts every connected minute ---

def _oppen_sedan(s, sekunder):
    """Pretend the session connected `sekunder` ago (and nothing is booked yet)."""
    s._uppkopplad_sedan = time.monotonic() - sekunder


@pytest.mark.asyncio
async def test_oppen_session_bokfors_varje_minut_och_aldrig_dubbelt(egen_budget):
    s = Fake()
    await s.vakna()
    _oppen_sedan(s, 150)
    s.bokfor_lopande()
    assert 149 < egen_budget.anvant() < 152
    s.bokfor_lopande()  # under a minute since: nothing
    assert egen_budget.anvant() < 152
    s._bokfort_till -= 20  # 20 s more, then the session ends
    await s.sova("tyst")
    assert 169 < egen_budget.anvant() < 172  # 150 + 20, once
    assert s.over_maxtid() is False


@pytest.mark.asyncio
async def test_lopande_bokforing_rubbar_inte_maxtiden(monkeypatch):
    monkeypatch.setenv("VOICE_SESSION_MAX_SECONDS", "600")
    s = Fake()
    await s.vakna()
    _oppen_sedan(s, 601)
    s.bokfor_lopande()
    assert s.over_maxtid() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fas", [0.0, 2.5, 4.99])
async def test_kill_9_med_bada_hogtalarna_tappar_hogst_en_minut(egen_budget, monkeypatch, fas):
    """Both speakers open, the sleep loop's 5 s tick, kill -9 at the worst moment
    (just before a booking): the ledger in a new process lacks at most 60 s of
    what the journal shows (G's review of PR #17: 120 s with a 60 s step)."""
    import app.providers.sovlage as sovlage

    nu = [1000.0]
    monkeypatch.setattr(sovlage.time, "monotonic", lambda: nu[0])
    kontoret, koket = Fake(), Fake()
    await kontoret.vakna()
    nu[0] += fas
    await koket.vakna()
    start = {kontoret: 1000.0, koket: 1000.0 + fas}
    varst = 0.0
    for _ in range(int(300 / 0.01)):  # 300 s in 10 ms steps, a tick every 5 s
        nu[0] = round(nu[0] + 0.01, 2)
        if round(nu[0] * 100) % 500 == 0:
            for s in (kontoret, koket):
                s.bokfor_lopande()
        if round(nu[0] * 100) % 500 == 499:  # kill -9 now, just before a tick:
            journalen = sum(nu[0] - t for t in start.values())  # both full sessions
            varst = max(varst, journalen - egen_budget.anvant())  # vs what is booked
    ny_process = Budget(path=egen_budget.path, today=lambda: "2026-10-04")
    assert ny_process.anvant() == egen_budget.anvant()
    assert varst <= 60.0


@pytest.mark.asyncio
async def test_bokford_tid_raknas_inte_tva_ganger_mot_taket(egen_budget, monkeypatch):
    monkeypatch.setenv("MOLN_MAX_MINUTER_PER_DAG", "60")
    s = Fake()
    await s.vakna()
    _oppen_sedan(s, 40 * 60)
    s.bokfor_lopande()  # 40 min in the ledger, still open
    assert s.over_budget() is False  # 40, not 40 + 40


@pytest.mark.asyncio
async def test_tva_hogtalare_35_och_30_min_nekar_nasta_vakning(egen_budget, monkeypatch):
    monkeypatch.setenv("MOLN_MAX_MINUTER_PER_DAG", "60")
    kontoret, koket = Fake(), Fake()
    await kontoret.vakna()
    await koket.vakna()
    _oppen_sedan(kontoret, 35 * 60)
    _oppen_sedan(koket, 30 * 60)
    kontoret.bokfor_lopande()
    assert koket.over_budget() is True  # same cap: 35 booked + 30 open
    await kontoret.sova("tyst")
    await koket.sova("tyst")
    assert await Fake().vakna() is False


def test_dodad_mellan_de_tva_skrivningarna_ger_det_nya_vardet(tmp_path):
    """kill -9 after the ledger was replaced but before its copy: the higher wins."""
    path = tmp_path / "m.json"
    Budget(path=str(path), today=lambda: "2026-10-04").lagg_till(1234)
    path.write_text('{"2026-10-04": 1300.0}')  # the new value; the copy still says 1234
    assert Budget(path=str(path), today=lambda: "2026-10-04").anvant() == 1300.0


@pytest.mark.parametrize("fel", ["trasig", "tom", "borta"])
def test_trasig_minutfil_i_ny_process_ger_aldrig_lagre_varde(tmp_path, fel):
    path = tmp_path / "m.json"
    Budget(path=str(path), today=lambda: "2026-10-04").lagg_till(1234)
    if fel == "trasig":
        path.write_text('{"2026-10-04": 12')
    elif fel == "tom":
        path.write_text("")
    else:
        path.unlink()
    assert Budget(path=str(path), today=lambda: "2026-10-04").anvant() >= 1234


@pytest.mark.asyncio
async def test_sovloopen_bokfor_lopande(egen_budget):
    s = Fake()
    await s.vakna()
    _oppen_sedan(s, 90)
    r = _recovery(s)
    r.SOV_CHECK_S = 0.01
    r._last_input_audio = r._last_wake = time.monotonic()  # mid-conversation
    task = asyncio.create_task(r._sov_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    assert s.sover is False and 89 < egen_budget.anvant() < 92
