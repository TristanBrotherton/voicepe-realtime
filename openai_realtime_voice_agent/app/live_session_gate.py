"""On-demand GPT-Live session lifetime, with one hard invariant.

**ZERO GPT-Live session seconds while the device is idle.**

GPT-Live bills per connected session second and idle time counts (see
:mod:`app.live_cost`). The original runtime opened its Live session from
pipecat's ``StartFrame`` — that is, when the device's WebSocket connected —
and kept it for the life of the connection, refreshing it before the 60-minute
cap. For an appliance that is plugged in all day, session lifetime equalled
uptime: two mostly idle devices can accumulate ~400 session-minutes a day,
~$20 of voice charges before a single backend call.

This module inverts that. The Live socket is opened by a *turn* and closed by
the absence of one:

    idle  ──wake / first mic frame──▶  OPENING ──session.started──▶  OPEN
      ▲                                   │                           │
      └── CLOSED ◀── close(reason) ◀───────┴───────────────────────────┘

Close is deterministic and happens on every one of:

* ``idle`` — nothing audible and no user speech for ``idle_s``, with no
  backend work in flight;
* ``follow-up quiet`` — a reply finished and the bounded follow-up window
  (``followup_s``) expired without the user speaking again;
* ``silent`` — ``max_silent_s`` with no *audible* output and no user audio,
  regardless of what the model claims to be doing. Inaudible output can never
  hold a session open (GPT-Live streams continuous digital silence between
  replies and there is no output-audio-done event, so "the model is speaking"
  is not a safe liveness signal to bill against);
* ``max lifetime`` — ``max_session_s`` since the session opened, absolute;
* ``server expiry`` — ``session.started.expires_at`` minus a margin;
* ``handshake timeout`` — ``session.started`` never arrived;
* ``device stop`` / ``follow-up cut-off`` / ``device disconnected``;
* ``spend cutoff`` — the :class:`~app.live_cost.LiveSpendLedger` cap;
* any error path, including a failed open.

Startup opens nothing. A device that connects and is never spoken to pays
nothing, which is the invariant the tests assert directly
(``tests/test_live_session_gate.py``).

Three further properties the brief asked for by name:

* **Single-flight ownership.** One ``asyncio.Lock`` plus a monotonically
  increasing ``generation`` per device. Concurrent wakes, a wake racing a
  reconnect, or a late supervisor tick from a previous session can never open
  two sessions or close the wrong one.
* **Bounded pre-roll.** The device starts streaming the post-wake utterance
  while the Live handshake is still in flight. The service buffers it locally
  (``PRESTART_AUDIO_MAX_S``) and replays it in order on ``session.started``,
  so no early device audio is lost. The gate never waits for the handshake
  before returning, so nothing upstream blocks on it.
* **No automatic reconnect while idle.** The gate owns reconnection: a dropped
  socket is only re-opened while a turn is genuinely in flight, and never from
  a background refresh loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Dict, Optional

from app.live_cost import LiveSpendLedger, SessionCharge, session_cost_usd

logger = logging.getLogger(__name__)

CLOSED = "closed"
OPENING = "opening"
OPEN = "open"


@dataclass(frozen=True)
class LiveSessionPolicy:
    """Every bound on a GPT-Live session's lifetime, in seconds.

    The defaults are deliberately tight. A household turn is a few seconds of
    speech plus a few seconds of reply; anything beyond that is either a long
    delegated lookup (bounded by ``max_silent_s``) or a bug (bounded by
    ``max_session_s``).
    """

    # Quiet after any activity, with no backend work in flight, before closing.
    idle_s: float = 10.0
    # Bounded follow-up reuse: how long a finished reply keeps the session so a
    # follow-up question does not pay a new handshake. Mirrors the device's own
    # follow-up mic window; the device's {"type":"flush"} closes it earlier.
    followup_s: float = 8.0
    # Optional billed grace after the device's follow-up mic closes. A fresh
    # wake inside this small window reuses the ready session and avoids a new
    # TLS + session.start handshake. Zero preserves immediate close.
    rewake_grace_s: float = 0.0
    # Longest stretch with nothing audible and no user speech. This is the
    # anti-silence backstop: it ignores response_active entirely, so neither a
    # wedged delegation nor a continuous run of digital silence can keep paying.
    max_silent_s: float = 45.0
    # Absolute maximum session lifetime, from open to close. $0.25 at the
    # published rate.
    max_session_s: float = 300.0
    # session.started must arrive within this long, or the socket is dropped.
    open_timeout_s: float = 10.0
    # Close this far ahead of the server's own session.expires_at.
    expiry_margin_s: float = 15.0
    # Supervisor granularity. Small enough that every bound above is honoured
    # to within a quarter second; cheap, because it only compares floats.
    tick_s: float = 0.25

    def sanity_checked(self) -> "LiveSessionPolicy":
        """Clamp an operator's numbers into a shape that preserves the invariant."""
        idle = max(1.0, float(self.idle_s))
        followup = max(0.0, float(self.followup_s))
        rewake = max(0.0, min(30.0, float(self.rewake_grace_s)))
        silent = max(idle, float(self.max_silent_s))
        longest = max(idle, followup, rewake, silent) + 5.0
        return replace(
            self,
            idle_s=idle,
            followup_s=followup,
            rewake_grace_s=rewake,
            max_silent_s=silent,
            max_session_s=max(longest, float(self.max_session_s)),
            open_timeout_s=max(1.0, float(self.open_timeout_s)),
            expiry_margin_s=max(0.0, float(self.expiry_margin_s)),
            tick_s=min(1.0, max(0.01, float(self.tick_s))),
        )


