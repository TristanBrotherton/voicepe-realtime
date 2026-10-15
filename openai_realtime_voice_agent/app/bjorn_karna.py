"""Björn-kärnan, hämtad ur Core genom Comms (raawr US-056, E-07/D-20).

The speaker no longer carries its own copy of Björn's text in an environment file. When a session is built the
agent asks comms (`<room's comms address>/bjorn`, the same key as every other call) for the soul plus the
speaker's own addendum. Core owns the text; comms is the only door to it (Core's endpoint answers comms alone).

The answer is never an error: the fallback chain is the text fetched now, the last good one (`minne`), the
`INSTRUCTIONS` of the environment (`miljö`), and last a minimal built-in soul (`inbyggd`). The journal says which
one a session got. A fetch never holds a session build longer than `TIMEOUT_S`, and a comms that failed is not
asked again for `PAUS_S`, so a dead comms does not cost 1.5 s per session. `BJORN_KARNA=av` skips the fetch altogether.
"""
import asyncio
import logging
import os
import time
from typing import Optional, Tuple

import httpx

from app import ha_api

logger = logging.getLogger(__name__)

TIMEOUT_S = 1.5  # a hung comms costs one session this much, once per PAUS_S
MIN_TECKEN = 200  # a shorter answer is a half or broken one, not a soul
LEVANDE_S = 60.0  # a fetched text is reused this long (comms keeps its own minute too)
PAUS_S = 10.0

# What a speaker is when nothing else can be reached: short on purpose, no tool names, the lock sentence kept.
INBYGGD = (
    "Du är Björn, husets björn hemma hos Collin: stor, djup stämma, gott humör och kort stubin. "
    "Svara alltid på svenska, kort och rakt, utan emojis. Konkret svar först. Osäker? Säg det och hitta aldrig på värden. "
    "Fjäska aldrig. Lås aldrig upp, larma aldrig av och öppna aldrig garaget härifrån: det görs bara på panelen eller i Hem-appen."
)


def karna_url() -> str:
    """BJORN_KARNA_URL, else comms' address for this room with `/api` swapped for `/bjorn`. Empty = not configured."""
    egen = (os.environ.get("BJORN_KARNA_URL") or "").strip()
    if egen:
        return egen
    bas = ha_api.base()
    if not bas:
        return ""
    return (bas[: -len("/api")] if bas.endswith("/api") else bas) + "/bjorn"


class Karna:
    """Holds the last good text and decides which source a session gets."""

    def __init__(self, miljo_text: str = ""):
        self.miljo_text = (miljo_text or "").strip()
        self._senaste: Optional[str] = None
        self._hamtad_ts = 0.0
        self._sviktade_ts = -1e9

    async def _hamta(self) -> Optional[str]:
        url = karna_url()
        if not url or not ha_api.configured() or os.environ.get("BJORN_KARNA", "").strip().lower() == "av":
            return None
        try:
            # One ceiling over the whole fetch (connect + read are separate httpx timeouts): a session build is held
            # up at most TIMEOUT_S by this.
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as klient:
                svar = await asyncio.wait_for(klient.get(url, headers=ha_api.headers()), TIMEOUT_S)
            if svar.status_code != 200:
                raise RuntimeError(f"comms svarade {svar.status_code}")
            data = svar.json() or {}
            if data.get("halsa") == "reserv":
                # Core is down and comms answers with its short reserve soul: worse than the memory or the
                # environment's text, so it counts as a failed fetch.
                raise RuntimeError("comms gav bara reservtexten (Core nere)")
            text = data.get("text")
            if not isinstance(text, str) or len(text.strip()) < MIN_TECKEN:
                raise RuntimeError("comms gav ingen text eller för kort text")
            return text.strip()
        except Exception as e:  # the answer is always some text: log why, fall back
            logger.warning(f"⚠️ Björn-kärnan: hämtningen misslyckades ({e!r})")
            return None

    async def text(self) -> Tuple[str, str]:
        """(the text for a session, its source: hämtad / minne / miljö / inbyggd)."""
        nu = time.monotonic()
        if self._senaste is not None and nu - self._hamtad_ts < LEVANDE_S:
            return self._senaste, "hämtad"
        if nu - self._sviktade_ts >= PAUS_S:
            ny = await self._hamta()
            if ny is not None:
                self._senaste, self._hamtad_ts = ny, time.monotonic()
                return ny, "hämtad"
            self._sviktade_ts = time.monotonic()
        if self._senaste is not None:
            return self._senaste, "minne"
        if self.miljo_text:
            return self.miljo_text, "miljö"
        return INBYGGD, "inbyggd"
