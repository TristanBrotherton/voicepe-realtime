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
