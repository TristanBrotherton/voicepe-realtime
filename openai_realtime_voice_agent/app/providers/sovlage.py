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

The budget (0.26.1, the owner 2026-10-04: "allt vi gör framåt behöver
försiktighet"): connected minutes are counted per day, for every engine and
speaker together, in MOLN_LEDGER. Past MOLN_MAX_MINUTER_PER_DAG (60) a wake
does not connect and an open session is put to sleep. A bug elsewhere can then
cost at most that many minutes a day, whatever the provider charges.
"""
import datetime
import json
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


def max_sekunder_per_dag() -> float:
    try:
        return max(0.0, float(os.environ.get("MOLN_MAX_MINUTER_PER_DAG", "60"))) * 60.0
    except ValueError:
        return 3600.0


class Budget:
    """Connected seconds per local day, on disk so a restart does not reset them."""

    def __init__(self, path=None, today=None):
        self.path = path or os.environ.get("MOLN_LEDGER", "/data/moln_minuter.json")
        self._today = today or (lambda: datetime.date.today().isoformat())

    def _read(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def anvant(self) -> float:
        return float(self._read().get(self._today(), 0.0))

    def lagg_till(self, sekunder: float) -> None:
        if sekunder <= 0:
            return
        data = self._read()
        day = self._today()
        data = {day: float(data.get(day, 0.0)) + sekunder}  # only today is kept
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(data, f)
        except OSError as e:
            logger.warning(f"⚠️ cloud budget not saved: {e!r}")


BUDGET = Budget()


class SovlageMixin:
    """First in the MRO of every engine's service, so its _connect gates them all."""

    sover = False
    _vaknat_forut = False
    _uppkopplad_sedan = None
    budget = BUDGET

    def oppen_tid(self) -> float:
        return 0.0 if self._uppkopplad_sedan is None else time.monotonic() - self._uppkopplad_sedan

    def over_budget(self) -> bool:
        return self.budget.anvant() + self.oppen_tid() >= max_sekunder_per_dag()

    async def _connect(self, *args, **kwargs):  # type: ignore[override]
        if self.sover:
            logger.debug("💤 asleep: no connection to the cloud engine")
            return
        await super()._connect(*args, **kwargs)

    async def vakna(self) -> bool:
        """Connect now (the wake word was heard). True if it was asleep."""
        if not self.sover:
            return False
        if self.over_budget():
            logger.warning(
                f"💸 cloud budget for today used ({self.budget.anvant() / 60:.0f} of "
                f"{max_sekunder_per_dag() / 60:.0f} min) — not connecting"
            )
            return False
        self.sover = False
        self._uppkopplad_sedan = time.monotonic()
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
        self.budget.lagg_till(self.oppen_tid())
        self._uppkopplad_sedan = None
        try:
            await self._disconnect()
        except Exception as e:
            logger.warning(f"⚠️ disconnect on sleep failed: {e!r}")
        logger.info(f"💤 disconnected from the cloud engine ({reason})")
        return True

    async def _ateranslut(self, forut: bool) -> None:
        """Engine-specific connect; `forut` = it was connected before (keep the conversation)."""
        await self._connect()
