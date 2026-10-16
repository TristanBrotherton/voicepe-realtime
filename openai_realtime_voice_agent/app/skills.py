"""The speaker's tools through comms' skill catalogue (raawr US-095, E-07/D-27 point 5).

Gemini Live cannot change its tools inside a session (pipecat only does it by reconnecting), so with
ROST_SKILLS on the session carries a fixed few: comms' `las_skill` and `anvand_verktyg` (raawr US-089) and
the speaker's own skills, which act on the speaker itself and not on the house. Everything about the house goes
through `anvand_verktyg`, and its locks (lock, alarm, garage, the rules per channel) sit in comms, never here.

ROST_SKILLS: empty/`av` = today's tools; `b` = only the two meta tools; `c` = the two plus `hus_lage` and
`styr` directly (the measurement of US-095 picks b or c). The catalogue (`<room's comms>/skills`, one line per
skill) is fetched when a session is built and goes last in the instruction; when it cannot be had, the last good
one is used, else one sentence that the house cannot be reached right now.
"""
import asyncio
import logging
import os
from typing import Dict, List, Optional, Tuple

import httpx

from app import ha_api

logger = logging.getLogger(__name__)

TIMEOUT_S = 1.0  # a hung catalogue costs one session build this much
META = frozenset({"las_skill", "anvand_verktyg"})
HETA = frozenset({"hus_lage", "styr"})
# The speaker's own skills: they act on this speaker or this conversation, so they stay here (D-27 point 5).
EGNA = frozenset({"request_follow_up", "mark_false_wake", "voice_enrollment", "set_timer", "cancel_timer",
                  "list_timers"})
# What comms' door listed live 2026-10-16 (journal, Kontor). With ROST_SKILLS off the session keeps exactly these
# from the door: the door is growing with Grok's tools (raawr US-083..094), and each one the speaker is handed on
# top of HA's own would only lengthen a list that is already 51 long and duplicate HA (styr next to HassTurnOn).
DORR_IDAG = frozenset({"folj_upp", "fraga_huset", "hamta_svar", "kasta_in", "lage_drift", "lista_svar", "mac_byggen",
                       "svara"})
NAS_INTE = ("Husets verktyg nås inte just nu. Får du en fråga om huset, säg kort att du inte når huset "
            "just nu och be personen försöka igen om en stund.")


def lage() -> str:
    """'b', 'c', or '' (off)."""
    v = os.environ.get("ROST_SKILLS", "").strip().lower()
    return v if v in ("b", "c") else ""


def behall(namn: str) -> bool:
    """Whether the model is offered `namn` in skills mode. comms may hand a tool out as `<domain>__<name>`."""
    bas = namn.rsplit("__", 1)[-1]
    return namn in EGNA or bas in META or (lage() == "c" and bas in HETA)


def visa_fran_dorren(namn: str) -> bool:
    """With ROST_SKILLS off: whether a tool the door lists goes on the session (today's few, nothing new)."""
    return namn.rsplit("__", 1)[-1] in DORR_IDAG


def katalog_url() -> str:
    """The room's comms address with `/api` swapped for `/skills`; empty = not configured."""
    bas = ha_api.base()
    if not bas.endswith("/api"):
        return ""
    return bas[: -len("/api")] + "/skills"


def block(skills: List[Tuple[str, str]]) -> str:
    rader = "\n".join(f"- {namn}: {rad}" for namn, rad in skills)
    return ("\n\nHUSETS SKILLS. Läs en skill med las_skill innan du använder dess verktyg, kör sedan "
            "verktyget med anvand_verktyg. Säger svaret nej, säg det kort och gå vidare.\n" + rader)


class Katalog:
    """The last good catalogue per room, so a comms hiccup does not take the house away from a session."""

    def __init__(self):
        self._senaste: Dict[str, str] = {}

    async def _hamta(self) -> Optional[List[Tuple[str, str]]]:
        url = katalog_url()
        if not url or not ha_api.configured():
            return None
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as klient:
                svar = await asyncio.wait_for(klient.get(url, headers=ha_api.headers()), TIMEOUT_S)
            if svar.status_code != 200:
                raise RuntimeError(f"comms svarade {svar.status_code}")
            skills = [(str(s["namn"]), str(s["rad"])) for s in (svar.json() or {}).get("skills", [])
                      if s.get("namn") and s.get("rad")]
            if not skills:
                raise RuntimeError("tom katalog")
            return skills
        except Exception as e:  # a session is never held up over the catalogue: log, fall back
            logger.warning(f"⚠️ skills: katalogen kunde inte hämtas ({e!r})")
            return None

    async def text(self, rum: str) -> str:
        skills = await self._hamta()
        if skills:
            self._senaste[rum] = block(skills)
            logger.info(f"🧩 skills ({rum}): {len(skills)} i katalogen: {[n for n, _ in skills]}")
            return self._senaste[rum]
        if rum in self._senaste:
            logger.info(f"🧩 skills ({rum}): senast hämtade katalogen används")
            return self._senaste[rum]
        return "\n\n" + NAS_INTE
