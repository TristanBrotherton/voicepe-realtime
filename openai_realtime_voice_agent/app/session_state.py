"""One state machine per speaker session (raawr US-032 AC-9).

IDLE -> WAKE -> LISTENING -> THINKING -> SPEAKING -> CLOSING -> IDLE, with the
branches TABLE allows. This is the only code that sends a phase message to the
device and the only code that wakes or puts the cloud engine to sleep; every
change is one journal line with its reason, and an event the state does not
allow changes nothing and is journaled as rejected. tests/test_session_state.py
fails if another module calls the engine's vakna/sova or the device's
send_phase.

The phase values stay the firmware's: listening / thinking / replying / idle.
"""
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

IDLE, WAKE, LISTENING, THINKING, SPEAKING, CLOSING = (
    "IDLE", "WAKE", "LISTENING", "THINKING", "SPEAKING", "CLOSING")
STATES = (IDLE, WAKE, LISTENING, THINKING, SPEAKING, CLOSING)

# wake: the wake word. listening/thinking/replying/idle: the phase the turn
# machinery wants to show. close: the engine is put to sleep. closed: it is down.
EVENTS = ("wake", "listening", "thinking", "replying", "idle", "close", "closed")
_FASER = {"listening": LISTENING, "thinking": THINKING, "replying": SPEAKING, "idle": IDLE}

# (state, event) -> next state, None = not allowed there. Every pair is here.
# Wake and the phases are allowed almost everywhere: a wake word, a follow-up
# window or a late engine reply can come at any time, and refusing one would
# be a lost wake (0.27.6). What is refused is what cannot happen: a phase
# while the engine is torn down, "closed" when nothing is closing, and closing
# twice.
TABLE = {}
for _s in (IDLE, WAKE, LISTENING, THINKING, SPEAKING):
    TABLE[(_s, "wake")] = WAKE
    for _e, _n in _FASER.items():
        TABLE[(_s, _e)] = _n
    TABLE[(_s, "close")] = CLOSING
    TABLE[(_s, "closed")] = None
TABLE.update({
    (CLOSING, "wake"): WAKE,           # the wake word wins over a teardown
    (CLOSING, "listening"): None,
    (CLOSING, "thinking"): None,
    (CLOSING, "replying"): None,
    (CLOSING, "idle"): CLOSING,        # the device may go idle while the engine closes
    (CLOSING, "close"): None,
    (CLOSING, "closed"): IDLE,
})
assert set(TABLE) == {(s, e) for s in STATES for e in EVENTS}


class SessionMaskin:
    """The state of one speaker's session, and the only sender of its signals."""

    def __init__(self, device_id: str, send_phase: Optional[Callable[[str], Awaitable]] = None):
        self.device_id = device_id
        self._send_phase = send_phase
        self.state = IDLE

    def handle(self, event: str, reason: str = "") -> bool:
        """Apply an event. False (and nothing changed) when the state refuses it."""
        nasta = TABLE[(self.state, event)]
        if nasta is None:
            logger.info(f"🧭 {self.device_id}: rejected {event} in {self.state} ({reason})")
            return False
        if nasta != self.state:
            logger.info(f"🧭 {self.device_id}: {self.state} -> {nasta} ({event}: {reason})")
        self.state = nasta
        return True

    async def phase(self, value: str, reason: str = "") -> None:
        """Show a phase on the device, if the state allows it."""
        if not self.handle(value, reason or "phase"):
            return
        if self._send_phase is not None:
            await self._send_phase(value)

    async def vakna(self, tjanst) -> bool:
        """The wake word: connect the engine if it sleeps."""
        self.handle("wake", "device wake")
        fn = getattr(tjanst, "vakna", None)
        return bool(fn is not None and await fn())

    async def sov(self, tjanst, reason: str) -> bool:
        """Put the engine to sleep for `reason`. False when it was not awake."""
        if not self.handle("close", reason):
            return False
        try:
            return await tjanst.sova(reason)
        finally:
            self.handle("closed", reason)
