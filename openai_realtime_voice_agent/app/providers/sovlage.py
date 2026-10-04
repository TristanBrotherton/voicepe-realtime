"""Sleep mode: the cloud engine is connected only while a conversation is on.

raawr INKAST 2026-10-04 (urgent): the agent held one realtime session per
speaker open around the clock. xAI bills per connected minute and closes a
quiet session after 900 s; the agent reconnected in 0.5 s, all night, and a
silent house cost ~45 dollars in a day. Gemini Live connected at start too.

Now every engine's service starts asleep: pipecat's start() calls _connect(),
which is a no-op while `sover` is True. The device's wake connects it
(`vakna`, awaited in the wake handler, so the mic audio after the wake waits
in the socket instead of being lost), and ConnectionRecovery puts it back to
sleep (`sova`) once the conversation has been quiet for SOV_EFTER_S. A sleeping
service is never reconnected by anything: not the recovery, not xAI's idle
close, not Gemini's own reconnect.

MOLN_SOVLAGE=0 turns it off (always connected, as before 0.26.0).
"""
import logging
import os
import time

logger = logging.getLogger(__name__)


def sovlage_pa() -> bool:
    return os.environ.get("MOLN_SOVLAGE", "1").strip().lower() not in ("0", "false", "no", "off")


def sov_efter_s() -> float:
    """Quiet seconds after a conversation before the engine is disconnected."""
    try:
        return max(5.0, float(os.environ.get("SOV_EFTER_S", "30")))
    except ValueError:
        return 30.0


class SovlageMixin:
    """First in the MRO of every engine's service, so its _connect gates them all."""

    sover = False
    _vaknat_forut = False

    async def _connect(self, *args, **kwargs):  # type: ignore[override]
        if self.sover:
            logger.debug("💤 asleep: no connection to the cloud engine")
            return
        await super()._connect(*args, **kwargs)

    async def vakna(self) -> bool:
        """Connect now (the wake word was heard). True if it was asleep."""
        if not self.sover:
            return False
        self.sover = False
        t0 = time.monotonic()
        try:
            await self._ateranslut(self._vaknat_forut)
        except Exception as e:
            logger.error(f"❌ could not connect to the cloud engine on wake: {e!r}")
        self._vaknat_forut = True
        logger.info(f"☁️ connected to the cloud engine on wake ({time.monotonic() - t0:.1f}s)")
        return True

    async def sova(self, reason: str) -> bool:
        """Disconnect; nothing reconnects until the next wake. True if it was awake."""
        if self.sover:
            return False
        self.sover = True  # first: the closing socket's errors are then ignored
        try:
            await self._disconnect()
        except Exception as e:
            logger.warning(f"⚠️ disconnect on sleep failed: {e!r}")
        logger.info(f"💤 disconnected from the cloud engine ({reason})")
        return True

    async def _ateranslut(self, forut: bool) -> None:
        """Engine-specific connect; `forut` = it was connected before (keep the conversation)."""
        await self._connect()
