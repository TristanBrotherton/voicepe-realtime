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
import tempfile
import time
import weakref

logger = logging.getLogger(__name__)


def sovlage_pa() -> bool:
    return os.environ.get("MOLN_SOVLAGE", "1").strip().lower() not in ("0", "false", "no", "off")


def sov_efter_s() -> float:
    """Quiet seconds after a conversation before the engine is disconnected."""
    try:
        return max(5.0, float(os.environ.get("SOV_EFTER_S", "30")))
    except ValueError:
        return 30.0


def max_sekunder_per_samtal() -> float:
    """VOICE_SESSION_MAX_SECONDS (600): a hard cap on one connection, audio or not.

    Henrik 2026-10-04, on top of sleep mode and the daily budget: a session
    that never goes quiet (an open mic, a TV, a stuck device) is cut anyway.
    """
    try:
        return max(30.0, float(os.environ.get("VOICE_SESSION_MAX_SECONDS", "600")))
    except ValueError:
        return 600.0


def max_sekunder_per_dag(prov: bool = False) -> float:
    """The owner's cap (60 min) for the real speakers; the test stand-ins have
    their own (MOLN_MAX_MINUTER_PROV_PER_DAG, 30 min), US-032."""
    namn, standard = ("MOLN_MAX_MINUTER_PROV_PER_DAG", 30.0) if prov else ("MOLN_MAX_MINUTER_PER_DAG", 60.0)
    try:
        return max(0.0, float(os.environ.get(namn, standard))) * 60.0
    except ValueError:
        return standard * 60.0


KOPIA = ".kopia"
# AC-4 allows 1 min between ledger and journal for the whole house: two
# speakers x (20 s + the 5 s tick) = 50 s lost at most on a kill -9.
BOKFOR_VAR_S = 20.0


class Budget:
    """Connected seconds per local day, on disk so a restart does not reset them."""

    def __init__(self, path=None, today=None, prov: bool = False):
        self.prov = prov  # a test stand-in's own ledger and cap (Henrik 2026-10-08)
        self.etikett = "prov" if prov else "moln"
        self.path = path or os.environ.get(
            "MOLN_LEDGER_PROV" if prov else "MOLN_LEDGER",
            "/data/moln_minuter_prov.json" if prov else "/data/moln_minuter.json")
        self._today = today or (lambda: datetime.date.today().isoformat())
        self._kand: float | None = None  # last good total; only valid on _kand_dag
        self._kand_dag: str | None = None

    def _kvar(self) -> dict:
        """Reuse the last good total only on the day it was read."""
        if self._kand is None or self._kand_dag != self._today():
            return {}
        return {self._today(): self._kand}

    @staticmethod
    def _las_fil(path: str) -> dict:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"not an object: {type(data).__name__}")
        return data

    def _read(self) -> dict:
        """Today's total: the higher of the ledger and its copy (US-032 AC-4).

        Each is replaced atomically, one after the other, so a torn, empty or
        missing ledger in a new process still has the copy's value.
        """
        day, varden, fel = self._today(), [], None
        for path in (self.path, self.path + KOPIA):
            try:
                varden.append(float(self._las_fil(path).get(day, 0.0)))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                fel = e
        if varden:
            if fel is not None:
                logger.error(f"❌ cloud budget file is unreadable, using the copy: {fel!r}")
            self._kand, self._kand_dag = max(varden), day
            return {day: self._kand}
        kvar = self._kvar()
        if kvar:
            logger.error(f"❌ cloud budget file is unreadable or missing, keeping last known value: {fel!r}")
            return kvar
        if fel is not None:
            logger.error(f"❌ cloud budget file is unreadable: {fel!r}")
        return {}

    def _skriv(self, data: dict) -> None:
        for path in (self.path, self.path + KOPIA):
            self._skriv_en(path, data)

    @staticmethod
    def _skriv_en(path: str, data: dict) -> None:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".moln-", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def anvant(self) -> float:
        return float(self._read().get(self._today(), 0.0))

    def tak_s(self) -> float:
        return max_sekunder_per_dag(self.prov)

    def lagg_till(self, sekunder: float) -> None:
        if sekunder <= 0:
            return
        data = self._read()
        day = self._today()
        total = float(data.get(day, 0.0)) + sekunder  # only today is kept
        try:
            self._skriv({day: total})
        except OSError as e:
            logger.warning(f"⚠️ cloud budget not saved: {e!r}")
            return
        self._kand = total
        self._kand_dag = day


BUDGET = Budget()
BUDGET_PROV = Budget(prov=True)
PROV_PREFIX = "attrapp"


def budget_for(device_id: str) -> "Budget":
    """The ledger a speaker's cloud minutes count against. The stand-in
    (tools/satellit_attrapp.py, device_id "attrapp...") has its own, so tests
    never eat the owner's cap. A real speaker cannot claim the name: comms
    stamps the device_id from the room, and the agent listens on loopback only."""
    return BUDGET_PROV if (device_id or "").startswith(PROV_PREFIX) else BUDGET


