"""Which engine runs, which one is broken, and when do we try the good one again.

Deliberately free of network, pipecat and Home Assistant, so the awkward parts
-- the cooldown, the second chance, both engines down at once -- can be tested
in milliseconds instead of by emptying an account.
"""
import logging
import time
from typing import Callable, Dict, Optional, Sequence

from app.provider_failures import Failure, classify

logger = logging.getLogger(__name__)

# A transient error gets this many tries on the same engine before the switch.
# One: enough to ride out a dropped socket, few enough that a real outage does
# not cost the user several dead turns.
TRANSIENT_RETRIES = 1


class ProviderRouter:
    """Decides which engine the next session is built with."""

    def __init__(
        self,
        primary: str,
        backup: Optional[str] = None,
        cooldown_s: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
        probe: Optional[Callable[[str], bool]] = None,
        extra: Sequence[str] = (),
    ) -> None:
        """
        Args:
            primary: The engine to use when nothing is wrong.
            backup: The engine to switch to, or None for no failover at all.
            cooldown_s: How long the backup runs before the primary is tried
                again.
            clock: Monotonic time source. Injected so tests need not sleep.
            probe: Cheap "does this engine answer at all?" check, asked only
                when a switch to the backup is on the table and the backup has
                not proved itself within the cooldown. None means no probe, so
                an unproven backup is never switched to.
            extra: Further engines after the backup, in order (0.26.2,
                VOICE_PROVIDERS=gemini,xai,openai). A failing engine hands
                over to the first one after it that is known healthy.
        """
        self.primary = primary
        self.backup = backup or None
        # The order of preference, without repeats or gaps.
        self.chain = list(dict.fromkeys(e for e in (primary, backup, *extra) if e))
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._active = primary
        self._reason = ""
        self._switched_at = 0.0
        # Failures counted against the engine currently running, cleared by a
        # good turn. Keyed by engine so a late frame from the one we left
        # cannot spend the new engine's budget.
        self._strikes: Dict[str, int] = {}
        # When each engine last completed a turn: the proof that it works.
        self._healthy_at: Dict[str, float] = {}
        self._probe = probe

    def current(self) -> str:
        """The engine the next session should be built with."""
        if (
            self._active != self.primary
            and self._switched_at
            and self._clock() - self._switched_at >= self.cooldown_s
        ):
            logger.info(
                f"⏳ cooldown over ({self.cooldown_s:.0f}s) — trying {self.primary} again"
            )
            self._active = self.primary
            self._reason = "cooldown over, retrying primary"
            self._switched_at = 0.0
            self._strikes.pop(self.primary, None)
        return self._active

    def report_failure(self, provider: str, message: str) -> str:
        """Record one failure and say which engine is in charge afterwards.

        Args:
            provider: The engine the failure came from.
            message: The error text.

        Returns:
            The engine to run from now on. Equal to ``provider`` means no
            switch: either the failure was ours, or this engine has a retry
            left, or there is nowhere else to go.
        """
        active = self.current()
        if provider != active:
            # A late error frame from the session we already left. Answering it
            # would bounce us straight back to the engine we just abandoned.
            logger.debug(f"ignoring stale failure from {provider} (running {active})")
            return active

        failure = classify(message)
        if failure in (Failure.APP, Failure.RATE_LIMIT):
            # A rate limit clears in seconds on the same engine; the caller
            # waits and asks again. Switching would cost half an hour.
            return active

        if failure is Failure.TRANSIENT:
            strikes = self._strikes.get(provider, 0) + 1
            self._strikes[provider] = strikes
            if strikes <= TRANSIENT_RETRIES:
                logger.info(
                    f"🔁 {provider} hiccup ({strikes}/{TRANSIENT_RETRIES + 1}): {message[:80]}"
                )
                return active

        return self._switch(failure, message)

    def note_success(self, provider: str) -> None:
        """A turn completed on this engine, so forget its earlier hiccups."""
        self._strikes.pop(provider, None)
        self._healthy_at[provider] = self._clock()

    def _known_healthy(self, provider: str) -> bool:
        """Proved itself within the cooldown, or passes the probe now."""
        seen = self._healthy_at.get(provider)
        if seen is not None and self._clock() - seen < self.cooldown_s:
            return True
        if self._probe is None:
            return False
        try:
            return bool(self._probe(provider))
        except Exception as e:
            logger.warning(f"⚠️ probe of {provider} raised: {e!r}")
            return False

    def status(self) -> Dict[str, object]:
        """What a dashboard needs to show which engine is live and why."""
        active = self.current()
        remaining = 0.0
        if active != self.primary and self._switched_at:
            remaining = max(0.0, self.cooldown_s - (self._clock() - self._switched_at))
        return {
            "provider": active,
            "primary": self.primary,
            "backup": self.backup,
            "chain": list(self.chain),
            "reason": self._reason,
            "switched_at": self._switched_at,
            "retry_primary_in_s": round(remaining, 1),
        }

    def _switch(self, failure: Failure, message: str) -> str:
        # Down the chain from the engine that failed: the first one known
        # healthy takes over. 2026-10-02: the house was moved to a backup that
        # could not hear and sat deaf for 30 min, so an unproven engine is
        # skipped. Nothing healthy after it: back to the primary, which needs
        # no proof (a primary that hiccups beats an engine nobody has seen work).
        later = self.chain[self.chain.index(self._active) + 1:] if self._active in self.chain else []
        other = next((e for e in later if self._known_healthy(e)), None)
        if other is None and self._active != self.primary:
            other = self.primary
        if other is None:
            if later:
                logger.warning(
                    f"⚠️ {self._active} failed ({failure.value}) but none of {later} is known "
                    f"healthy — staying on {self._active}: {message[:120]}"
                )
                self._reason = f"{failure.value}, backup unhealthy: {message[:140]}"
            else:
                logger.warning(
                    f"⚠️ {self._active} failed ({failure.value}) and there is no backup: {message[:120]}"
                )
                self._reason = f"{failure.value}: {message[:160]}"
            return self._active

        logger.warning(
            f"🔀 switching {self._active} → {other} ({failure.value}): {message[:120]}"
        )
        self._active = other
        self._reason = f"{failure.value}: {message[:160]}"
        self._switched_at = self._clock() if other != self.primary else 0.0
        self._strikes.clear()
        return self._active
