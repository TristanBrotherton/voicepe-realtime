"""Bana 0 (raawr US-016): local STT -> HA via comms -> the model only on a miss.

A fake Wyoming server on 127.0.0.1:0, a fake comms behind httpx.MockTransport
and a fake realtime service that records every client event it is sent.
"""
import asyncio
import json
import time

import httpx
import pytest

from app import bana0
import app.main  # noqa: F401 -- before the comms fixture patches httpx.AsyncClient

COMMS = "http://comms.test:3500/kanal/rost/kontoret/api"
NYCKEL = "kontorets-comms-nyckel"
PCM = bytes(range(256)) * 30  # 7680 B -> three chunks of <= 3200 B


class FakeService:
    def __init__(self):
        self.events = []

    async def send_client_event(self, event):
        self.events.append(event)

    def typer(self):
        return [e.type for e in self.events]


async def _wyoming(svar=b"", hang=False):
    """Start a fake Wyoming STT; returns (server, port, received events)."""
    seen = []

    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                header = json.loads(line)
                payload = await reader.readexactly(header.get("payload_length", 0))
                seen.append((header["type"], header.get("data"), payload))
                if header["type"] == "audio-stop":
                    if hang:
                        await reader.read()  # silent until the client gives up
                    writer.write(svar)
                    await writer.drain()
                    return
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


def _transcript(text):
    """Newer Wyoming: data after the header line, announced by data_length."""
    data = json.dumps({"text": text}).encode()
    return json.dumps({"type": "transcript", "data_length": len(data)}).encode() + b"\n" + data


@pytest.fixture
def comms(monkeypatch):
    """Fake comms; set `comms.svar` to a Response or an exception."""
    monkeypatch.setenv("HA_API_URL", COMMS)
    monkeypatch.setenv("COMMS_NYCKEL", NYCKEL)
    state = type("Comms", (), {"seen": [], "svar": httpx.Response(204)})()

    def handler(request):
        state.seen.append(request)
        if isinstance(state.svar, Exception):
            raise state.svar
        return state.svar

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state


def _ha(speech):
    return httpx.Response(200, json={"response": {"speech": {"plain": {"speech": speech}}}})


async def _tur(port, service, said, timeout_stt=1.0):
    async def stt(pcm, timeout):
        return await bana0.transkribera(pcm, "127.0.0.1", port, timeout)

    return await bana0.tur(
        PCM, stt=stt, timeout_stt=timeout_stt, timeout_comms=4.0,
        skicka_svar_till_modellen=lambda t: bana0.be_om_bekraftelse(service, t),
        skapa_svar=lambda: bana0.be_om_svar(service),
        efter_traff=lambda: said.append("vakt"),
    )


def _bekraftelse(service, ha_text):
    """A hit: a system item naming what HA said, then a response with no tools."""
    assert service.typer() == ["conversation.item.create", "response.create"]
    item = service.events[0].item
    assert (item.role, item.content[0].type) == ("system", "input_text")
    assert ha_text in item.content[0].text
    assert service.events[1].response.tool_choice == "none"


@pytest.mark.asyncio
async def test_wyoming_protokoll_ordning_och_transcript():
    # A stray event before the transcript, with a payload, must be skipped.
    stray = json.dumps({"type": "info", "data": {}, "payload_length": 3}).encode() + b"\nxyz"
    server, port, seen = await _wyoming(stray + _transcript(" tänd lampan "))
    async with server:
        assert await bana0.transkribera(PCM, "127.0.0.1", port, 2.0) == "tänd lampan"

    typer = [t for t, _, _ in seen]
    assert typer == ["transcribe", "audio-start", "audio-chunk", "audio-chunk", "audio-chunk", "audio-stop"]
    assert seen[0][1] == {"language": "sv"}
    audio = {"rate": 16000, "width": 2, "channels": 1}
    assert seen[1][1] == audio
    assert all(d == audio and len(p) <= 3200 for t, d, p in seen if t == "audio-chunk")
    assert b"".join(p for t, _, p in seen if t == "audio-chunk") == PCM


@pytest.mark.asyncio
async def test_prova_skickar_bara_text_till_comms_med_nyckeln(comms):
    comms.svar = _ha("Tände lampan")
    assert await bana0.prova("tänd lampan", 4.0) == "Tände lampan"
    (r,) = comms.seen
    assert (r.method, str(r.url)) == ("POST", COMMS + "/conversation/process")
    assert r.headers["x-raawr-nyckel"] == NYCKEL
    assert json.loads(r.content) == {"text": "tänd lampan"}

    for svar in (httpx.Response(204), httpx.Response(500), httpx.Response(200, json={}), httpx.ConnectError("nere")):
        comms.svar = svar
        assert await bana0.prova("vad är klockan", 4.0) is None


