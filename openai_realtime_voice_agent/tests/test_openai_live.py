"""OpenAI Live (raawr US-025): the protocol against a fake server, and the money rules.

Nothing here talks to api.openai.com: a websockets server on 127.0.0.1:0
plays OpenAI. Each test fails on 0.27.0 (the engine did not exist).
"""
import asyncio
import base64
import json
import time

import pytest
from pipecat.frames.frames import UserStartedSpeakingFrame
from pipecat.frames.frames import TTSAudioRawFrame, LLMFullResponseEndFrame
from websockets.asyncio.server import serve

from app.providers import OPENAI_LIVE, PROVIDERS, ProviderOptions, build_service
from app.providers import openai_live
from app.providers.sovlage import Budget, SovlageMixin


@pytest.fixture(autouse=True)
def egna_liggare(tmp_path, monkeypatch):
    """Never the real ledgers; no lock left over from another test."""
    monkeypatch.setattr(SovlageMixin, "budget", Budget(path=str(tmp_path / "moln.json"), today=lambda: "d"))
    monkeypatch.setattr(openai_live, "OPENAI_BUDGET", Budget(path=str(tmp_path / "oa.json"), today=lambda: "d"))
    openai_live._Las.agare = None
    monkeypatch.setenv("OPENAI_LIVE_REPLY_GAP_MS", "100")
    yield
    openai_live._Las.agare = None


class FakeLive:
    """Records every client event; answers session.start; can push server events."""

    def __init__(self, started=True):
        self.seen = []
        self.started = started
        self.conn = None

    async def handler(self, ws):
        self.conn = ws
        async for raw in ws:
            ev = json.loads(raw)
            self.seen.append(ev)
            if ev["type"] == "session.start" and self.started:
                await ws.send(json.dumps({"type": "session.started", "session": {"id": "sess_1"}}))

    def types(self):
        return [e["type"] for e in self.seen]


async def _server(fake):
    server = await serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


def _service(url, tools=()):
    opts = ProviderOptions(api_key="k", model="", voice="", instructions="Du är Björn.")
    s = build_service(OPENAI_LIVE, opts, list(tools))
    s._url = url
    s.frames = []

    async def push(frame, direction=None):
        s.frames.append(frame)

    s.push_frame = push
    # No pipeline runs here, so no pipecat task manager: plain asyncio tasks.
    s.create_task = lambda coro, name=None: asyncio.get_running_loop().create_task(coro)
    return s


