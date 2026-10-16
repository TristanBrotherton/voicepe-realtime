"""What a GPT-Live session costs, and the hard cutoff that bounds it.

GPT-Live does not bill per response. It bills **per connected session
second**: the published rate is $0.05 per session minute, billed per second,
and *idle time inside an open session counts*. Backend (delegated Responses)
tokens, hosted tools and local infrastructure are billed separately and are
NOT part of the number this module produces.

That is the whole reason the on-demand lifecycle in
:mod:`app.live_session_gate` exists. An always-connected appliance accumulates
session minutes while nobody is talking to it:

    two devices, mostly idle, one day: ~400 session-minutes -> ~$20
    of voice charges alone, before a single backend call.

A permanently open session is $3.00/hour, $72/day, ~$2,190/month per device.
So this module provides two things, both deliberately boring and testable:

* :func:`session_cost_usd` — seconds -> dollars, with no rounding up to whole
  minutes (OpenAI bills per second, so neither do we).
* :class:`LiveSpendLedger` — a rolling-window record of voice seconds with a
  **hard** cutoff. When either the session-minute cap or the dollar cap for
  the window is reached, the ledger refuses further sessions and the gate
  closes the one in flight. It is a spend watchdog, not an estimate: it fails
  closed, and the caps are operator-set with conservative defaults.

Everything here is pure except the optional JSON persistence, so a restart
cannot be used to escape the day's budget.
"""
from __future__ import annotations

import json
import logging
import os
import time
import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Published GPT-Live voice rate. Session time only: the delegated Responses
# backend, hosted tools and telephony are separate line items on the bill.
USD_PER_SESSION_MINUTE = 0.05
USD_PER_SESSION_SECOND = USD_PER_SESSION_MINUTE / 60.0
# Rolling accounting window for the cutoff. A day, so a cap reads as "dollars
# per day" the way an operator thinks about it.
DEFAULT_WINDOW_S = 24 * 60 * 60.0
# Conservative defaults. 60 session-minutes/day is $3.00/day — far more voice
# time than a household appliance uses on-demand (a turn is seconds), and far
# less than the ~407 minutes an always-connected session burned.
DEFAULT_MAX_SESSION_MINUTES_PER_DAY = 60.0
DEFAULT_MAX_USD_PER_DAY = 3.0
# Where the ledger survives a restart. /data is the add-on's persistent volume.
LEDGER_PATH = "/data/live_spend.json"


def session_cost_usd(seconds: float) -> float:
    """Voice cost of ``seconds`` of connected GPT-Live session time.

    Billed per second, so 61 s is $0.0508 and not two whole minutes.
    """
    return max(0.0, float(seconds)) * USD_PER_SESSION_SECOND


def projected_cost_usd(session_seconds: float, sessions: int = 0) -> Dict[str, float]:
    """A priced summary for logs, sensors and tests."""
    seconds = max(0.0, float(session_seconds))
    return {
        "sessions": int(sessions),
        "session_seconds": round(seconds, 3),
        "session_minutes": round(seconds / 60.0, 4),
        "usd": round(session_cost_usd(seconds), 6),
    }


@dataclass(frozen=True)
class SessionCharge:
    """One closed GPT-Live session, as the bill will see it."""

    device_id: str
    session_id: str
    seconds: float
    reason: str
    closed_at: float

    @property
    def usd(self) -> float:
        return session_cost_usd(self.seconds)

    def describe(self) -> str:
        return (
            f"{self.device_id or '?'}/{self.session_id or '?'} "
            f"{self.seconds:.1f}s ${self.usd:.4f} ({self.reason})"
        )