@pytest.mark.asyncio
async def test_traff_ger_ingen_modellbegaran_och_inget_verktyg(comms):
    comms.svar = _ha("Tände lampan i kontoret")
    server, port, _ = await _wyoming(_transcript("tänd lampan i kontoret"))
    service, said = FakeService(), []
    async with server:
        assert await _tur(port, service, said) == "bana0"

    # HA's own (dry) reply is not spoken; the model confirms, the net is armed.
    assert said == ["vakt"]
    _bekraftelse(service, "Tände lampan i kontoret")


@pytest.mark.asyncio
async def test_miss_gar_till_modellen(comms):
    comms.svar = httpx.Response(204)
    # not a phrase from the speech-to-text's hint: those never reach comms (test_bana0_pahittat)
    server, port, _ = await _wyoming(_transcript("vad heter du"))
    service, said = FakeService(), []
    async with server:
        assert await _tur(port, service, said) == "modell"
    assert said == []
    assert service.typer() == ["response.create"]
    assert len(comms.seen) == 1


@pytest.mark.asyncio
async def test_stt_hanger_gar_till_modellen_inom_budget(comms):
    server, port, _ = await _wyoming(hang=True)
    service, said = FakeService(), []
    async with server:
        start = time.monotonic()
        assert await _tur(port, service, said, timeout_stt=0.3) == "modell"
        assert time.monotonic() - start < 1.0
    assert service.typer() == ["response.create"]
    assert comms.seen == []  # no transcript -> comms never asked


@pytest.mark.asyncio
async def test_stt_nere_gar_till_modellen(comms):
    server, port, _ = await _wyoming()
    server.close()
    await server.wait_closed()
    service = FakeService()
    assert await _tur(port, service, []) == "modell"
    assert service.typer() == ["response.create"]


@pytest.mark.asyncio
async def test_comms_nere_gar_till_modellen(comms):
    comms.svar = httpx.ConnectError("comms nere")
    server, port, _ = await _wyoming(_transcript("tänd lampan"))
    service, said = FakeService(), []
    async with server:
        assert await _tur(port, service, said) == "modell"
    assert said == []
    assert service.typer() == ["response.create"]


@pytest.mark.asyncio
async def test_bana0_fel_talas_och_gar_inte_till_modellen(comms):
    # comms answers 200 with HA's own failure text when the execution failed.
    comms.svar = _ha("Tyvärr, jag kunde inte nå lampan")
    server, port, _ = await _wyoming(_transcript("tänd lampan"))
    service, said = FakeService(), []
    async with server:
        assert await _tur(port, service, said) == "bana0"
    # HA's failure text reaches the model as what happened; no tools to retry with.
    _bekraftelse(service, "Tyvärr, jag kunde inte nå lampan")


@pytest.mark.asyncio
async def test_traff_dar_modellen_inte_nas_ar_fortfarande_en_traff(comms):
    comms.svar = _ha("Tände lampan")
    server, port, _ = await _wyoming(_transcript("tänd lampan"))
    vakt = []

    async def stt(pcm, timeout):
        return await bana0.transkribera(pcm, "127.0.0.1", port, timeout)

    async def nere(text):
        raise RuntimeError("ingen modell")

    async def skapa():
        raise AssertionError("a hit must never ask the model to answer the turn")

    async with server:
        assert await bana0.tur(
            PCM, stt=stt, timeout_stt=1.0, timeout_comms=4.0,
            skicka_svar_till_modellen=nere, skapa_svar=skapa,
            efter_traff=lambda: vakt.append(1),
        ) == "bana0"
    assert vakt == [1]  # the cached "Klart." net still runs


# --- T3: the wiring (main.py, websocket_handler.py, the service, the serializer) ---

def _app(create_response=True, bana0_stt=None):
    from app.main import Application

    app = Application()
    app.instructions = "Base."
    app.max_output_tokens = None
    app.openai_api_key, app.model, app.voice, app.openai_speed = "sk-key", "gpt-realtime-2", "marin", 1.0
    app.noise_reduction = ""
    app.turn_detection_type = "semantic_vad"
    app.vad_eagerness = "low"
    app.vad_threshold, app.vad_prefix_padding_ms, app.vad_silence_duration_ms = 0.5, 300, 800
    app.semantic_vad_create_response = create_response
    app.interrupt_response = False
    app.transcription_model, app.transcription_language = "gpt-4o-transcribe", ""
    app.bana0_stt = bana0_stt
    return app