class SovlageMixin:
    """First in the MRO of every engine's service, so its _connect gates them all."""

    sover = False
    _vaknat_forut = False
    _uppkopplad_sedan = None
    _bokfort_till = None  # the open time up to here is already in the ledger
    budget = BUDGET
    _oppna: weakref.WeakSet = weakref.WeakSet()

    def oppen_tid(self) -> float:
        return 0.0 if self._uppkopplad_sedan is None else time.monotonic() - self._uppkopplad_sedan

    def _tagg(self) -> str:
        """'moln 1a2b3c' or 'prov 1a2b3c': tools/minutkoll.py pairs the journal lines by it."""
        return f"{self.budget.etikett} {id(self) & 0xFFFFFF:06x}"

    def _obokfort(self) -> float:
        if self._uppkopplad_sedan is None:
            return 0.0
        return time.monotonic() - max(self._uppkopplad_sedan, self._bokfort_till or 0.0)

    def bokfor(self) -> None:
        """Add this session's open seconds not yet in the ledger, once.

        Clearing the clock first makes a second call, from sova() after
        teardown or the other way around, add nothing.
        """
        sekunder, oppen = self._obokfort(), self.oppen_tid()
        if self._uppkopplad_sedan is not None:
            # Every close path passes here; tools/minutkoll.py pairs it with the connect.
            logger.info(f"🧾 cloud session closed after {oppen:.0f}s [{self._tagg()}]")
        self._uppkopplad_sedan = None
        self._bokfort_till = None
        self._oppna.discard(self)
        self.budget.lagg_till(sekunder)

    def bokfor_lopande(self, var_s: float = BOKFOR_VAR_S) -> None:
        """Book an open session every `var_s`, so a kill -9 loses at most
        `var_s` + one sleep-loop tick of it (US-032 AC-4). The session clock,
        and so the cap, is untouched."""
        sekunder = self._obokfort()
        if sekunder < var_s:
            return
        self._bokfort_till = time.monotonic()
        self.budget.lagg_till(sekunder)

    async def sov_begaran(self, reason: str) -> bool:
        """The engine asks to sleep (xAI closes an idle session): through the
        session's state machine, like every other sleep."""
        from app.session_state import SessionMaskin

        maskin = getattr(self, "_maskin", None) or SessionMaskin("engine")
        return await maskin.sov(self, reason)

    def over_maxtid(self) -> bool:
        return self.oppen_tid() >= max_sekunder_per_samtal()

    def over_budget(self) -> bool:
        oppet = sum(m._obokfort() for m in list(self._oppna) if m.budget is self.budget)
        return self.budget.anvant() + oppet >= self.budget.tak_s()

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
                f"{self.budget.tak_s() / 60:.0f} min) — not connecting"
            )
            return False
        self.sover = False
        self._uppkopplad_sedan = time.monotonic()
        self._bokfort_till = None
        self._oppna.add(self)
        t0 = time.monotonic()
        try:
            await self._ateranslut(self._vaknat_forut)
            uppe = await self._ar_uppkopplad()
        except Exception as e:
            logger.error(f"❌ could not connect to the cloud engine on wake: {e!r}")
            uppe = False
        if not uppe:
            # Live 2026-10-04 (US-018): the connect failed offline, the service
            # still counted as awake, and nothing reconnected it when the net
            # came back. Asleep again, so the next wake tries anew.
            # Not booked: the socket never came up. A handshake still running
            # is torn down first: left alone it came up later as a session no
            # cap could see (G's review of 0.27.6, fynd 1).
            try:
                await self._disconnect()
            except Exception as e:
                logger.warning(f"⚠️ tearing down the unfinished connect failed: {e!r}")
            self.sover = True
            self._uppkopplad_sedan = None
            self._oppna.discard(self)
            logger.warning("☁️ cloud engine did not connect on wake — asleep, the next wake retries")
            return False
        self._vaknat_forut = True
        logger.info(f"☁️ connected to the cloud engine on wake ({time.monotonic() - t0:.1f}s) [{self._tagg()}]")
        return True

    async def _ar_uppkopplad(self) -> bool:
        """Engine-specific: is there a live connection after _ateranslut? Default: assume so."""
        return True

    async def sova(self, reason: str) -> bool:
        """Disconnect; nothing reconnects until the next wake. True if it was awake."""
        if self.sover:
            return False
        self.sover = True  # first: the closing socket's errors are then ignored
        self.bokfor()
        try:
            await self._disconnect()
        except Exception as e:
            logger.warning(f"⚠️ disconnect on sleep failed: {e!r}")
        logger.info(f"💤 disconnected from the cloud engine ({reason})")
        return True

    async def _ateranslut(self, forut: bool) -> None:
        """Engine-specific connect; `forut` = it was connected before (keep the conversation)."""
        await self._connect()
