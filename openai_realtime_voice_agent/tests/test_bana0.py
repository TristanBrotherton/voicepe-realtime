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

    async def say(text):
        said.append(text)

    return await bana0.tur(
        PCM, stt=stt, timeout_stt=timeout_stt, timeout_comms=4.0, say=say,
        skicka_svar_till_modellen=lambda t: bana0.lagg_till_svar(service, t),
        skapa_svar=lambda: bana0.be_om_svar(service),
    )


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

    assert said == ["Tände lampan i kontoret"]
    assert service.typer() == ["conversation.item.create"]
    item = service.events[0].item
    assert (item.role, item.content[0].type, item.content[0].text) == (
        "assistant", "output_text", "Tände lampan i kontoret")


@pytest.mark.asyncio
async def test_miss_gar_till_modellen(comms):
    comms.svar = httpx.Response(204)
    server, port, _ = await _wyoming(_transcript("vad är klockan"))
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
    assert said == ["Tyvärr, jag kunde inte nå lampan"]
    assert "response.create" not in service.typer()


@pytest.mark.asyncio
async def test_traff_dar_talet_kraschar_ber_aldrig_modellen_svara(comms):
    comms.svar = _ha("Tände lampan")
    server, port, _ = await _wyoming(_transcript("tänd lampan"))
    service = FakeService()

    async def stt(pcm, timeout):
        return await bana0.transkribera(pcm, "127.0.0.1", port, timeout)

    async def say(text):
        raise RuntimeError("TTS nere")

    async with server:
        assert await bana0.tur(
            PCM, stt=stt, timeout_stt=1.0, timeout_comms=4.0, say=say,
            skicka_svar_till_modellen=lambda t: bana0.lagg_till_svar(service, t),
            skapa_svar=lambda: bana0.be_om_svar(service),
        ) == "bana0"
    assert service.typer() == ["conversation.item.create"]


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
async def test_speech_stopped_med_traff_ger_inget_response_create_och_ett_say(comms):
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
    assert [e.type for e in sent] == ["conversation.item.create"]
    assert said == [("Tände lampan i kontoret", "kontoret")]
    assert idle == ["bana0"]


@pytest.mark.asyncio
async def test_speech_stopped_med_miss_ber_modellen_svara(comms):
    comms.svar = httpx.Response(204)
    server, port, _ = await _wyoming(_transcript("vad är klockan"))
    handler, connection, service, sent = _koppling(("127.0.0.1", port))
    await connection.serializer.deserialize(PCM)
    async with server:
        await service._handle_evt_speech_stopped(None)
        await service._turn_end_task
    assert [e.type for e in sent] == ["response.create"]


@pytest.mark.asyncio
async def test_bana0_av_ingen_krok_och_inga_egna_handelser():
    handler, connection, service, sent = _koppling(None)
    assert service.on_user_turn_end is None
    await service._handle_evt_speech_stopped(None)
    assert service._turn_end_task is None
    assert sent == []
