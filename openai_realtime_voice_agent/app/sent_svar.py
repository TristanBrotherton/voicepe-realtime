"""A late answer: the speaker asks first, and reads it only on a yes (raawr US-051; the door is US-052's).

Comms posts {rum, result_id, agent, amne, amnesord, created_at} (never the text) to `POST /sent-svar` when a
specialist finished after the user moved on. The agent answers {"fragar": true|false} at once (Comms waits 2 s) and,
on true, says "<agent> har mer om <amne>. Vill du höra det?" through the guarded TTS lane. The device then holds its
follow-up mic open; the mic audio is collected HERE, not sent to the engine, so no cloud session opens for the
question or the yes/no (AC-5). The words are heard by the local speech-to-text (bana 0's Wyoming server) and read
against two fixed word lists (the model never judges them). Yes: the text is fetched from comms
(`POST <comms>/kanal/rost/<rum>/svar {result_id}` -> {"text"}) and spoken. No, anything else, or silence: nothing is
read and the answer stays at Core.

Quiet hours 00:00-07:00 Swedish local time (`Europe/Stockholm`), a conversation under way, an unknown room or an
unconfigured speech-to-text: fragar false, nothing is said, and the notice is remembered so the morning question
(AC-6) can ask once. `SENT_SVAR_NU=HH:MM` fakes the clock for the self-test (tools/sent_svar_prov.py).
"""
import asyncio
import datetime
import logging
import os
import time
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

from app import ha_api
from app.avbrott import dbfs

logger = logging.getLogger(__name__)

JA = {"ja", "ja tack", "kör", "gärna", "okej", "visst", "läs", "berätta"}
NEJ = {"nej", "nej tack", "inte nu", "senare", "strunta i det"}
TYSTA_FRAN, TYSTA_TILL = 0, 7
LYSSNA_MAX_S = 9.0     # from the question's end: the follow-up window is 6-8 s
TAL_DBFS = -45.0       # a 20-640 ms chunk above this is speech
TAL_SLUT_S = 1.0       # quiet after speech that closes the answer
SPARA_MAX = 10         # notices remembered per room for the morning question
TAK_SVAR_S = 24 * 3600
MORGON_TAK = 3         # AC-6: at most three answers are read per yes
MORGON_FRAGA = "Ska jag ge dig det jag sparat?"


def fraga_text(agent: str, amne: str) -> str:
    return f"{agent} har mer om {amne}. Vill du höra det?"


def tolka(text: Optional[str]) -> Optional[str]:
    """'ja', 'nej' or None. Only a text that IS a yes or a no counts; "Ha" or a longer sentence is no answer."""
    t = " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in (text or "").lower()).split())
    return "ja" if t in JA else "nej" if t in NEJ else None


def nu() -> datetime.datetime:
    """Swedish local time now. SENT_SVAR_NU=HH:MM fakes the time of day (the self-test only)."""
    fake = os.environ.get("SENT_SVAR_NU", "").strip()
    n = datetime.datetime.now(ZoneInfo("Europe/Stockholm"))
    if fake:
        h, m = fake.split(":")
        n = n.replace(hour=int(h), minute=int(m))
    return n


def tyst_timme() -> bool:
    return TYSTA_FRAN <= nu().hour < TYSTA_TILL


def svar_url() -> str:
    """comms' answer door for this room: HA_API_URL ends in /kanal/rost/<rum>/api, the door in /kanal/rost/<rum>/svar."""
    b = ha_api.base()
    return (b[:-4] if b.endswith("/api") else b) + "/svar"