def _create_response(app):
    from app.providers import OPENAI
    from app.providers.openai_realtime import build

    options = app.provider_options(OPENAI)
    service = build(options, tools=[])
    assert service._session_properties.audio.input.turn_detection.create_response is options.semantic_vad_create_response
    return options.semantic_vad_create_response, service


def test_create_response_av_bara_nar_bana0_pa():
    # Off: exactly as before -- the server creates, the context is pre-seeded.
    off, service = _create_response(_app())
    assert off is True
    _app()._preseed_context(service)
    assert service._context is not None

    # On: the server no longer creates; the agent does, on a miss. The
    # pre-seed must still run, or the first context greets the room.
    on, service = _create_response(_app(bana0_stt=("127.0.0.1", 10300)))
    assert on is False
    _app(bana0_stt=("127.0.0.1", 10300))._preseed_context(service)
    assert service._context is not None

    # Off with create_response already false: no pre-seed, as before.
    _, service = _create_response(_app(create_response=False))
    _app(create_response=False)._preseed_context(service)
    assert service._context is None


def test_stt_adress():
    assert bana0.stt_adress("") is None
    assert bana0.stt_adress("10.10.0.5:10300") == ("10.10.0.5", 10300)
    assert bana0.stt_adress("stt.lan") == ("stt.lan", 10300)
    assert bana0.stt_adress("stt.lan:x") is None


def _koppling(stt):
    """A built pipeline for one OpenAI device with a real SafeRealtimeLLMService."""
    from app.device_registry import DeviceConnection
    from app.providers import OPENAI
    from app.raw_audio_serializer import RawAudioSerializer
    from app.websocket_handler import WebSocketHandler

    _, service = _create_response(_app(bana0_stt=stt))
    sent = []

    async def send(event):
        sent.append(event)

    async def noop(*a, **k):
        return None

    service.send_client_event = send
    service.start_ttfb_metrics = service.start_processing_metrics = service.push_frame = noop
    service.push_interruption_task_frame_and_wait = noop

    handler = WebSocketHandler()
    handler.bana0_stt = stt
    serializer = RawAudioSerializer("kontoret", input_sample_rate=16000)
    connection = DeviceConnection(device_id="kontoret", websocket=object(), serializer=serializer)
    connection.provider = OPENAI
    connection.transport = handler.create_transport(object(), serializer, OPENAI)
    connection.openai_service = service
    handler.build_pipeline(connection)
    return handler, connection, service, sent