@dataclass(frozen=True)
class BudgetVerdict:
    """Whether another session second may be bought."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.allowed


@dataclass
class LiveSpendLedger:
    """Rolling-window GPT-Live voice spend with a hard cutoff.

    ``max_session_minutes_per_day`` and ``max_usd_per_day`` are both enforced;
    either one reaching its cap stops new sessions and closes the open one.
    Set a cap to 0 to disable *that* cap — never both, and the gate warns when
    an operator does, because the whole point is that an appliance cannot run
    up an unbounded voice bill while nobody is in the room.
    """

    max_session_minutes_per_day: float = DEFAULT_MAX_SESSION_MINUTES_PER_DAY
    max_usd_per_day: float = DEFAULT_MAX_USD_PER_DAY
    window_s: float = DEFAULT_WINDOW_S
    path: Optional[str] = LEDGER_PATH
    clock: Callable[[], float] = time.time
    # (closed_at, seconds) pairs inside the window, oldest first.
    _charges: Deque[Tuple[float, float]] = field(default_factory=deque, repr=False)
    _history: List[SessionCharge] = field(default_factory=list, repr=False)
    sessions_total: int = 0
    seconds_total: float = 0.0

    def __post_init__(self) -> None:
        self._active = {}
        self._readers = {}
        self.persistence_error = ""
        self._load()

    def reserve(self, maximum_seconds, reader, minimum_seconds=15.):
        """Persist worst-case ownership BEFORE buying a socket.

        Active reservations from an interrupted process are charged in full on
        restart. Separate add-on instances require separate caps; this ledger
        does not coordinate unrelated processes or claim household accounting.
        """
        if self.persistence_error:
            return None
        remaining = self.remaining_seconds()
        reserved = sum(v["reserved"] for v in self._active.values())
        seconds = float(maximum_seconds)
        if remaining is not None:
            seconds = min(seconds, max(0., remaining - reserved))
        if seconds <= minimum_seconds:
            return None
        owner = uuid.uuid4().hex
        self._active[owner] = {"reserved": seconds, "opened_at": self.clock()}
        self._readers[owner] = reader
        self._save()
        if self.persistence_error:
            self._active.pop(owner, None)
            self._readers.pop(owner, None)
            return None
        return owner

    def extend(self, owner, maximum_seconds):
        """Grow an open session's reservation toward ``maximum_seconds``, never
        past what the daily budget still allows. Returns the reserved seconds."""
        if owner not in self._active or self.persistence_error:
            return self._active.get(owner, {}).get("reserved", 0.)
        current = self._active[owner]["reserved"]
        remaining = self.remaining_seconds()
        want = float(maximum_seconds)
        if remaining is not None:
            others = sum(v["reserved"] for k, v in self._active.items() if k != owner)
            want = min(want, max(0., remaining - others))
        if want > current:
            self._active[owner]["reserved"] = want
            self._save()
        return self._active[owner]["reserved"]

    def reservation_seconds(self, owner):
        return self._active[owner]["reserved"]

    @property
    def active_seconds(self):
        return sum(max(0., float(self._readers[k]())) for k in self._active)

    def settle(self, owner, charge):
        self._active.pop(owner, None)
        self._readers.pop(owner, None)
        self.record(charge)
        self._save()

    # -- accounting ------------------------------------------------------

    def record(self, charge: SessionCharge) -> None:
        """Charge one closed session against the window."""
        if charge.seconds <= 0:
            # A session that never billed a second still counts as an open/close
            # cycle for the telemetry, but adds nothing to the window.
            self.sessions_total += 1
            self._history.append(charge)
            del self._history[:-50]
            return
        self._charges.append((charge.closed_at, float(charge.seconds)))
        self.sessions_total += 1
        self.seconds_total += float(charge.seconds)
        self._history.append(charge)
        del self._history[:-50]
        self._prune()
        self._save()

    def _prune(self) -> None:
        cutoff = self.clock() - self.window_s
        while self._charges and self._charges[0][0] < cutoff:
            self._charges.popleft()

    @property
    def window_seconds(self) -> float:
        """Billed voice seconds inside the rolling window."""
        self._prune()
        return sum(seconds for _at, seconds in self._charges)

    @property
    def window_usd(self) -> float:
        return session_cost_usd(self.window_seconds)

    # -- the cutoff ------------------------------------------------------

    def remaining_seconds(self) -> Optional[float]:
        """Voice seconds still inside budget, or ``None`` when uncapped."""
        limits = []
        if self.max_session_minutes_per_day > 0:
            limits.append(self.max_session_minutes_per_day * 60.0 - self.window_seconds)
        if self.max_usd_per_day > 0:
            limits.append(
                (self.max_usd_per_day - self.window_usd) / USD_PER_SESSION_SECOND
            )
        if not limits:
            return None
        return min(limits)

    def check(self, open_seconds: float = 0.0) -> BudgetVerdict:
        """May a session run (or keep running) for ``open_seconds`` more?

        ``open_seconds`` is the voice time the currently-open session has
        already accrued but not yet charged, so a single long session cannot
        outrun the cutoff by never closing.
        """
        if self.persistence_error:
            return BudgetVerdict(False, self.persistence_error)
        open_seconds = self.active_seconds if self._active else open_seconds
        remaining = self.remaining_seconds()
        if remaining is None:
            return BudgetVerdict(True)
        remaining -= max(0.0, float(open_seconds))
        if remaining > 0:
            return BudgetVerdict(True)
        spent = self.window_seconds + max(0.0, float(open_seconds))
        return BudgetVerdict(False, (
            f"GPT-Live spend cutoff reached: {spent / 60.0:.1f} session-minutes "
            f"(${session_cost_usd(spent):.2f}) in the last "
            f"{self.window_s / 3600.0:.0f}h, caps "
            f"{self.max_session_minutes_per_day:g} min / "
            f"${self.max_usd_per_day:g}"
        ))

    # -- telemetry -------------------------------------------------------

    def snapshot(self, open_seconds: float = 0.0, recent: int = 5) -> Dict[str, object]:
        """Everything a spend watchdog or sensor needs, priced."""
        open_seconds = self.active_seconds if self._active else open_seconds
        window = self.window_seconds + max(0.0, float(open_seconds))
        remaining = self.remaining_seconds()
        if remaining is not None:
            remaining = max(0.0, remaining - max(0.0, float(open_seconds)))
        return {
            "window_hours": round(self.window_s / 3600.0, 2),
            "sessions_in_window": len(self._charges),
            "sessions_total": self.sessions_total,
            "session_seconds": round(window, 3),
            "session_minutes": round(window / 60.0, 4),
            "usd": round(session_cost_usd(window), 6),
            "usd_per_session_minute": USD_PER_SESSION_MINUTE,
            "cap_session_minutes": self.max_session_minutes_per_day,
            "cap_usd": self.max_usd_per_day,
            "remaining_session_seconds": None if remaining is None else round(remaining, 1),
            "recent": [c.describe() for c in self._history[-recent:]],
            "note": "voice session time only; delegated backend tokens are billed separately",
        }

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            # First start is allowed only if the initial durable write works.
            self._save()
            return
        except (OSError, ValueError) as exc:
            self.persistence_error = f"Live spend persistence unavailable: {type(exc).__name__}"
            return
        if not isinstance(data, dict):
            self.persistence_error = "Live spend ledger is invalid"
            return
        try:
            for name in ("seconds_total", "sessions_total"):
                value = float(data.get(name, 0))
                if not math.isfinite(value) or value < 0:
                    raise ValueError("invalid totals")
            for at, seconds in data.get("charges", []):
                if not all(math.isfinite(float(v)) for v in (at, seconds)) or float(seconds) < 0:
                    raise ValueError("invalid charge")
            active = data.get("active", {})
            for entry in active.values():
                seconds = float(entry["reserved"])
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError("invalid reservation")
                data.setdefault("charges", []).append([self.clock(), seconds])
                data["seconds_total"] = float(data.get("seconds_total", 0)) + seconds
        except (ValueError, TypeError, KeyError, AttributeError):
            self.persistence_error = "Live spend ledger is invalid"
            return
        cutoff = self.clock() - self.window_s
        for entry in data.get("charges") or []:
            try:
                at, seconds = float(entry[0]), float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if at >= cutoff and seconds > 0:
                self._charges.append((at, seconds))
        self._charges = deque(sorted(self._charges))
        try:
            self.sessions_total = int(data.get("sessions_total") or 0)
            self.seconds_total = float(data.get("seconds_total") or 0.0)
        except (TypeError, ValueError):
            pass
        self._save()
        if self._charges:
            logger.info(
                f"💵 GPT-Live spend ledger restored: {self.window_seconds / 60.0:.1f} "
                f"session-minutes (${self.window_usd:.2f}) still inside the "
                f"{self.window_s / 3600.0:.0f}h window"
            )

    def _save(self) -> None:
        """Persist so a restart cannot reset the day's budget."""
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        if not os.path.isdir(directory):
            self.persistence_error = "Live spend persistence directory unavailable"
            return
        snapshot = {
            "active": self._active,
            "charges": [[at, seconds] for at, seconds in self._charges],
            "sessions_total": self.sessions_total,
            "seconds_total": round(self.seconds_total, 3),
        }
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            self.persistence_error = f"Live spend persistence failed: {type(e).__name__}"
            logger.error(self.persistence_error)