async def hamta_svar(result_id: str, timeout: float = 5.0) -> Optional[str]:
    """The answer text from comms, or None (404 inget_svar, a refused key, a timeout)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(svar_url(), headers=ha_api.headers(), json={"result_id": result_id})
        if r.status_code != 200:
            logger.info(f"📬 sent svar: comms gave {r.status_code} for {result_id}")
            return None
        return (r.json().get("text") or "").strip() or None
    except Exception as e:
        logger.warning(f"📬 sent svar: fetching {result_id} failed: {e!r}")
        return None


class Lyssnare:
    """Collects the mic audio of one answer: ends when speech was followed by TAL_SLUT_S of quiet, on the device's
    flush (the follow-up window closed), or after LYSSNA_MAX_S. `pcm` is what was heard (16 kHz mono 16-bit)."""

    def __init__(self):
        self.pcm = bytearray()
        self.tal = False
        self._tyst_bytes = 0
        self.klar = asyncio.Event()

    def mata(self, pcm: Optional[bytes]) -> None:
        if pcm is None:  # the device's flush: the window is over
            self.klar.set()
            return
        self.pcm += pcm
        if dbfs(pcm) > TAL_DBFS:
            self.tal, self._tyst_bytes = True, 0
        elif self.tal:
            self._tyst_bytes += len(pcm)
            if self._tyst_bytes >= TAL_SLUT_S * 32000:
                self.klar.set()

    async def lyssna(self, max_s: float = LYSSNA_MAX_S) -> bytes:
        try:
            await asyncio.wait_for(self.klar.wait(), max_s)
        except asyncio.TimeoutError:
            pass
        return bytes(self.pcm)


class SentSvar:
    """One per agent. `tala(text, device_id)` speaks through the guarded lane; `stt(pcm)` is the local speech-to-text
    (text or None); `lat_fraga(device_id)` returns (serializer, samtal_pagar) for a connected device or None."""

    def __init__(self, tala: Callable[..., Awaitable], stt: Callable[[bytes], Awaitable[Optional[str]]],
                 enhet: Callable[[str], Optional[tuple]], hamta: Callable[[str], Awaitable[Optional[str]]] = hamta_svar):
        self._tala, self._stt, self._enhet, self._hamta = tala, stt, enhet, hamta
        self.sparade: dict[str, list[dict]] = {}   # held notices per room: the morning question reads these
        self._pagar: set[str] = set()              # rooms with a question under way
        self._morgon_fragad: dict[str, datetime.date] = {}  # the day each room was asked the morning question
        self._uppgifter: set = set()

    def _spara(self, rum: str, notis: dict) -> None:
        lista = self.sparade.setdefault(rum, [])
        lista[:] = [n for n in lista if n["result_id"] != notis["result_id"]
                    and time.time() - n["_t"] < TAK_SVAR_S][-(SPARA_MAX - 1):]
        lista.append({**notis, "_t": time.time()})

    async def notis(self, body: dict) -> dict:
        """The answer to Comms' POST. Never raises."""
        try:
            rum, rid = str(body["rum"]), str(body["result_id"])
            agent, amne = str(body.get("agent") or "Björn"), str(body.get("amne") or "något")
        except (KeyError, TypeError):
            return {"fragar": False, "orsak": "ogiltig"}
        grund = {"rum": rum, "result_id": rid, "agent": agent, "amne": amne}
        enhet = self._enhet(rum)
        if enhet is None:
            return {"fragar": False, "orsak": "okand_enhet"}
        serializer, samtal_pagar = enhet
        orsak = ("natt" if tyst_timme() else "samtal" if samtal_pagar else "upptagen" if rum in self._pagar
                 else "ingen_stt" if self._stt is None else None)
        if orsak:
            self._spara(rum, grund)
            logger.info(f"📬 sent svar {rid} ({rum}): frågas inte nu ({orsak}), sparat")
            return {"fragar": False, "orsak": orsak}
        self._pagar.add(rum)
        t = asyncio.get_running_loop().create_task(self._fraga(grund, serializer))
        self._uppgifter.add(t)
        t.add_done_callback(self._uppgifter.discard)
        return {"fragar": True}

    async def _hor_ja_nej(self, rum: str, serializer, fraga: str) -> tuple[Optional[str], Optional[str]]:
        """Say `fraga`, take the mic from the engine, hear the answer locally: (text, 'ja'|'nej'|None)."""
        lyssnare = Lyssnare()
        serializer.fraga_tap = lyssnare.mata  # before the question: the engine hears none of it
        try:
            await self._tala(fraga, rum)
            pcm = await lyssnare.lyssna()
            text = await self._stt(pcm) if lyssnare.tal else None
            return text, tolka(text)
        finally:
            serializer.fraga_tap = None

    async def _fraga(self, g: dict, serializer) -> None:
        rum, rid = g["rum"], g["result_id"]
        try:
            text, svar = await self._hor_ja_nej(rum, serializer, fraga_text(g["agent"], g["amne"]))
            logger.info(f"📬 sent svar {rid} ({rum}): hörde {text!r} -> {svar or 'inget svar'}")
            if svar == "ja":
                await self._las(rum, rid)
            else:
                self._spara(rum, g)  # a no or silence leaves it unread: the morning question may ask again
        except Exception as e:
            logger.warning(f"📬 sent svar {rid} ({rum}) failed: {e!r}")
        finally:
            self._pagar.discard(rum)

    async def _las(self, rum: str, rid: str) -> bool:
        läst = await self._hamta(rid)
        await self._tala(läst or "Det svaret finns inte kvar.", rum)
        return bool(läst)

    async def morgon(self, rum: str) -> bool:
        """AC-6 (decision C 2026-10-11, Henrik may change it): after the first reply of a session at or after 07:00,
        once per room and day, ask "Ska jag ge dig det jag sparat?" if unread answers are held. Yes: read at most three,
        the oldest first, one at a time; no or silence: they stay. Never raises; True when a question was asked."""
        try:
            enhet = self._enhet(rum)
            idag = nu().date()
            nya = [n for n in self.sparade.get(rum, []) if time.time() - n["_t"] < TAK_SVAR_S]
            if (enhet is None or self._stt is None or tyst_timme() or not nya or rum in self._pagar
                    or self._morgon_fragad.get(rum) == idag):
                return False
            self._morgon_fragad[rum] = idag  # asked once, whatever the answer
            self._pagar.add(rum)
            try:
                text, svar = await self._hor_ja_nej(rum, enhet[0], MORGON_FRAGA)
                logger.info(f"📬 morgonfråga ({rum}, {len(nya)} sparade): hörde {text!r} -> {svar or 'inget svar'}")
                if svar == "ja":
                    nya.sort(key=lambda n: n["_t"])
                    for n in nya[:MORGON_TAK]:
                        await self._las(rum, n["result_id"])
                        self.sparade[rum] = [m for m in self.sparade[rum] if m["result_id"] != n["result_id"]]
                    if len(nya) > MORGON_TAK:
                        await self._tala("Det finns fler.", rum)
            finally:
                self._pagar.discard(rum)
            return True
        except Exception as e:
            logger.warning(f"📬 morgonfråga ({rum}) failed: {e!r}")
            return False