@pytest.mark.asyncio
async def test_turn_pcm_nollstalls_vid_vakning_och_kapas():
    from app.raw_audio_serializer import TURN_PCM_CAP, RawAudioSerializer

    ser = RawAudioSerializer("kontoret", input_sample_rate=16000)
    await ser.deserialize(b"\x01\x00" * 100)
    await ser.deserialize(json.dumps({"type": "wake"}))
    await ser.deserialize(PCM)
    assert ser.take_turn_audio() == PCM
    assert ser.take_turn_audio() == b""
    await ser.deserialize(b"\x00\x00" * (TURN_PCM_CAP // 2 + 10))
    assert len(ser.take_turn_audio()) == TURN_PCM_CAP


@pytest.mark.asyncio
async def test_speech_stopped_med_traff_ber_modellen_bekrafta_utan_verktyg(comms):
    comms.svar = _ha("Tände lampan i kontoret")
    server, port, seen = await _wyoming(_transcript("tänd lampan i kontoret"))
    handler, connection, service, sent = _koppling(("127.0.0.1", port))
    said, idle = [], []

    async def say(text, device_id=None):
        said.append((text, device_id))

    async def force_idle(reason=""):
        idle.append(reason)

    handler.say = say
    connection.phase_emitter.force_idle = force_idle
    await connection.serializer.deserialize(PCM)
    async with server:
        await service._handle_evt_speech_stopped(None)
        await service._turn_end_task

    assert b"".join(p for t, _, p in seen if t == "audio-chunk") == PCM
    assert [e.type for e in sent] == ["conversation.item.create", "response.create"]
    assert sent[0].item.role == "system" and "Tände lampan i kontoret" in sent[0].item.content[0].text
    assert sent[1].response.tool_choice == "none"
    assert said == []  # the dry voice never speaks HA's reply
    assert idle == ["bana0"]


@pytest.mark.asyncio
async def test_openai_klockan_besvaras_lokalt_och_modellen_svarar_inte_igen(comms, monkeypatch):
    monkeypatch.setenv("KLOCKA_KLIPP", "1")  # off by default since 2026-10-09; these test the code behind the flag
    """US-032 AC-7 on OpenAI: the clips speak, comms is not asked, no response is
    created, and the model is told the question is answered (no double answer)."""
    from app import klockan

    monkeypatch.setattr(klockan, "delar", lambda nu=None: ["Klockan är fjorton", "och tjugotvå minuter"])
    server, port, _ = await _wyoming(_transcript("Vad är klockan just nu?"))
    handler, connection, service, sent = _koppling(("127.0.0.1", port))
    said, idle, flaggor = [], [], []

    async def say(text, device_id=None, pace=True, pcm=None):
        said.append((text, pcm))

    async def clip(provider, text, fallback=True):
        flaggor.append(fallback)
        return text.encode()

    async def force_idle(reason=""):
        idle.append(reason)

    handler.say, handler.ack_clip = say, clip
    connection.phase_emitter.force_idle = force_idle
    await connection.serializer.deserialize(PCM)
    async with server:
        await service._handle_evt_speech_stopped(None)
        await service._turn_end_task

    assert said == [("Klockan är fjorton och tjugotvå minuter", "Klockan är fjortonoch tjugotvå minuter".encode())]
    assert flaggor == [False, False]  # never the conductor's dry voice
    assert comms.seen == []
    assert [e.type for e in sent] == ["conversation.item.create"]
    assert "redan fått svaret" in sent[0].item.content[0].text
    assert idle == ["klockan"]


@pytest.mark.asyncio
async def test_speech_stopped_med_miss_ber_modellen_svara(comms):
    comms.svar = httpx.Response(204)
    server, port, _ = await _wyoming(_transcript("hur varmt är det ute"))
    handler, connection, service, sent = _koppling(("127.0.0.1", port))
    await connection.serializer.deserialize(PCM)
    async with server:
        await service._handle_evt_speech_stopped(None)
        await service._turn_end_task
    assert [e.type for e in sent] == ["response.create"]


@pytest.mark.asyncio
async def test_bana0_av_ingen_krok_och_inga_egna_handelser():
    handler, connection, service, sent = _koppling(None)
    assert service.on_user_turn_end is None and service.on_user_turn_start is None
    await service._handle_evt_speech_stopped(None)
    assert service._turn_end_task is None
    assert sent == []


# --- F2: a follow-up turn has no wake, so the buffer must start at the user's speech ---

@pytest.mark.asyncio
async def test_folj_upp_tur_innehaller_bara_anvandarens_yttrande():
    # The pre-roll kept from before speech_started (TURN_PCM_PREROLL, 1.5 s since 0.29.5), because the
    # event arrives after the speech began and the first syllable must survive.
    from app.raw_audio_serializer import TURN_PCM_PREROLL as preroll
    handler, connection, service, sent = _koppling(("127.0.0.1", 10300))
    ser = connection.serializer
    tidigare = b"\x07\x00" * 16000 * 3  # 3 s of silence/echo after the last turn
    yttrande = PCM
    await ser.deserialize(tidigare)
    await service._handle_evt_speech_started(None)
    await ser.deserialize(yttrande)
    pcm = ser.take_turn_audio()
    assert pcm == tidigare[-preroll:] + yttrande


@pytest.mark.asyncio
async def test_bufferten_ar_tom_medan_modellen_svarar():
    handler, connection, service, sent = _koppling(("127.0.0.1", 10300))
    connection.phase_emitter._current = "replying"
    await connection.serializer.deserialize(PCM)
    assert connection.serializer.take_turn_audio() == b""
    connection.phase_emitter._current = "listening"
    await connection.serializer.deserialize(PCM)
    assert connection.serializer.take_turn_audio() == PCM


# --- Bana 0 on Gemini (up to 0.22.5 it was wired for OpenAI only) ----------

@pytest.fixture
def ha_svarar(monkeypatch):
    """HA's answer via comms, faked at bana0.prova: the comms fixture swaps
    httpx.AsyncClient for a function, which google-genai cannot subclass."""
    svar = []

    async def prova(text, timeout):
        return svar[0]

    monkeypatch.setattr(bana0, "prova", prova)
    return svar


def _gemini_koppling(stt, events):
    """A built pipeline for one Gemini device; Google is a recorder."""
    from app.device_registry import DeviceConnection
    from app.providers import GEMINI, build_service
    from app.raw_audio_serializer import RawAudioSerializer
    from app.websocket_handler import WebSocketHandler
    from test_gemini_provider import OPENAI_SHAPE, _ScriptedTurns, _options, _wire

    service = build_service(GEMINI, _options(), OPENAI_SHAPE)
    google = _wire(service, _ScriptedTurns(events))

    async def noop(*a, **k):
        return None

    service.push_frame = noop
    handler = WebSocketHandler()
    handler.bana0_stt = stt
    serializer = RawAudioSerializer("kontoret", input_sample_rate=16000)
    connection = DeviceConnection(device_id="kontoret", websocket=object(), serializer=serializer)
    connection.provider = GEMINI
    connection.transport = handler.create_transport(object(), serializer, GEMINI)
    connection.openai_service = service
    handler.build_pipeline(connection)
    said, idle = [], []

    async def say(text, device_id=None, pace=True, pcm=None):
        said.append((text, device_id))

    async def force_idle(reason=""):
        idle.append(reason)

    async def clip(provider, text, fallback=True):
        if text.startswith("Klockan är") or text.startswith("och "):
            assert fallback is False  # the clock never plays the dry voice
        return b"pcm"

    handler.say = say
    handler.ack_clip = clip
    from app.phase_emitter import TurnLiveness
    connection.turn_liveness = TurnLiveness()
    connection.phase_emitter.force_idle = force_idle
    return connection, service, google, said, idle


async def _speak(connection, service, n):
    from test_gemini_provider import _frame

    for b in range(n):
        await connection.serializer.deserialize(PCM)
        await service._send_user_audio(_frame(b))


@pytest.mark.asyncio
async def test_gemini_traff_google_hor_aldrig_ordern(ha_svarar):
    """"släck kontoret" on Gemini: HA does it and says so; Google hears
    nothing, so the model can neither answer it nor redo it next turn."""
    from test_gemini_provider import _kinds

    ha_svarar.append("Släckte i kontoret")
    server, port, seen = await _wyoming(_transcript("släck kontoret"))
    connection, service, google, said, idle = _gemini_koppling(
        ("127.0.0.1", port), [None, "start", None, "end"])
    async with server:
        await _speak(connection, service, 4)
        await service._turn_end_task
        await asyncio.sleep(0.05)  # the net runs at once: Gemini never hears the order
    assert len(said) == 1 and said[0][0] in bana0.OK_VARIANTER and said[0][1] == "kontoret"
    assert idle == ["bana0"]
    assert _kinds(google) == []
    assert [t for t, _, _ in seen if t == "transcribe"]


@pytest.mark.asyncio
async def test_gemini_klockan_google_hor_aldrig_fragan(ha_svarar, monkeypatch):
    monkeypatch.setenv("KLOCKA_KLIPP", "1")  # off by default since 2026-10-09; these test the code behind the flag
    """US-032 AC-7: "vad är klockan" on Gemini is said from the cached clips;
    neither comms nor Google hears it, so it works offline."""
    from app import klockan
    from test_gemini_provider import _kinds

    monkeypatch.setattr(klockan, "delar", lambda nu=None: ["Klockan är fjorton", "och tjugotvå minuter"])
    server, port, _ = await _wyoming(_transcript("Vad är klockan?"))
    connection, service, google, said, idle = _gemini_koppling(
        ("127.0.0.1", port), [None, "start", None, "end"])
    async with server:
        await _speak(connection, service, 4)
        await service._turn_end_task
        await asyncio.sleep(0.05)
    assert said == [("Klockan är fjorton och tjugotvå minuter", "kontoret")]
    assert idle == ["klockan"]
    assert _kinds(google) == []


@pytest.mark.asyncio
async def test_gemini_miss_ger_modellen_hela_turen(ha_svarar):
    from test_gemini_provider import _frame, _kinds

    ha_svarar.append(None)
    server, port, _ = await _wyoming(_transcript("hur varmt är det ute"))
    connection, service, google, said, idle = _gemini_koppling(
        ("127.0.0.1", port), [None, "start", None, "end"])
    async with server:
        await _speak(connection, service, 4)
        await service._turn_end_task
    assert said == []
    # start, pre-roll (frame 0), the three speech frames, end
    assert _kinds(google) == ["start", "audio", "audio", "audio", "audio", "end"]
    assert google[1]["audio"].data == _frame(0).audio


@pytest.mark.asyncio
async def test_gemini_utan_bana0_strommar_som_forut():
    from test_gemini_provider import _kinds

    connection, service, google, said, idle = _gemini_koppling(None, ["start", "end"])
    assert service.on_user_turn_end is None
    await _speak(connection, service, 2)
    assert _kinds(google) == ["start", "audio", "audio", "end"]


@pytest.mark.asyncio
async def test_gemini_tappad_tur_sags_hogt():
    """1011 mid-turn (2026-10-02 14:30:47): he gets a line, not silence."""
    from app.providers.gemini_live import TURN_LOST_LINE

    connection, service, google, said, idle = _gemini_koppling(None, ["start"])
    await _speak(connection, service, 1)

    async def no_push(*a, **k):
        return None

    service.push_error = no_push
    service._connection_start_time = time.time() - 5
    await service._handle_connection_error(Exception("1011 Internal error encountered."))
    for _ in range(3):
        await asyncio.sleep(0)
    assert said == [(TURN_LOST_LINE, "kontoret")]
    assert idle == ["turn-lost"]


@pytest.mark.asyncio
async def test_fraga_over_hans_eget_svar_tappar_inte_borjan():
    """Live 2026-10-09 12:09: he spoke while the phase still said "replying"; bana 0's audio
    lacked the first 0.8 s the engine had ('a question may have been cut')."""
    from app.raw_audio_serializer import TURN_PCM_PREROLL, RawAudioSerializer

    ser = RawAudioSerializer("koket", input_sample_rate=16000)
    ser.is_replying = lambda: True
    bit = b"\x01\x00" * 1600  # 100 ms
    for _ in range(TURN_PCM_PREROLL // len(bit) + 4):  # his own reply, then he starts to speak over it
        await ser.deserialize(bit)
    assert ser.start_turn_audio() is True  # the local VAD says he is speaking; the phase has not moved; the ring was used
    for _ in range(8):
        await ser.deserialize(bit)
    pcm = ser.take_turn_audio()
    assert len(pcm) == TURN_PCM_PREROLL + 8 * len(bit)  # pre-roll + everything since
    # Without the change, nothing was collected while "replying" and the turn was empty.


@pytest.mark.asyncio
async def test_vackning_over_svaret_samlar_traffen_aven_om_fasen_inte_flyttat_sig():
    from app.raw_audio_serializer import RawAudioSerializer

    ser = RawAudioSerializer("koket", input_sample_rate=16000)
    ser.is_replying = lambda: True
    await ser.deserialize(json.dumps({"type": "wake"}))
    await ser.deserialize(PCM)
    assert ser.take_turn_audio() == PCM
    await ser.deserialize(PCM)  # next turn, still replying and no new start: his own voice is not collected
    assert ser.take_turn_audio() == b""


# --- A whole command ends the turn at "preend" (raawr US-047) -----------------


@pytest.mark.parametrize("text,ok", [
    ("släck kontoret", True), ("Tänd lampan i köket.", True), ("vad är klockan", True),
    ("släck", False), ("tänd lampan i", False), ("släck köket och", False), ("slå på", False),
    ("vad är huvudstaden i frankrike", False), (None, False), ("", False),
    # raawr US-047, G's findings: unfinished or turned-around orders and plain words that are not light orders
    ("tänd inte", False), ("tänd inte köket", False), ("kan du tända", False), ("tänd lampan för", False),
    ("tänd lampan så", False), ("släck när", False), ("tänd köket och släck", False), ("släck köket och tänd", False),
    ("vi går på bio", False), ("jag tänkte på bio", False), ("ställ klockan på sju", False), ("stäng dörren", False),
    ("slå av", False), ("stäng av", False), ("slå på köket", True), ("stäng av taket", True),
])
def test_komplett_kommando(text, ok):
    assert bana0.komplett_kommando(text) is ok


@pytest.mark.asyncio
async def test_end_early_ends_the_turn_once_and_swallows_the_normal_end():
    """At "preend" a whole command ends the turn; the detector's own "end" a moment later must not
    start the decision again. A speaker who has started again (the pre-detector hears speech) is not cut."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    _wire(service, _ScriptedTurns(["start", None, "preend", None, "end", "start"]))
    avgjorde = []

    async def on_end():
        avgjorde.append(len(service._held or []))
        service._held = None

    service.on_user_turn_end = on_end
    for b in range(3):
        await service._send_user_audio(_frame(b))
    assert service.end_early() is True
    assert service.end_early() is False  # once per turn
    await asyncio.sleep(0.05)
    assert avgjorde == [4]  # pre-roll + three frames
    await service._send_user_audio(_frame(3))
    await service._send_user_audio(_frame(4))  # the normal "end": swallowed, no second decision
    await asyncio.sleep(0.05)
    assert avgjorde == [4]  # pre-roll + three frames
    assert service._tidigt is False
    await service._send_user_audio(_frame(5))  # the next "start" opens a fresh turn
    assert service._held is not None


@pytest.mark.asyncio
async def test_end_early_does_not_cut_a_speaker_who_resumed():
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    turns = _ScriptedTurns(["start", None])
    _wire(service, turns)
    service.on_user_turn_end = lambda: None
    for b in range(2):
        await service._send_user_audio(_frame(b))
    turns._pre_speaking = True
    assert service.end_early() is False


@pytest.mark.asyncio
async def test_end_early_is_refused_when_the_normal_end_already_decided_the_turn():
    """The early text can come after the normal end. Then the turn is decided already: no second decision,
    no second timing line (G, PR #53)."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    _wire(service, _ScriptedTurns(["start", None, "end"]))
    avgjorde = []

    async def on_end():
        avgjorde.append(1)

    service.on_user_turn_end = on_end
    for b in range(3):
        await service._send_user_audio(_frame(b))
    await asyncio.sleep(0.05)
    assert avgjorde == [1]
    service._held = [object()]  # the hook has not cleared it yet
    assert service.end_early() is False


@pytest.mark.asyncio
async def test_speech_that_resumes_after_an_early_end_opens_a_turn_of_its_own():
    """He goes on talking after the early end: the detector would never call that a new start. The
    continuation must not be thrown away with the decided turn (G, PR #53)."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    turns = _ScriptedTurns(["start", None, "preend", None, None])
    _wire(service, turns)

    async def on_end():
        service._held = None

    service.on_user_turn_end = on_end
    for b in range(3):
        await service._send_user_audio(_frame(b))
    assert service.end_early() is True
    await asyncio.sleep(0.05)
    turns._pre_speaking = True  # the pre-detector hears him again
    await service._send_user_audio(_frame(3))
    assert service._tidigt is False
    assert turns.resets == 1


@pytest.mark.asyncio
async def test_early_end_times_the_turn_from_the_end_of_speech_not_from_the_text():
    """The speech ended PRE_END_MS before "preend"; text that came 0.2 s later must not move it."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service
    from app.providers.local_turns import PRE_END_MS

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    _wire(service, _ScriptedTurns(["start", None, "preend"]))
    tystnad = []

    class Tider:
        def start(self, s):
            tystnad.append(s)

    service.tider = Tider()
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    for b in range(3):
        await service._send_user_audio(_frame(b))
    await asyncio.sleep(0.2)
    assert service.end_early() is True
    assert tystnad[0] >= PRE_END_MS / 1000 + 0.19


@pytest.mark.asyncio
async def test_continuation_after_an_early_end_gets_its_own_decision_when_the_hook_still_holds_the_turn():
    """G's second pass: bana 0 holds the decided turn (waiting for comms) while he goes on; the rest must be
    its own turn, decided after the first, not merged into the held audio that a hit throws away."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    turns = _ScriptedTurns(["start", None, "preend", None, "start", None, "end"])
    _wire(service, turns)
    frisl = asyncio.Event()
    avgjorde = []
    starter = []
    service.on_user_turn_start = lambda: starter.append(1)

    async def on_end():
        avgjorde.append(len(service._held or []))
        if len(avgjorde) == 1:
            await frisl.wait()
        service._held = None  # a hit: drop_turn

    service.on_user_turn_end = on_end
    for b in range(3):
        await service._send_user_audio(_frame(b))
    assert service.end_early() is True
    await asyncio.sleep(0.05)
    turns._pre_speaking = True
    for b in range(3, 7):  # the continuation: reset, start, quiet, end
        await service._send_user_audio(_frame(b))
    assert avgjorde == [4]  # still only the first decision; the continuation waits
    frisl.set()
    await asyncio.sleep(0.1)
    assert len(avgjorde) == 2 and avgjorde[1] >= 3  # its own turn, with its own frames
    assert starter == [1, 1]  # the first turn's start and the continuation's own


@pytest.mark.asyncio
async def test_a_drop_in_the_middle_of_the_continuation_keeps_what_was_said_of_it():
    """G's third pass: a hit drops the first turn while the continuation is being collected. The continuation
    must not be left half-collected: it reopens as a turn of its own with its frames as the pre-roll."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    turns = _ScriptedTurns(["start", None, "preend", None, None, "start"])
    _wire(service, turns)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    for b in range(3):
        await service._send_user_audio(_frame(b))
    assert service.end_early() is True
    await asyncio.sleep(0.05)
    service._held = [object()]  # bana 0 is still deciding the first turn
    turns._pre_speaking = True
    for b in range(3, 5):
        await service._send_user_audio(_frame(b))
    assert service._forts is not None and len(service._forts) == 2
    await service.drop_turn()  # a hit: the first turn is dropped
    assert service._forts is None and service._held is None
    assert bytes(service._preroll) == _frame(3).audio + _frame(4).audio
    await service._send_user_audio(_frame(5))  # still talking: the detector opens it again
    assert service._held is not None and _frame(3).audio in b"".join(f.audio for f in service._held)


@pytest.mark.asyncio
async def test_a_decision_that_ends_in_the_middle_of_the_continuation_hands_it_the_turn():
    """A's FAIL on PR #53: a miss or a ping clears the held turn without drop_turn while the continuation is
    collected. The continuation becomes the held turn, is decided at its own end, and the next turn is answered."""
    from test_gemini_provider import _ScriptedTurns, _frame, _options, _wire, OPENAI_SHAPE
    from app.providers import build_service

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    turns = _ScriptedTurns(["start", None, "preend", None, "start", None, "end", "start", None, "end"])
    _wire(service, turns)
    avgjorde = []

    async def on_end():
        avgjorde.append(len(service._held or []))
        service._held = None  # answer_turn: the miss path clears it, no drop_turn

    service.on_user_turn_end = on_end
    for b in range(3):
        await service._send_user_audio(_frame(b))
    assert service.end_early() is True
    await asyncio.sleep(0.05)
    assert avgjorde == [4] and service._held is None
    turns._pre_speaking = True
    for b in range(3, 7):  # the continuation after the first decision is over: reset, start, quiet, end
        await service._send_user_audio(_frame(b))
    await asyncio.sleep(0.05)
    assert len(avgjorde) == 2, "the continuation must be decided on its own"
    assert service._forts is None
    for b in range(7, 10):  # and the turn after that is answered too
        await service._send_user_audio(_frame(b))
    await asyncio.sleep(0.05)
    assert len(avgjorde) == 3


@pytest.mark.parametrize("text", ["Nej.", "Nej tack!", "Det var allt.", "tack", "Nä, tack", "Tack, det var allt.", "Tack, det räcker"])
def test_ar_nej_hela_turen(text):
    assert bana0.ar_nej(text)


@pytest.mark.parametrize("text", ["Nej, tänd köket", "Tack, vad blir det för väder", "Nej jag menade sovrummet", "", None])
def test_ar_nej_inte_en_fraga_med_nej_i(text):
    assert not bana0.ar_nej(text)


def _tur_nej(text, avsluta):
    async def stt(pcm, t):
        return text

    async def aldrig(*a):
        raise AssertionError("a nej must reach neither comms nor the model")

    return asyncio.run(bana0.tur(PCM, stt=stt, timeout_stt=1, timeout_comms=1,
                                 skicka_svar_till_modellen=aldrig, skapa_svar=aldrig, avsluta=avsluta))


def test_nej_stanger_utan_modell_och_utan_comms():
    stangd = []

    async def avsluta():
        stangd.append(1)

    assert _tur_nej("Nej tack.", avsluta) == "nej" and stangd == [1]


def test_nej_utan_avsluta_gar_vidare_till_modellen():
    assert _tur_nej("Nej tack.", None) == "modell"


@pytest.mark.asyncio
async def test_the_clear_stt_tells_a_miss_from_no_words(monkeypatch):
    """US-060 / A's FAIL on #65: 'heard nothing' and 'could not hear' are different answers."""
    from app import bana0

    async def tyst(pcm, host, port):
        return ""

    async def miss(pcm, host, port):
        return None

    async def fel(pcm, host, port):
        raise ConnectionError("down")
    monkeypatch.setattr(bana0, "_transkribera", tyst)
    assert await bana0.transkribera_tydligt(b"", "h", 1, 1.0) == ("", True)
    assert await bana0.transkribera(b"", "h", 1, 1.0) is None  # the old call is unchanged
    monkeypatch.setattr(bana0, "_transkribera", miss)
    assert await bana0.transkribera_tydligt(b"", "h", 1, 1.0) == (None, False)
    monkeypatch.setattr(bana0, "_transkribera", fel)
    assert await bana0.transkribera_tydligt(b"", "h", 1, 1.0) == (None, False)
