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
import inspect
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

# Without internet (raawr US-018) the cloud TTS cannot render HA's reply or an
# answer. These two lines are rendered at startup (main._warm_early_acks) and
# cached on disk, so they play offline. Never a question mark: it opens the mic.
OK_FALLBACK = "Klart."
OFFLINE_LINE = "Jag når inte nätet just nu. Lampor och sånt fungerar ändå."
LOKALA_REPLIKER = (OK_FALLBACK, OFFLINE_LINE)


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
    skicka_svar_till_modellen: Callable[[str], Awaitable[None]],
    skapa_svar: Callable[[], Awaitable[None]],
    efter_miss: Optional[Callable[[], None]] = None,
    efter_traff: Optional[Callable[[], None]] = None,
) -> str:
    """One finished user turn. Returns 'bana0' on a hit, 'modell' otherwise.

    `stt(pcm, timeout)` is typically transkribera bound to host/port.
    On a hit HA's own reply is NOT spoken (the owner 2026-10-03: "hellre tyst
    än den torra"): the model is told what was done and confirms in its own
    words, with no tools (`skicka_svar_till_modellen`). `efter_traff()` is the
    net when the model stays silent (vakta_bekraftelse, US-018).
    `efter_miss()` runs after the model was asked (vakta_natet, US-018).
    """
    try:
        text = await stt(pcm, timeout_stt) if pcm else None
        svar = await prova(text, timeout_comms) if text else None
    except Exception as e:
        logger.warning(f"bana0: turn failed, model answers: {e!r}")
        svar = None
    if not svar:
        try:
            await skapa_svar()
        except Exception as e:  # no model socket: efter_miss says why
            logger.warning(f"bana0: asking the model failed: {e!r}")
        if efter_miss is not None:
            efter_miss()
        return "modell"
    # HA already acted: the model may only confirm it, never do it again.
    logger.info(f"bana0: hit {text!r} -> {svar!r}")
    try:
        await skicka_svar_till_modellen(gjort(text, svar))
    except Exception as e:
        logger.warning(f"bana0: telling the model failed after a hit: {e!r}")
    if efter_traff is not None:
        efter_traff()
    return "bana0"


def gjort(text: str, svar: str) -> str:
    """What the model is told after a hit: the order, HA's reply, and what to do."""
    return (
        f"Användaren sa: \"{text}\". Huset har REDAN gjort det; Home Assistant svarade: "
        f"\"{svar}\". Bekräfta kort med egna ord, en mening. Anropa inga verktyg och gör inget mer."
    )


def ingen_bekraftelse_an(liveness, asked: float) -> bool:
    """The model has not spoken since the hit and he has not started a new utterance.

    Not claim_silence_ack: bana 0 forces the phase idle right after a hit,
    which counts as "turn over" there and silenced the net every time
    (live 2026-10-04 17:14, Gemini: the lamp went off, nothing was said).
    """
    return not liveness.model_spoke_since(asked, 0.0) and liveness.user_started_at <= asked


async def vakta_bekraftelse(
    *,
    vanta_s: float,
    claim: Callable[[], bool],
    say_ok: Callable[[], Awaitable[None]],
) -> bool:
    """After a hit: if the model has said nothing after `vanta_s`, say OK_FALLBACK.

    Offline the model never answers; the room still hears it was done, in the
    engine's own voice (cached clip). True when the clip was said.
    """
    await asyncio.sleep(vanta_s)
    if not claim():
        return False
    logger.warning(f"bana0: no confirmation from the model, saying {OK_FALLBACK!r}")
    try:
        await say_ok()
    except Exception as e:
        logger.warning(f"bana0: fallback confirmation failed: {e!r}")
        return False
    return True


async def natet_nere(probe: Callable[[str], bool], engines, timeout: float) -> bool:
    """True when NO engine's API answers within `timeout`.

    An async probe is awaited on this loop. A sync probe still runs in a
    thread, so a blocking GET cannot freeze the loop. Every engine, not just
    the one running: an xAI-only outage is a failover for the router, not
    "no internet" for the room. A probe that hangs or raises counts as down
    for that engine.
    """
    async def one(engine) -> bool:
        try:
            if inspect.iscoroutinefunction(probe):
                return bool(await asyncio.wait_for(probe(engine), timeout))
            return bool(await asyncio.wait_for(asyncio.to_thread(probe, engine), timeout))
        except Exception:
            return False

    engines = [e for e in dict.fromkeys(engines) if e]
    if not engines:
        return False
    return not any(await asyncio.gather(*(one(e) for e in engines)))


async def vakta_natet(
    *,
    natet_nere: Callable[[], Awaitable[bool]],
    claim: Callable[[], bool],
    say: Callable[[str], Awaitable[None]],
) -> bool:
    """After a miss: if the model's engine cannot be reached, say OFFLINE_LINE.

    `claim()` is the turn's once-only silence slot (TurnLiveness.claim_silence_ack):
    false when the model already spoke or the "Ett ögonblick" ack took it, so
    the room never hears both. True when the line was said.
    """
    try:
        if not await natet_nere():
            return False
    except Exception as e:
        logger.warning(f"bana0: engine probe raised: {e!r}")
    if not claim():
        return False
    logger.warning("bana0: the engine is unreachable, saying so")
    try:
        await say(OFFLINE_LINE)
    except Exception as e:
        logger.warning(f"bana0: offline line failed: {e!r}")
        return False
    return True


async def be_om_bekraftelse(service, besked: str) -> None:
    """After a hit: tell the model what was done and ask for a short spoken confirmation.

    A system item, then response.create with no tools, so the model cannot
    act on the order a second time.
    """
    await service.send_client_event(events.ConversationItemCreateEvent(
        item=events.ConversationItem(
            type="message", role="system",
            content=[events.ItemContent(type="input_text", text=besked)],
        )
    ))
    await service.send_client_event(events.ResponseCreateEvent(
        response=events.ResponseProperties(tool_choice="none")
    ))


async def be_om_svar(service) -> None:
    """Bana 0 missed: ask the model to answer the turn (turn detection has create_response off)."""
    await service.send_client_event(events.ResponseCreateEvent())
