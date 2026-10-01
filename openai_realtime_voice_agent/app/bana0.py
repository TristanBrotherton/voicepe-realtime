"""Bana 0: the fast path for plain home commands (raawr US-016).

When a turn ends, the turn's audio goes to the local Wyoming STT, the text
goes to Home Assistant's own conversation agent -- through raawr-comms, which
stays the only door -- and only if HA did not handle it does the realtime
model get asked to answer. A hit never reaches the model as a request; the
model is only told what was said, so it knows the order is done.

Everything here is a plain function with injected callables, so the pipeline
wiring (main.py / websocket_handler.py) stays a few lines and the tests need
no live HA, STT or model.
"""
import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import httpx
from pipecat.services.openai.realtime import events

from app import ha_api

logger = logging.getLogger(__name__)

RATE, WIDTH, CHANNELS = 16000, 2, 1
CHUNK_BYTES = 3200  # 100 ms of 16 kHz PCM16 mono
AUDIO = {"rate": RATE, "width": WIDTH, "channels": CHANNELS}


def stt_adress(value: str) -> Optional[tuple[str, int]]:
    """The `bana0_stt` option, "host" or "host:port" (Wyoming default 10300). Empty or bad = off."""
    value = (value or "").strip()
    if not value:
        return None
    host, sep, port = value.rpartition(":")
    if not sep:
        return value, 10300
    try:
        return (host, int(port)) if host else None
    except ValueError:
        logger.warning(f"bana0: bad bana0_stt {value!r}, bana 0 off")
        return None


def _event(typ: str, data: Optional[dict] = None, payload: bytes = b"") -> bytes:
    header = {"type": typ, "data": data or {}}
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header).encode() + b"\n" + payload


async def _transkribera(pcm16k: bytes, host: str, port: int) -> Optional[str]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(_event("transcribe", {"language": "sv"}))
        writer.write(_event("audio-start", AUDIO))
        for i in range(0, len(pcm16k), CHUNK_BYTES):
            writer.write(_event("audio-chunk", AUDIO, pcm16k[i:i + CHUNK_BYTES]))
        writer.write(_event("audio-stop"))
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                return None  # server closed without a transcript
            header = json.loads(line)
            # Wyoming may put data and payload after the header line.
            data = header.get("data") or {}
            if header.get("data_length"):
                data = json.loads(await reader.readexactly(header["data_length"]))
            if header.get("payload_length"):
                await reader.readexactly(header["payload_length"])
            if header.get("type") == "transcript":
                return (data.get("text") or "").strip() or None
    finally:
        writer.close()


async def transkribera(pcm16k: bytes, host: str, port: int, timeout: float) -> Optional[str]:
    """Raw Wyoming over asyncio streams. None on timeout, error, or empty text."""
    try:
        return await asyncio.wait_for(_transkribera(pcm16k, host, port), timeout)
    except Exception as e:  # incl. TimeoutError: every failure is a miss
        logger.warning(f"bana0: STT failed: {e!r}")
        return None


async def prova(text: str, timeout: float) -> Optional[str]:
    """Ask HA's conversation agent via comms. HA's spoken reply on a hit, else None.

    200 -> response.speech.plain.speech (comms answers 200 with HA's own
    failure text too; that is spoken, never sent to the model). 204 means HA
    did not handle it. Anything else, an error or a timeout is a miss.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                ha_api.url("/conversation/process"),
                headers=ha_api.headers(),
                json={"text": text},
            )
        if r.status_code != 200:
            return None
        speech = r.json()["response"]["speech"]["plain"]["speech"]
        if not isinstance(speech, str):
            return None
        return speech.strip() or None
    except Exception as e:
        logger.warning(f"bana0: comms failed: {e!r}")
        return None


async def tur(
    pcm: bytes,
    *,
    stt: Callable[[bytes, float], Awaitable[Optional[str]]],
    timeout_stt: float,
    timeout_comms: float,
    say: Callable[[str], Awaitable[None]],
    skicka_svar_till_modellen: Callable[[str], Awaitable[None]],
    skapa_svar: Callable[[], Awaitable[None]],
) -> str:
    """One finished user turn. Returns 'bana0' on a hit, 'modell' otherwise.

    `stt(pcm, timeout)` is typically transkribera bound to host/port.
    """
    try:
        text = await stt(pcm, timeout_stt) if pcm else None
        svar = await prova(text, timeout_comms) if text else None
    except Exception as e:
        logger.warning(f"bana0: turn failed, model answers: {e!r}")
        svar = None
    if not svar:
        await skapa_svar()
        return "modell"
    # HA already acted: from here on the model must never be asked to answer,
    # or the room hears the order handled twice.
    logger.info(f"bana0: hit {text!r} -> {svar!r}")
    for steg in (say, skicka_svar_till_modellen):
        try:
            await steg(svar)
        except Exception as e:
            logger.warning(f"bana0: {getattr(steg, '__name__', steg)} failed after a hit: {e!r}")
    return "bana0"


async def lagg_till_svar(service, text: str) -> None:
    """Tell the realtime model what HA said, as an assistant item. No response."""
    await service.send_client_event(events.ConversationItemCreateEvent(
        item=events.ConversationItem(
            type="message", role="assistant",
            content=[events.ItemContent(type="output_text", text=text)],
        )
    ))


async def be_om_svar(service) -> None:
    """Bana 0 missed: ask the model to answer the turn (turn detection has create_response off)."""
    await service.send_client_event(events.ResponseCreateEvent())