class LiveSessionGate:
    """Owns exactly one GPT-Live session at a time, for one device.

    The gate talks to the service through a small duck-typed surface so it can
    be tested without a socket: ``open_session()``, ``close_session(reason)``,
    ``session_ready``, ``session_id``, ``session_expires_at``,
    ``session_seconds`` and ``response_active``.
    """

    def __init__(
        self,
        service,
        *,
        device_id: str = "",
        policy: Optional[LiveSessionPolicy] = None,
        ledger: Optional[LiveSpendLedger] = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        on_refused: Optional[Callable[[str], Awaitable[None]]] = None,
        on_closed: Optional[Callable[[SessionCharge], None]] = None,
    ):
        self._service = service
        self.device_id = device_id
        self.policy = (policy or LiveSessionPolicy()).sanity_checked()
        self.ledger = ledger if ledger is not None else LiveSpendLedger()
        self._mono = monotonic
        self._wall = wall_clock
        # Told when a turn is refused for budget reasons, so the user hears why
        # instead of silence.
        self._on_refused = on_refused
        self._on_closed = on_closed

        self._lock = asyncio.Lock()
        self._intent_epoch = 0
        self._reservation = None
        self._reserved_seconds = 0.
        self._state = CLOSED
        self.generation = 0
        self._opened_mono: Optional[float] = None
        self._supervisor: Optional[asyncio.Task] = None
        self._closed_for_good = False
        # Deadlines, all monotonic. None while closed.
        self._idle_deadline: Optional[float] = None
        self._silent_deadline: Optional[float] = None
        # "idle" or "follow-up quiet" — which of the two the idle deadline
        # currently represents.
        self._idle_reason = "idle"
        # Set once a turn is genuinely in flight, cleared when it resolves.
        self._turn_in_flight = False
        # Telemetry for a spend watchdog.
        self.opens = 0
        self.open_failures = 0
        self.refusals = 0
        self.closes: Dict[str, int] = {}
        self.last_charge: Optional[SessionCharge] = None

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state in (OPENING, OPEN)

    @property
    def turn_in_flight(self) -> bool:
        """True while a user request is unresolved — the only time a dropped
        socket is worth re-opening. Never true while idle."""
        return self.is_open and self._turn_in_flight

    @property
    def open_seconds(self) -> float:
        """Voice seconds this session has accrued so far.

        Prefers the server's own cumulative ``session.usage.updated`` count and
        falls back to wall time since the socket opened, which is what the
        session is billed on. ``usage.seconds`` arrives only about once a
        minute, so the local clock is the larger number for short sessions and
        is what a cutoff must act on.
        """
        if self._opened_mono is None:
            return 0.0
        local = max(0.0, self._mono() - self._opened_mono)
        reported = getattr(self._service, "session_seconds", 0.0) or 0.0
        try:
            reported = float(reported)
        except (TypeError, ValueError):
            reported = 0.0
        return max(local, reported)

    def snapshot(self) -> Dict[str, object]:
        """Cost and lifecycle telemetry for logs, sensors and tests."""
        open_seconds = self.open_seconds
        return {
            "device_id": self.device_id,
            "state": self._state,
            "generation": self.generation,
            "opens": self.opens,
            "open_failures": self.open_failures,
            "refusals": self.refusals,
            "closes": dict(self.closes),
            "open_session_seconds": round(open_seconds, 3),
            "open_session_usd": round(session_cost_usd(open_seconds), 6),
            "last_session": self.last_charge.describe() if self.last_charge else "",
            "spend": self.ledger.snapshot(open_seconds=open_seconds),
        }

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------

    async def open(self, reason: str, *, turn: bool = True) -> bool:
        """Open the Live session for a genuine wake/turn, or extend the open one.

        Single-flight: concurrent callers serialize on the lock, and the second
        one finds the session already open and merely refreshes its deadlines.
        Returns True when a session is open (or opening) on return.
        """
        epoch = self._intent_epoch
        async with self._lock:
            return await self._open_locked(reason, turn, epoch)

    async def _open_locked(self, reason, turn, epoch):
        if self._closed_for_good or epoch != self._intent_epoch:
            return False
        if turn:
            self._turn_in_flight = True
        if self._state in (OPENING, OPEN):
            self._touch(self._mono())
            return True
        verdict = self.ledger.check()
        if not verdict.allowed:
            self.refusals += 1
            self._turn_in_flight = False
            logger.error(f"⛔ refusing a GPT-Live session: {verdict.reason}")
            await self._notify_refused(verdict.reason)
            return False
        self._reservation = self.ledger.reserve(
            self.policy.max_session_s + 15. + self.policy.tick_s,
            lambda: self.open_seconds,
            minimum_seconds=self.policy.open_timeout_s + 15. + self.policy.tick_s)
        if self._reservation is None:
            self._turn_in_flight = False
            self.refusals += 1
            await self._notify_refused("Live spend reservation refused")
            return False
        self._reserved_seconds = self.ledger.reservation_seconds(self._reservation)
        self.generation += 1
        generation = self.generation
        self._state = OPENING
        now = self._mono()
        self._opened_mono = now
        self._touch(now)
        try:
            await asyncio.wait_for(self._service.open_session(), self.policy.open_timeout_s)
        except asyncio.CancelledError:
            await self._close_locked("open cancelled", generation)
            raise
        except Exception as e:
            self.open_failures += 1
            logger.error(f"❌ GPT-Live session failed to open ({reason}): {e!r}")
            # Fall through to a full close so no socket, task or accrued
            # second is left behind by a failed open.
            await self._close_locked("open failed", generation)
            return False
        if epoch != self._intent_epoch or self._closed_for_good:
            await self._close_locked("open invalidated", generation)
            return False
        self.opens += 1
        logger.info(
            f"🟢 GPT-Live session opening ({reason}) "
            f"[gen {generation}, max {self.policy.max_session_s:.0f}s, "
            f"${session_cost_usd(self._reserved_seconds):.3f} reserved including close margin]"
        )
        self._state = OPEN
        self._supervisor = asyncio.create_task(
            self._supervise(generation), name=f"live-gate-{self.device_id or 'device'}"
        )
        return True

    async def recover(self, reason):
        """Recovery shares the exact open/close owner and budget lifecycle.

        close() invalidates intent before waiting for this lock; a stop at any
        await prevents reopening (or immediately closes an in-flight open).
        """
        epoch = self._intent_epoch
        async with self._lock:
            if not self.turn_in_flight or self._closed_for_good:
                return False
            await self._close_locked("recovery: " + reason, self.generation)
            if epoch != self._intent_epoch or self._closed_for_good:
                return False
            return await self._open_locked("recovery", True, epoch)

    async def _notify_refused(self, reason: str) -> None:
        if self._on_refused is None:
            return
        try:
            await self._on_refused(reason)
        except Exception as e:
            logger.debug(f"budget-refusal observer failed: {e!r}")

    # ------------------------------------------------------------------
    # activity
    # ------------------------------------------------------------------

    def _touch(self, now: float) -> None:
        self._idle_deadline = now + self.policy.idle_s
        self._silent_deadline = now + self.policy.max_silent_s
        # Fresh activity: the next idle close is an ordinary idle, not the end
        # of a follow-up window. The distinction is only for the close reason —
        # which is what the telemetry and the canary runbook are read against.
        self._idle_reason = "idle"

    def note_user_audio(self) -> None:
        """Mic transport activity is not evidence of a new utterance."""
        if self._state == CLOSED or self._idle_reason == "follow-up quiet":
            return
        self._touch(self._mono())

    def note_user_utterance(self) -> None:
        """A fresh nonempty user transcript starts a real follow-up turn."""
        if self._state != CLOSED:
            self._turn_in_flight = True
            self._touch(self._mono())

    def note_audible_output(self) -> None:
        """Audible reply audio reached the device. Silence deliberately does not."""
        if self._state == CLOSED or self._idle_reason == "follow-up quiet":
            return
        self._touch(self._mono())

    def note_backend_activity(self) -> None:
        """A delegation or tool made progress: extend the idle deadline only.

        The silent deadline is *not* extended, so backend work that produces
        nothing audible still ends the session at ``max_silent_s``.
        """
        if self._state == CLOSED:
            return
        # A short acknowledgement can finish before a slow tool does. If the
        # backend then makes progress, the reply was not terminal and the
        # follow-up window must no longer own the session.
        if self._idle_reason == "follow-up quiet":
            if not getattr(self._service, "backend_work_active", False):
                return
            self._turn_in_flight = True
            self._idle_reason = "idle"
        self._idle_deadline = self._mono() + self.policy.idle_s

    def note_reply_finished(self) -> None:
        """A reply ended: hold the session for the bounded follow-up window."""
        if self._state == CLOSED:
            return
        now = self._mono()
        # The spoken reply can end while a tool or delegation is still running
        # (GPT-Live answers first and continues working). The request is only
        # resolved — and a dropped socket only not worth re-opening — once
        # nothing is in flight.
        if getattr(self._service, "backend_work_active", False):
            # This was an acknowledgement or an assistant segment whose
            # physical playout/tool continuation has not finished. Keep the
            # turn owned and let the ordinary idle/silent bounds supervise it.
            self._turn_in_flight = True
            self._idle_deadline = now + self.policy.idle_s
            self._idle_reason = "idle"
            return
        self._turn_in_flight = False
        self._idle_deadline = now + self.policy.followup_s
        self._silent_deadline = max(
            self._silent_deadline or 0.0, now + self.policy.followup_s
        )
        self._idle_reason = "follow-up quiet"

    def note_followup_closed(self) -> bool:
        """Optionally retain the ready session for one bounded re-wake window.

        The device has closed its microphone, so this never extends listening
        or changes turn-taking. It spends only the configured grace seconds;
        the default remains zero and closes immediately as before.
        """
        if self._state == CLOSED or self.policy.rewake_grace_s <= 0:
            return False
        now = self._mono()
        self._turn_in_flight = False
        self._idle_deadline = now + self.policy.rewake_grace_s
        self._silent_deadline = now + self.policy.rewake_grace_s
        self._idle_reason = "re-wake grace"
        return True

    def note_turn_resolved(self) -> None:
        """The request is answered or abandoned; a drop is no longer worth a retry."""
        self._turn_in_flight = False

    # ------------------------------------------------------------------
    # supervision
    # ------------------------------------------------------------------

    async def _supervise(self, generation: int) -> None:
        """Enforce every deadline. The only thing that bills is time, so this
        loop is the whole cost-safety mechanism."""
        policy = self.policy
        try:
            while True:
                await asyncio.sleep(policy.tick_s)
                if generation != self.generation or self._state != OPEN:
                    return
                reason = self._expired(generation)
                if reason is None:
                    continue
                await self.close(reason, generation=generation)
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"⚠️ GPT-Live session supervisor failed: {e!r}")
            await self.close("supervisor error", generation=generation)

    def _expired(self, generation: int) -> Optional[str]:
        """The close reason that applies right now, in priority order."""
        policy, now = self.policy, self._mono()
        opened = self._opened_mono
        if opened is not None and now - opened >= policy.max_session_s:
            return "max lifetime"
        if self._reservation and self.open_seconds >= max(0., self._reserved_seconds - 15. - policy.tick_s):
            return "spend cutoff"
        verdict = self.ledger.check(open_seconds=self.open_seconds)
        if not verdict.allowed:
            return "spend cutoff"
        expires_at = getattr(self._service, "session_expires_at", None)
        if isinstance(expires_at, (int, float)) and expires_at > 0:
            if self._wall() >= float(expires_at) - policy.expiry_margin_s:
                return "server expiry"
        if not getattr(self._service, "session_ready", False):
            if opened is not None and now - opened >= policy.open_timeout_s:
                return "handshake timeout"
            # Still handshaking: the idle/silent deadlines below are measured
            # from the open, so do not let them fire before the session exists.
            return None
        if (self._idle_reason == "re-wake grace"
                and self._idle_deadline is not None and now >= self._idle_deadline):
            return self._idle_reason
        if self._silent_deadline is not None and now >= self._silent_deadline:
            if getattr(self._service, "router_pending", False):
                # A routed request is still being worked on (a slow agent
                # question). Never close under it; max lifetime still bounds
                # the session, and the voice narrates progress meanwhile.
                self._silent_deadline = now + policy.idle_s
                return None
            return "silent"
        if self._idle_deadline is not None and now >= self._idle_deadline:
            backend_work = getattr(self._service, "backend_work_active", False)
            response_active = getattr(self._service, "response_active", False)
            if self._idle_reason != "follow-up quiet" and response_active:
                # Real work in flight. Extend the idle deadline only; the
                # silent deadline above stays where it is and still ends this.
                self._idle_deadline = now + policy.idle_s
                return None
            if self._idle_reason == "follow-up quiet" and backend_work:
                self._turn_in_flight = True
                self._idle_reason = "idle"
                self._idle_deadline = now + policy.idle_s
                return None
            return self._idle_reason
        return None

    # ------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------

    async def close(self, reason: str, *, generation: Optional[int] = None) -> bool:
        """Close the session this gate owns. Idempotent and generation-fenced."""
        if generation is not None and generation != self.generation:
            # A late tick or a handler from a previous session. Closing here
            # would tear down a session the user is talking to right now.
            return False
        self._intent_epoch += 1
        async with self._lock:
            if generation is not None and generation != self.generation:
                return False
            return await self._close_locked(reason, self.generation)

    async def _close_locked(self, reason: str, generation: int) -> bool:
        if self._state == CLOSED:
            return False
        self._state = CLOSED
        self._turn_in_flight = False
        self._idle_deadline = None
        self._silent_deadline = None
        seconds = self.open_seconds
        session_id = str(getattr(self._service, "session_id", "") or "")
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and supervisor is not asyncio.current_task():
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"gate supervisor shutdown: {e!r}")
        cancelled = False
        try:
            await asyncio.wait_for(self._service.close_session(reason), 15.)
        except asyncio.CancelledError:
            cancelled = True
            seconds = max(seconds, self._reserved_seconds)
            self.ledger.persistence_error = "Live close cancelled; further sessions refused"
        except Exception as e:
            seconds = max(seconds, self._reserved_seconds)
            self.ledger.persistence_error = "Live close unconfirmed; further sessions refused"
            logger.warning(f"⚠️ GPT-Live session close ({reason}) failed: {e!r}")
        finally:
            seconds = max(seconds, self.open_seconds)
            self._opened_mono = None
        self.closes[reason] = self.closes.get(reason, 0) + 1
        charge = SessionCharge(
            device_id=self.device_id, session_id=session_id, seconds=seconds,
            reason=reason, closed_at=self._wall(),
        )
        self.last_charge = charge
        self.ledger.settle(self._reservation, charge)
        self._reservation = None
        logger.info(
            f"🔴 GPT-Live session closed ({reason}) [gen {generation}] — "
            f"{seconds:.1f}s = ${charge.usd:.4f}; "
            f"{self.ledger.window_seconds / 60.0:.1f} session-min "
            f"(${self.ledger.window_usd:.2f}) in the last "
            f"{self.ledger.window_s / 3600.0:.0f}h"
        )
        if self._on_closed is not None:
            try:
                self._on_closed(charge)
            except Exception as e:
                logger.debug(f"session-close observer failed: {e!r}")
        if cancelled:
            raise asyncio.CancelledError
        return True

    async def shutdown(self, reason: str = "shutdown") -> None:
        """Close for good: no later wake can re-open this gate."""
        self._closed_for_good = True
        await self.close(reason)