async def _vant(cond, t=2.0):
    for _ in range(int(t / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def test_motorn_finns_och_tar_24_khz():
    from app.providers import input_sample_rate, supports_client_events, self_heals
    assert OPENAI_LIVE in PROVIDERS
    assert input_sample_rate(OPENAI_LIVE) == 24000
    assert not supports_client_events(OPENAI_LIVE) and self_heals(OPENAI_LIVE)


def test_session_start_har_modellen_och_verktygen():
    opts = ProviderOptions(api_key="k", model="", voice="", instructions="Hej.")
    cfg = openai_live.session_config(opts, [{"type": "function", "name": "HassTurnOn",
                                             "description": "tänd", "parameters": {"type": "object"}}])
    assert cfg["model"] == "gpt-live-1"
    assert cfg["delegation"]["type"] == "responses"
    assert cfg["delegation"]["responses"]["tools"][0]["name"] == "HassTurnOn"


@pytest.mark.asyncio
async def test_sover_tills_vakning_och_vantar_pa_session_started():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        assert s.sover is True
        await s._connect()  # what pipecat's start() does: nothing while asleep
        assert fake.seen == []
        assert await s.vakna() is True
        assert fake.types() == ["session.start"]
        assert fake.seen[0]["session"]["model"] == "gpt-live-1"
        await s.sova("tyst")
        assert await _vant(lambda: "session.close" in fake.types())


@pytest.mark.asyncio
async def test_utan_session_started_somnar_den_igen():
    fake = FakeLive(started=False)
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        s._ar_uppkopplad_orig = s._ar_uppkopplad

        async def kort():
            return await s._ar_uppkopplad_orig(timeout=0.2)

        s._ar_uppkopplad = kort
        assert await s.vakna() is False
        assert s.sover is True and openai_live._Las.agare is None


@pytest.mark.asyncio
async def test_ljud_in_och_ut_och_svaret_tar_slut():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        s._turns = None  # stream straight through
        done = []

        async def klar():
            done.append(1)

        s.set_turn_complete_handler(klar)
        await s.vakna()
        await s._append(b"\x01\x02" * 10)
        assert await _vant(lambda: "session.input_audio.append" in fake.types())
        ev = [e for e in fake.seen if e["type"] == "session.input_audio.append"][0]
        assert base64.b64decode(ev["audio"]) == b"\x01\x02" * 10
        await fake.conn.send(json.dumps({"type": "session.output_audio.delta",
                                         "delta": base64.b64encode(b"\x00\x01" * 240).decode()}))
        assert await _vant(lambda: any(isinstance(f, TTSAudioRawFrame) for f in s.frames))
        audio = [f for f in s.frames if isinstance(f, TTSAudioRawFrame)][0]
        assert audio.sample_rate == 24000
        assert await _vant(lambda: done == [1])
        assert any(isinstance(f, LLMFullResponseEndFrame) for f in s.frames)
        await s.sova("klar")


@pytest.mark.asyncio
async def test_verktyg_via_delegering_och_resultat_tillbaka():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        anrop = []

        async def handler(params):
            anrop.append((params.function_name, params.arguments))
            await params.result_callback({"ok": True})

        s.register_function("vaderprognos", handler)
        await s.vakna()
        await fake.conn.send(json.dumps({"type": "response.event", "delegation_id": "d1", "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "vaderprognos", "arguments": "{\"dag\": 1}"}}}))
        assert await _vant(lambda: "response.create" in fake.types())
        out = [e for e in fake.seen if e["type"] == "response.item.create"][0]["item"]
        assert out == {"type": "function_call_output", "call_id": "c1", "output": "{\"ok\": true}"}
        assert anrop == [("vaderprognos", {"dag": 1})]
        await s.sova("klar")


class Turer:
    def __init__(self, events):
        self.events = list(events)

    def feed(self, pcm, rate=None):
        return self.events.pop(0) if self.events else None

    def reset(self):
        pass


def _frame(i):
    from pipecat.frames.frames import InputAudioRawFrame
    return InputAudioRawFrame(audio=bytes([i]) * 320, sample_rate=16000, num_channels=1)


@pytest.mark.asyncio
async def test_bana0_traff_nar_aldrig_live_miss_gor_det():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        await s.vakna()
        beslut = {}

        async def tur_slut():
            if beslut["traff"]:
                await s.drop_turn()
            else:
                await s.answer_turn()

        s.on_user_turn_end = tur_slut
        beslut["traff"] = True
        s._turns = Turer(["start", None, "end"])
        for i in range(3):
            await s._send_user_audio(_frame(i + 1))
        await asyncio.sleep(0.2)
        assert "session.input_audio.append" not in fake.types()  # Live never heard the order

        beslut["traff"] = False
        s._turns = Turer(["start", None, "end"])
        for i in range(3):
            await s._send_user_audio(_frame(i + 1))
        assert await _vant(lambda: fake.types().count("session.input_audio.append") >= 3)
        await s.sova("klar")


@pytest.mark.asyncio
async def test_en_session_at_gangen():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        a, b = _service(url), _service(url)
        assert await a.vakna() is True
        assert await b.vakna() is False and b.vagran == "las"
        await a.sova("klar")
        assert await b.vakna() is True
        await b.sova("klar")


@pytest.mark.asyncio
async def test_openai_taket_sager_nej(monkeypatch):
    monkeypatch.setenv("OPENAI_MAX_MINUTER_PER_DAG", "6")
    openai_live.OPENAI_BUDGET.lagg_till(6 * 60)
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        assert await s.vakna() is False and s.vagran == "budget"
        assert fake.seen == []


@pytest.mark.asyncio
async def test_sova_raknar_openai_minuter():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        await s.vakna()
        s._uppkopplad_sedan -= 120
        await s.sova("klar")
        assert 119 < openai_live.OPENAI_BUDGET.anvant() < 125


def test_kedjan_tar_openai_live(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDERS", "gemini,openai_live,xai")
    assert build_router().chain == ["gemini", "openai_live", "xai"]


@pytest.mark.asyncio
async def test_nekad_vakning_flyttar_hogtalaren_till_nasta_motor():
    from unittest.mock import AsyncMock
    from app.provider_router import ProviderRouter
    from app.websocket_handler import ConnectionRecovery

    class Nekad(SovlageMixin):
        sover = True
        vagran = None

        async def vakna(self):
            self.vagran = "las"
            return False

    router = ProviderRouter("openai_live", "gemini", probe=lambda e: True)
    flytt = AsyncMock()
    r = ConnectionRecovery(Nekad(), provider="openai_live", router=router, on_failover=flytt)
    await r.vakna()
    await asyncio.sleep(0.05)
    flytt.assert_awaited_once()
    assert router.current() == "gemini"


# --- review of US-025 (adversarial, 2026-10-04) ---

@pytest.mark.asyncio
async def test_produktionsvagen_somnar_nar_session_started_uteblir():
    """Through ConnectionRecovery.vakna with its real 5 s limit: the engine's own
    3 s wait must end first, so the engine is asleep and the lock free again."""
    from app.websocket_handler import ConnectionRecovery

    fake = FakeLive(started=False)
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        r = ConnectionRecovery(s, provider=OPENAI_LIVE)
        await r.vakna()
        assert s.sover is True and openai_live._Las.agare is None


@pytest.mark.asyncio
async def test_yttre_tidsgrans_soever_motorn():
    """Even if the outer limit fires first, the engine goes back to sleep."""
    from app.websocket_handler import ConnectionRecovery

    fake = FakeLive(started=False)
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        r = ConnectionRecovery(s, provider=OPENAI_LIVE)
        r.VAKNA_TIMEOUT_S = 0.3  # shorter than the engine's own 3 s wait
        await r.vakna()
        assert s.sover is True and openai_live._Las.agare is None


@pytest.mark.asyncio
async def test_nedrivning_raknar_minuterna():
    from app.device_registry import DeviceConnection
    from app.websocket_handler import WebSocketHandler

    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        assert await s.vakna() is True
        s._uppkopplad_sedan -= 120  # two minutes connected
        conn = DeviceConnection(device_id="kontoret", websocket=object(), serializer=None)
        conn.openai_service = s
        await WebSocketHandler()._teardown(conn)
        assert openai_live._Las.agare is None  # the lock is released; minutes counted (the machine closes it)
        assert openai_live.OPENAI_BUDGET.anvant() >= 119


@pytest.mark.asyncio
async def test_svaret_tar_inte_slut_medan_ett_verktyg_kor():
    fake = FakeLive()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        s._turns = None
        done = []

        async def klar():
            done.append(1)

        s.set_turn_complete_handler(klar)
        await s.vakna()
        s._verktyg_pagar = 1  # a delegated tool is running
        await fake.conn.send(json.dumps({"type": "session.output_audio.delta",
                                         "delta": base64.b64encode(b"\x00\x01" * 240).decode()}))
        assert await _vant(lambda: any(isinstance(f, TTSAudioRawFrame) for f in s.frames))
        await asyncio.sleep(0.3)  # past the (test) 100 ms gap
        assert done == []
        await s.sova("klar")


def test_sessionen_startas_med_det_riktiga_endpointen_godkanner(monkeypatch):
    """Probed against /v1/live/sessions 2026-10-10: one audio.format for both directions, no audio.input."""
    from types import SimpleNamespace

    opts = SimpleNamespace(model="gpt-live-1", instructions="x", voice="marin", max_output_tokens=None)
    verktyg = [{"name": "HassTurnOn", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    monkeypatch.delenv("OPENAI_LIVE_DELEGATION", raising=False)
    cfg = openai_live.session_config(opts, verktyg)
    assert cfg["audio"]["format"] == {"type": "audio/pcm", "rate": 24000} and "input" not in cfg["audio"]
    assert cfg["delegation"]["type"] == "responses" and cfg["delegation"]["responses"]["tools"][0]["name"] == "HassTurnOn"
    monkeypatch.setenv("OPENAI_LIVE_DELEGATION", "client")
    assert openai_live.session_config(opts, verktyg)["delegation"] == {"type": "client"}


@pytest.mark.asyncio
async def test_tystnaden_i_stromen_ar_inget_svar(monkeypatch):
    """Live streams zeros between answers (probed live 2026-10-10); only real sound opens a reply."""
    monkeypatch.setenv("OPENAI_LIVE_PREBUF_S", "0")
    s = _service("ws://x")
    await s._audio_out(b"\x00" * 4800)
    assert s.frames == [] and s._reply_open is False
    await s._audio_out(bytes([5, 1]) * 2400)
    assert any(isinstance(f, TTSAudioRawFrame) for f in s.frames) and s._reply_open is True
    s._reply_end_task.cancel()


@pytest.mark.asyncio
async def test_forsta_ljudet_haalls_tills_0_7_s_finns_sa_enheten_inte_tomkors(monkeypatch):
    """Kitchen 2026-10-10: Live sends 0.2-0.4 s, stalls, then streams. The first sound waits for 0.7 s of audio."""
    s = _service("ws://x")
    bit = bytes([5, 1]) * 4800  # 0.2 s at 24 kHz s16
    ljud = lambda: [f for f in s.frames if isinstance(f, TTSAudioRawFrame)]
    await s._audio_out(bit)
    await s._audio_out(bit)
    await s._audio_out(bit)
    assert s._reply_open and ljud() == []  # 0.6 s: still held
    await s._audio_out(bit)
    assert len(ljud()) == 4  # 0.8 s: all four released, in order
    await s._audio_out(bit)
    assert len(ljud()) == 5  # after that, straight through
    s._reply_end_task.cancel()


@pytest.mark.asyncio
async def test_ett_kort_svar_och_en_langsam_start_spelas_anda(monkeypatch):
    monkeypatch.setenv("OPENAI_LIVE_PREBUF_MAX_S", "0.05")
    s = _service("ws://x")
    bit = bytes([5, 1]) * 2400
    await s._audio_out(bit)
    await asyncio.sleep(0.2)  # the stall: the timer lets the first sound go
    assert len([f for f in s.frames if isinstance(f, TTSAudioRawFrame)]) == 1
    if s._reply_end_task is not None:
        s._reply_end_task.cancel()
    # a reply shorter than the buffer is played when it ends
    monkeypatch.setenv("OPENAI_LIVE_REPLY_GAP_MS", "10")
    monkeypatch.setenv("OPENAI_LIVE_PREBUF_MAX_S", "30")
    s2 = _service("ws://x")
    await s2._audio_out(bit)
    await asyncio.sleep(0.2)
    assert len([f for f in s2.frames if isinstance(f, TTSAudioRawFrame)]) == 1 and s2._reply_open is False


@pytest.mark.asyncio
async def test_prebuf_0_spelar_direkt_som_for(monkeypatch):
    monkeypatch.setenv("OPENAI_LIVE_PREBUF_S", "0")
    s = _service("ws://x")
    await s._audio_out(bytes([5, 1]) * 2400)
    assert len([f for f in s.frames if isinstance(f, TTSAudioRawFrame)]) == 1
    s._reply_end_task.cancel()


@pytest.mark.asyncio
async def test_en_bit_under_urspolningen_hamnar_efter_de_hallna_inte_framfor(monkeypatch):
    """A's review of #54: a chunk arriving while the held ones are pushed must not go in front of them."""
    s = _service("ws://x")
    ordning = []

    async def push(frame, *a, **k):
        if isinstance(frame, TTSAudioRawFrame):
            ordning.append(frame.audio[0])
        await asyncio.sleep(0.01)  # gives up control, as a real push does

    s.push_frame = push
    bit = lambda n: bytes([n, 1]) * 2400
    await s._audio_out(bit(1))
    await s._audio_out(bit(2))
    tom = asyncio.get_running_loop().create_task(s._slapp_forbuffer())  # the timer's release
    await asyncio.sleep(0.005)  # inside the loop
    await s._audio_out(bit(3))  # a chunk arrives from the reader meanwhile
    await tom
    await asyncio.sleep(0.05)
    assert ordning == [1, 2, 3]
    if s._reply_end_task is not None:
        s._reply_end_task.cancel()


def test_svar_pa_annat_sprak_loggas_med_de_forsta_orden(caplog):
    import logging

    s = _service("ws://x")
    with caplog.at_level(logging.WARNING):
        for d in ["Ronzio ", "a terra ", "hej ", "på dig"]:
            s._svarsprak(d)
    rader = [r.getMessage() for r in caplog.records if "another language" in r.getMessage()]
    assert len(rader) == 1 and "ronzio a terra hej" in rader[0] and "på dig" not in rader[0]


def test_lopande_bokforing_hamnar_ocksa_i_openai_liggaren():
    """G's review of #49: 100 s open, booked running then at the end, must be 100 s on BOTH ledgers, once."""
    import time

    s = _service("ws://x")
    s._uppkopplad_sedan = time.monotonic() - 100
    s.bokfor_lopande()
    assert 99 <= openai_live.OPENAI_BUDGET.anvant() <= 101 and 99 <= s.budget.anvant() <= 101
    s.bokfor()  # the teardown after it adds only what is left (nothing)
    assert openai_live.OPENAI_BUDGET.anvant() < 102 and s.budget.anvant() < 102


@pytest.mark.asyncio
async def test_hallet_ljud_slapps_inte_snabbare_an_live_tillater(monkeypatch):
    """Probed live: > 1.2x real time or a burst over 5 s is dropped. The first BURST_S go at once, the rest paced."""
    import time

    monkeypatch.setenv("OPENAI_LIVE_BURST_S", "0.2")
    monkeypatch.setenv("OPENAI_LIVE_MAX_X", "1.15")
    s = _service("ws://x")
    skickat = []

    async def append(pcm):
        skickat.append((time.monotonic(), len(pcm)))

    async def end():
        pass

    s._append, s._end_activity = append, end
    chunk = b"\x01\x00" * int(openai_live.IN_RATE * 0.2)  # 0.2 s each
    s._held = [chunk] * 5
    t0 = time.monotonic()
    await s.answer_turn()
    assert len(skickat) == 5
    assert time.monotonic() - t0 >= 3 * 0.2 / 1.15 * 0.9  # four chunks after the burst, each waited ~0.17 s


@pytest.mark.asyncio
async def test_en_andra_vakning_inom_minuten_far_ett_nytt_forsok(monkeypatch):
    """Probed live: gpt-live-1 is limited to 600 tokens/min and a session start asks for all of it ("try again in
    900ms"). The wake waits that long once instead of being lost."""
    starts = []

    class Strypt(FakeLive):
        async def handler(self, ws):
            self.conn = ws
            async for raw in ws:
                ev = json.loads(raw)
                self.seen.append(ev)
                if ev["type"] == "session.start":
                    starts.append(1)
                    if len(starts) == 1:
                        await ws.send(json.dumps({"type": "error", "error": {
                            "code": "rate_limit_exceeded",
                            "message": "Rate limit reached ... tokens per min (TPM): Limit 600. Please try again in 200ms."}}))
                    else:
                        await ws.send(json.dumps({"type": "session.started", "session": {"id": "s"}}))

    fake = Strypt()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        assert await s.vakna() is True
        assert len(starts) == 2 and s.sover is False
        await s._disconnect()


@pytest.mark.asyncio
async def test_retryn_klarar_att_servern_stanger_den_nekade_sockeln():
    """G's review of #49: the server closes the refused socket while we wait. That end must not put the engine to
    sleep (sov_begaran) and the second start must still come up."""
    starts = []

    class Stanger(FakeLive):
        async def handler(self, ws):
            self.conn = ws
            async for raw in ws:
                ev = json.loads(raw)
                self.seen.append(ev)
                if ev["type"] == "session.start":
                    starts.append(1)
                    if len(starts) == 1:
                        await ws.send(json.dumps({"type": "error", "error": {
                            "code": "rate_limit_exceeded", "message": "... Please try again in 200ms."}}))
                        await ws.close()
                        return
                    await ws.send(json.dumps({"type": "session.started", "session": {"id": "s"}}))

    fake = Stanger()
    server, url = await _server(fake)
    async with server:
        s = _service(url)
        assert await s.vakna() is True
        await asyncio.sleep(0.3)  # the first reader's end has been seen by now
        assert len(starts) == 2 and s.sover is False and s._ws is not None
        await s._disconnect()


@pytest.mark.asyncio
async def test_tidsraden_skrivs_for_live_som_for_gemini(caplog):
    """The comparison day reads `⏱ tider <rum> turslut=.. modell=.. enhet=..` for the kitchen too."""
    import logging

    from app.turn_tider import TurnTider

    nu = [100.0]
    s = _service("ws://x")
    s.tider = TurnTider("koket", klocka=lambda: nu[0])
    s._turns = type("T", (), {"silence_s": 0.8})()
    s._tider_start()          # the local turn end
    nu[0] += 1.2
    await s._audio_out(bytes([5, 1]) * 2400)   # the first real sound
    nu[0] += 0.05
    with caplog.at_level(logging.INFO):
        s.tider.mark("enhet")                  # it reached the device
    rad = [r.getMessage() for r in caplog.records if "⏱ tider koket" in r.getMessage()]
    assert rad and "turslut=800" in rad[0] and "modell=2000" in rad[0] and "enhet=2050" in rad[0]
    s._reply_end_task.cancel()


# --- A barge-in must be speech, not room sound (app/avbrott.py) -----------------------------------------------


def _ljud(dbfs_niva: float, antal: int = 1):
    """`antal` frames of 10 ms of a 400 Hz tone at the given RMS level."""
    import math
    import struct
    from pipecat.frames.frames import InputAudioRawFrame

    amp = 32768 * (10 ** (dbfs_niva / 20)) * math.sqrt(2)
    pcm = struct.pack("<160h", *[int(amp * math.sin(2 * math.pi * 400 * n / 16000)) for n in range(160)])
    return [InputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1) for _ in range(antal)]


class _Tal(Turer):
    """A detector stand-in that also tells whether it is in speech (the gate counts only those frames)."""
    _speaking = True


async def _spela_in(s, frames, handelser):
    s._turns = _Tal(handelser)
    for f in frames:
        await s._send_user_audio(f)


def _pagar_svar(s):
    s._reply_open = True
    s._sista_ljud_t = time.monotonic()


@pytest.mark.asyncio
async def test_rumsljud_under_svaret_avbryter_inte_bjorn():
    """Kitchen 2026-10-10: a TV or a voice in the next room must not cut the answer. Short or quiet speech that starts
    while Björn speaks is thrown away: no turn, no interruption."""
    s = _service("ws://x")
    s.on_user_turn_end = lambda: asyncio.sleep(0)
    _pagar_svar(s)
    await _spela_in(s, _ljud(-55, 80), ["start"] + [None] * 78 + ["end"])  # long but far too quiet
    assert s._held is None and s._kandidat is None
    _pagar_svar(s)
    await _spela_in(s, _ljud(-20, 20), ["start"] + [None] * 18 + ["end"])  # loud but only 200 ms
    assert s._held is None and s._kandidat is None
    assert not [f for f in s.frames if isinstance(f, UserStartedSpeakingFrame)]


@pytest.mark.asyncio
async def test_henriks_svar_medan_bjorn_pratar_avbryter_nar_det_ar_langt_och_hogt_nog():
    s = _service("ws://x")
    s.on_user_turn_end = lambda: asyncio.sleep(0)
    _pagar_svar(s)
    await _spela_in(s, _ljud(-22, 70), ["start"] + [None] * 69)  # 700 ms of clear speech
    assert [f for f in s.frames if isinstance(f, UserStartedSpeakingFrame)], "a real answer is a barge-in"
    assert s._held and s._kandidat is None


@pytest.mark.asyncio
async def test_utan_pagaende_svar_eller_med_troskeln_av_startar_tal_en_tur_direkt(monkeypatch):
    s = _service("ws://x")
    s.on_user_turn_end = lambda: asyncio.sleep(0)
    await _spela_in(s, _ljud(-22, 3), ["start", None, None])  # no answer playing: no gate
    assert s._held is not None and s._kandidat is None
    s2 = _service("ws://x")
    s2.on_user_turn_end = lambda: asyncio.sleep(0)
    monkeypatch.setenv("AVBROTT_TROSKEL", "0")
    _pagar_svar(s2)
    await _spela_in(s2, _ljud(-22, 3), ["start", None, None])
    assert s2._held is not None and s2._kandidat is None  # gate off: every start is a barge-in, as before


def test_vilolage_efter_svaret_ar_kortare_for_gemini_an_for_live(monkeypatch):
    """The follow-up window may open sooner on Gemini (no gaps); Live keeps its 1.5 s. An explicit env still wins."""
    from app.websocket_handler import idle_debounce_for
    monkeypatch.delenv("PHASE_IDLE_DEBOUNCE_MS", raising=False)
    assert idle_debounce_for("gemini") == 0.6 and idle_debounce_for("openai_live") == 1.5
    monkeypatch.setenv("PHASE_IDLE_DEBOUNCE_MS", "1000")
    assert idle_debounce_for("gemini") == 1.0 and idle_debounce_for("openai_live") == 1.0


@pytest.mark.asyncio
async def test_drop_turn_kastar_en_halvfardig_avbrottskandidat():
    """A's review of #55: a candidate left over from dropped input must not be mixed with the next speech."""
    s = _service("ws://x")
    s.on_user_turn_end = lambda: asyncio.sleep(0)
    _pagar_svar(s)
    await _spela_in(s, _ljud(-22, 10), ["start"] + [None] * 9)  # 100 ms: still a candidate
    assert s._kandidat is not None
    await s.drop_turn()
    assert s._kandidat is None
