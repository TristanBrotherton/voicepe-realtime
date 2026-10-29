"""One speaker owner per device connection.

Everything the device plays (model speech, canned clips, timer and
announcement lines, voice-training lines) is an *utterance*: a contiguous
piece of 24 kHz mono PCM16 with a kind, a turn and an outcome. The owner is
the only writer of binary frames to the device. It decides the order, sends
each utterance whole and back to back, and records what became of it.

The one invariant: once an utterance has started, it plays to its end. Only a
user interrupt (stop, wake, button, mute) or a lost link stops it; no add-on
API cuts a started utterance.

Order (decided only when the head has been fully sent, in ``_next``):

1. a droppable (``clip``, ``ack``) past its start deadline is dropped;
2. a waiting ``announce`` goes next, at the boundary, never mid-utterance;
3. one acknowledgement per turn: an ``ack`` or ``clip`` is dropped when one
   already started in its turn, or when an answer (``reply``/``result``) of
   the same turn is already queued or started;
4. otherwise first in, first out.

Pacing: model speech goes out the moment it is fed (no hold, no 1.0x
pacer), so the device cushion grows over a reply instead of shrinking at
every source stall. All audio is capped at ``cap_ms`` ahead of the estimated
playout, which bounds how long a control message can queue behind audio.
The device's own start (a cold prime) is its only start latency.

Framing (``docs/device-protocol.md``):

- ``utt1``: each utterance is ``utt_start`` + binary + ``utt_end``; the device
  reports ``utt_ack`` (playing, progress, done, cut, dropped) with the bytes
  its speaker chain accepted, and each interrupt is answered with
  ``interrupt_ack`` once the add-on has flushed. Outcomes are facts.
- ``legacy``: plain binary, as older firmware expects. Same queue and order;
  playing and done are estimated from the wall clock.

This module is pure: it knows nothing about pipecat, GPT-Live or the
WebSocket. It is given two async senders and a clock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
BYTES_PER_S = SAMPLE_RATE * 2
BYTES_PER_MS = BYTES_PER_S // 1000
# One binary frame: 100 ms. The device accepts up to this per frame.
FRAME_BYTES = 4800
# How far complete audio may run ahead of the estimated playout.
CAP_MS = 2000
# A clip that cannot start this soon after it was asked for is stale.
CLIP_START_DEADLINE_S = 0.5
# Bound on what one utterance may buffer here while another is the head.
UTT_MAX_BUFFER_BYTES = 30 * BYTES_PER_S
# The device's cold-start prime (firmware default) and its cold test.
DEFAULT_PRIME_MS = 200
COLD_GAP_S = 0.6
# Silence ahead of a clip on a cold legacy device, so the chain's start-up loss
# falls on silence (utt1 devices prime themselves).
LEGACY_LEAD_IN_MS = 200
# utt1: a `done` this overdue is assumed (and logged as an estimate).
ACK_OVERDUE_S = 3.0
# utt1 firmware tracks at most this many utterances at once.
DEVICE_MAX_UTTS = 16
# Utterances remembered by id, for late acks and redelivery.
HISTORY = 64

CLIP, ACK, REPLY, RESULT, PROMPT, ANNOUNCE, COACH = (
    "clip", "ack", "reply", "result", "prompt", "announce", "coach")
KINDS = (CLIP, ACK, REPLY, RESULT, PROMPT, ANNOUNCE, COACH)
DROPPABLE = frozenset({CLIP, ACK})
ANSWERS = frozenset({REPLY, RESULT})
# Model speech: drives the device phases (Bot frames).
SPEECH = frozenset({ACK, REPLY, RESULT, PROMPT})

LEGACY, UTT = "legacy", "utt1"

QUEUED, SENDING, SENT, PLAYING = "queued", "sending", "sent", "playing"
DONE, CUT, DROPPED, LOST = "done", "cut", "dropped", "lost"
TERMINAL = frozenset({DONE, CUT, DROPPED, LOST})

# Interrupt reasons. A user interrupt stops what plays; housekeeping ones
# (the no-speech watchdog, a residual reply at a new wake) still flush.
USER_REASONS = frozenset({"stop", "wake", "button", "mute"})
REASONS = USER_REASONS | {"no_speech", "residual"}

# Owner states.
IDLE, BUSY, INTERRUPTED, CLOSED = "idle", "busy", "interrupted", "closed"


class Utterance:
    """One piece of audio and what became of it."""

    def __init__(self, uid: int, kind: str, turn: str, now: float, streamed: bool,
                 deadline: Optional[float] = None) -> None:
        self.id = uid
        self.kind = kind
        self.turn = turn or ""
        self.streamed = streamed
        self.closed = not streamed
        self.deadline = deadline
        self.state = QUEUED
        self.reason: Optional[str] = None        # cut/dropped/lost reason
        self.total = 0                           # bytes received from the producer
        self.sent = 0                            # bytes written to the device
        self.fed: Optional[int] = None           # bytes the device's chain accepted
        self.estimated = False                   # outcome is an estimate, not a fact
        self.truncated = False                   # source died before it closed
        self._keep: Optional[bytearray] = None   # its PCM, kept for redelivery
        self._pending = bytearray()
        self.submitted = now
        self.head_mono: Optional[float] = None
        self.first_sent_mono: Optional[float] = None
        self.last_sent_mono: Optional[float] = None
        self.playing_mono: Optional[float] = None
        self.end_mono: Optional[float] = None
        self.start_offset: Optional[int] = None  # stream offset of its first byte
        self.play_start_est: Optional[float] = None
        self.play_end_est: Optional[float] = None
        self.device_wait_ms: Optional[int] = None
        self.underruns: Optional[int] = None
        self._settled = asyncio.Event()
        self._finished = asyncio.Event()

    @property
    def keep(self) -> Optional[bytes]:
        """The kept audio (``keep=True``), or None."""
        return bytes(self._keep) if self._keep is not None else None

    @property
    def droppable(self) -> bool:
        return self.kind in DROPPABLE

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def started(self) -> bool:
        """Has any of it reached the device?"""
        return self.sent > 0

    @property
    def outcome(self) -> str:
        return self.state

    @property
    def fully_sent(self) -> bool:
        return self.closed and self.sent >= self.total and not self._pending

    async def wait_settled(self, timeout: Optional[float] = None) -> str:
        """Until it is fully sent or has an outcome; returns the state."""
        try:
            await asyncio.wait_for(self._settled.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.state

    async def wait_finished(self, timeout: Optional[float] = None) -> str:
        """Until it has an outcome (done, cut, dropped, lost)."""
        try:
            await asyncio.wait_for(self._finished.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.state

    def record(self) -> Dict[str, object]:
        """The `utt` log line: numbers and enums only, never audio or words."""
        def ms(a, b):
            return int(round((b - a) * 1000)) if a is not None and b is not None else None
        return {
            "id": self.id, "kind": self.kind, "turn": self.turn, "bytes": self.total,
            "outcome": self.state, "cut_reason": self.reason, "fed": self.fed,
            "est": self.estimated, "truncated_at_source": self.truncated,
            "queue_wait_ms": ms(self.submitted, self.head_mono),
            "sent_to_playing_ms": ms(self.first_sent_mono, self.playing_mono),
            "device_wait_ms": self.device_wait_ms, "underruns": self.underruns,
        }


Sender = Callable[..., Awaitable[object]]
Observer = Callable[[Utterance, str], None]


class SpeakerOwner:
    """The single writer of audio to one device.

    Args:
        send_text: async callable taking one JSON string (a control frame).
        send_bytes: async callable taking one binary frame.
        framing: ``LEGACY`` or ``UTT``; see ``set_framing``.
        clock: monotonic seconds; the event loop's clock in tests.
        prime_ms: the device's cold-start prime, for playout estimates.
        cap_ms: how far audio may run ahead of the estimated playout.
        turn: callable returning the current turn id (for utterances that
            do not name one).
        on_event: ``observer(utterance, event)`` for every lifecycle event:
            ``head``, ``sending``, ``playing``, ``sent`` and the outcome.
        on_idle: called when nothing is queued, sending or playing.
    """

    def __init__(self, send_text: Sender, send_bytes: Sender, *, framing: str = LEGACY,
                 device_id: str = "", clock: Callable[[], float] = time.monotonic,
                 prime_ms: int = DEFAULT_PRIME_MS, cap_ms: int = CAP_MS,
                 turn: Optional[Callable[[], str]] = None,
                 on_event: Optional[Observer] = None,
                 on_idle: Optional[Callable[[], None]] = None) -> None:
        self._send_text = send_text
        self._send_bytes = send_bytes
        self.framing = framing if framing in (LEGACY, UTT) else LEGACY
        self.device_id = device_id
        self._clock = clock
        self.prime_ms = max(0, int(prime_ms))
        self.cap_ms = max(100, int(cap_ms))
        self._turn = turn
        self.observers: List[Observer] = [on_event] if on_event is not None else []
        self.idle_observers: List[Callable[[], None]] = [on_idle] if on_idle is not None else []
        self._queue: List[Utterance] = []
        self._head: Optional[Utterance] = None
        self._inflight: List[Utterance] = []      # sent, outcome pending
        self._by_id: OrderedDict[int, Utterance] = OrderedDict()
        self._next_id = 1
        self._interrupt_seq: Optional[int] = None
        self._interrupt_reason: Optional[str] = None
        self._closed = False
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._sent_total = 0
        self._play_end = 0.0                       # when what was sent has played
        self._turn_acked: Dict[str, int] = {}      # turn -> ack/clip utterances started
        self._turn_answered: Dict[str, int] = {}   # turn -> answers started
        self._was_busy = False
        self.counters = {"cut": 0, "lost": 0, "dropped": 0, "done": 0}

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        if self._closed:
            return CLOSED
        if self._interrupt_seq is not None:
            return INTERRUPTED
        return BUSY if self.busy else IDLE

    @property
    def busy(self) -> bool:
        """Anything queued, being sent, or sent and not yet played out."""
        return bool(self._queue or self._head is not None
                    or any(not u.terminal for u in self._inflight))

    @property
    def must_finish_pending(self) -> bool:
        """A must-finish utterance is open, queued or not fully sent: a model
        session close would truncate it at its source."""
        items = list(self._queue) + ([self._head] if self._head is not None else [])
        return any(not u.droppable and not u.terminal and not u.fully_sent for u in items)

    @property
    def speaking(self) -> bool:
        """Model speech is queued, sending or playing (drives Bot frames)."""
        items = list(self._queue) + ([self._head] if self._head else []) + self._inflight
        return any(u.kind in SPEECH and not u.terminal for u in items)

    def utterance(self, uid: int) -> Optional[Utterance]:
        return self._by_id.get(uid)

    def history(self) -> List[Utterance]:
        return list(self._by_id.values())

    def set_framing(self, caps) -> str:
        """Choose framing from the device's ``start.caps``: utt1 when offered."""
        framing = UTT if isinstance(caps, (list, tuple)) and "utt1" in caps else LEGACY
        if framing != self.framing:
            logger.info(f"🔈 speaker owner for {self.device_id or 'device'}: {framing} framing")
        self.framing = framing
        return framing

    def _now(self) -> float:
        return self._clock()

    def _current_turn(self) -> str:
        if self._turn is None:
            return ""
        try:
            return str(self._turn() or "")
        except Exception:  # pragma: no cover - a turn lookup never breaks audio
            return ""

    # ------------------------------------------------------------------
    # producers
    # ------------------------------------------------------------------

    def open(self, kind: str, turn: Optional[str] = None, keep: bool = False) -> Utterance:
        """Start a streamed utterance (model speech). Feed it, then end it.
        ``keep``: hold a copy of its audio for redelivery after a lost link."""
        utt = self._add(kind, turn, streamed=True)
        if keep:
            utt._keep = bytearray()
        return utt

    def feed(self, utt: Utterance, pcm: bytes) -> bool:
        """Append PCM to an open utterance. False once it has an outcome."""
        if utt.terminal or utt.closed or not pcm:
            return False
        if len(pcm) % 2:
            pcm = pcm[:-1]
        utt._pending.extend(pcm)
        utt.total += len(pcm)
        if utt._keep is not None:
            utt._keep.extend(pcm)
        if utt is not self._head and len(utt._pending) > UTT_MAX_BUFFER_BYTES:
            # The link has stalled for 30 s of audio. Never cut inside the
            # utterance: it is lost as a whole and connection recovery acts.
            logger.error(f"❌ speaker owner: utterance {utt.id} ({utt.kind}) buffered more than "
                         f"{UTT_MAX_BUFFER_BYTES // BYTES_PER_S} s; marking it lost")
            self._finish(utt, LOST, "overflow")
        self._kick()
        return True

    def end(self, utt: Utterance, truncated: bool = False) -> None:
        """No more audio for this utterance (``truncated``: its source died)."""
        if utt.closed:
            return
        utt.closed = True
        utt.truncated = bool(truncated)
        if utt.total == 0 and not utt.terminal:
            if utt in self._queue:
                self._queue.remove(utt)
            if utt is self._head:
                self._head = None
            self._finish(utt, DROPPED, "empty")
        self._kick()

    def submit(self, kind: str, pcm: bytes, turn: Optional[str] = None,
               keep: bool = False) -> Utterance:
        """Queue a complete utterance (a clip, an announcement, a coach line)."""
        utt = self._add(kind, turn, streamed=True)
        if kind == CLIP:
            utt.deadline = utt.submitted + CLIP_START_DEADLINE_S
        if keep:
            utt._keep = bytearray()
        self.feed(utt, pcm or b"")
        utt.streamed = False
        self.end(utt)
        return utt

    def _add(self, kind: str, turn: Optional[str], streamed: bool) -> Utterance:
        if kind not in KINDS:
            raise ValueError(f"unknown utterance kind {kind!r}")
        utt = Utterance(self._next_id, kind, self._current_turn() if turn is None else turn,
                        self._now(), streamed)
        self._next_id += 1
        self._by_id[utt.id] = utt
        while len(self._by_id) > HISTORY:
            old_id, old = next(iter(self._by_id.items()))
            if not old.terminal:
                break
            self._by_id.pop(old_id)
        if self._closed:
            self._finish(utt, LOST, "link")
            return utt
        self._queue.append(utt)
        self._kick()
        return utt

    # ------------------------------------------------------------------
    # device events
    # ------------------------------------------------------------------

    def interrupt(self, seq: Optional[int] = None, reason: Optional[str] = None,
                  utt_id: Optional[int] = None, fed: Optional[int] = None) -> Dict[str, List[int]]:
        """The device flushed its speaker. Everything not finished ends here:
        what was playing is ``cut``, the rest ``dropped``. Nothing is sent
        until ``acknowledge_interrupt``. Returns the ids by outcome."""
        now = self._now()
        reason = reason if reason in REASONS else "interrupt"
        self._interrupt_seq = seq if isinstance(seq, int) else -1
        self._interrupt_reason = reason
        self._update_estimates(now)
        result: Dict[str, List[int]] = {CUT: [], DROPPED: []}
        items = self._inflight + ([self._head] if self._head is not None else []) + self._queue
        for utt in items:
            if utt.terminal:
                continue
            playing = False
            if self.framing == UTT:
                if utt_id is not None:
                    playing = utt.id == utt_id
                else:
                    playing = utt.state == PLAYING
            else:
                playing = (utt.started and utt.play_start_est is not None
                           and utt.play_start_est <= now)
            if playing:
                if self.framing == UTT and utt_id == utt.id and isinstance(fed, int):
                    utt.fed = max(0, fed)
                elif utt.fed is None:
                    utt.fed = self._fed_estimate(utt, now)
                    utt.estimated = self.framing != UTT
                if utt.fed is not None and utt.fed >= utt.total and utt.fully_sent:
                    self._finish(utt, DONE)
                else:
                    self._finish(utt, CUT, reason)
                    result[CUT].append(utt.id)
            else:
                utt.fed = 0 if utt.fed is None else utt.fed
                self._finish(utt, DROPPED, reason)
                result[DROPPED].append(utt.id)
        self._queue.clear()
        self._head = None
        self._inflight = []
        self._play_end = 0.0      # the device flushed: its chain is cold again
        logger.info(f"🛑 speaker owner flushed on interrupt ({reason}, seq {seq}): "
                    f"cut {result[CUT]}, dropped {result[DROPPED]}")
        self._kick()
        return result

    async def acknowledge_interrupt(self, seq: Optional[int] = None) -> None:
        """The add-on has flushed (and told the model): accept audio again.
        utt1 devices discard everything between their interrupt and this."""
        if self._interrupt_seq is None:
            return
        pending = self._interrupt_seq
        if seq is None:
            seq = pending
        if seq != pending:
            return  # a newer interrupt superseded it; that one is acked instead
        self._interrupt_seq = None
        self._interrupt_reason = None
        if self.framing == UTT and isinstance(seq, int) and seq >= 0 and not self._closed:
            await self._text({"type": "interrupt_ack", "seq": seq})
        self._kick()
        self._check_idle()

    def on_ack(self, msg: dict) -> None:
        """A device ``utt_ack``: facts about one utterance."""
        uid = msg.get("id")
        utt = self._by_id.get(uid) if isinstance(uid, int) else None
        if utt is None:
            return
        st = msg.get("st")
        fed = msg.get("fed")
        now = self._now()
        if isinstance(fed, int) and fed >= 0:
            utt.fed = fed
            if utt.start_offset is not None and not utt.terminal:
                # The chain accepted `fed` bytes of it: what is still queued on
                # the device plays from now.
                remaining = max(0, self._sent_total - (utt.start_offset + fed))
                self._play_end = now + remaining / BYTES_PER_S
        if st == "playing":
            if isinstance(msg.get("wait_ms"), int):
                utt.device_wait_ms = msg["wait_ms"]
            if utt.state in (SENDING, SENT):
                utt.state = PLAYING
                utt.playing_mono = now
                self._emit(utt, PLAYING)
        elif st in ("done", "cut", "dropped"):
            if isinstance(msg.get("und"), int):
                utt.underruns = msg["und"]
            outcome = {"done": DONE, "cut": CUT, "dropped": DROPPED}[st]
            if utt.terminal:
                if utt.state != outcome:
                    # The add-on decided first (at the interrupt); the device's
                    # account is the fact.
                    logger.info(f"🔈 utt {utt.id}: device reports {outcome}, add-on had {utt.state}")
                    self._count(utt.state, -1)
                    utt.state = outcome
                    self._count(outcome, 1)
                utt.estimated = False
                return
            if outcome == DONE and utt.playing_mono is None:
                utt.playing_mono = now
            self._finish(utt, outcome, self._interrupt_reason if outcome != DONE else None)
        self._kick()

    def link_lost(self) -> None:
        """The WebSocket is gone. Every unfinished utterance is ``lost`` (the
        device may still play what it holds; the next ``start`` says)."""
        if self._closed:
            return
        self._closed = True
        for utt in self._inflight + ([self._head] if self._head else []) + self._queue:
            if not utt.terminal:
                utt.closed = True
                self._finish(utt, LOST, "link")
        self._queue.clear()
        self._head = None
        self._inflight = []
        self._kick()

    async def shutdown(self) -> None:
        self.link_lost()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # the sender
    # ------------------------------------------------------------------

    def _kick(self) -> None:
        if self._wake is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            self._wake = asyncio.Event()
        self._wake.set()
        if self._task is None and not self._closed:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._closed:
                self._wake.clear()
                timeout = await self._step()
                if self._closed:
                    break
                if timeout == 0:
                    continue
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"❌ speaker owner stopped: {e!r}", exc_info=True)

    async def _step(self) -> Optional[float]:
        """Send what may go now. Returns how long to sleep (None: until kicked)."""
        now = self._now()
        due = self._update_estimates(now)
        if self._interrupt_seq is not None:
            return due
        if (self._head is None and self.framing == UTT
                and sum(1 for u in self._inflight if not u.terminal) >= DEVICE_MAX_UTTS - 1):
            # The device tracks a bounded number of utterances: the next one
            # waits until an earlier one has finished playing.
            return due
        if self._head is None:
            self._head = self._next(now)
            if self._head is not None:
                self._head.state = SENDING
                self._head.head_mono = now
                self._emit(self._head, "head")
        head = self._head
        if head is None:
            self._check_idle()
            return due
        while head._pending:
            lead = self._play_end - now
            if lead * 1000.0 >= self.cap_ms:
                wait = lead - self.cap_ms / 1000.0 + 0.005
                return wait if due is None else min(wait, due)
            if not head.started:
                await self._begin(head, now)
                if self._head is not head or self._interrupt_seq is not None:
                    return 0
            n = min(FRAME_BYTES, len(head._pending))
            chunk = bytes(head._pending[:n])
            del head._pending[:n]
            self._account(head, len(chunk), now)
            await self._binary(chunk)
            if self._head is not head or self._interrupt_seq is not None or self._closed:
                return 0
            now = self._now()
        if head.closed and not head._pending:
            if head.started and self.framing == UTT:
                await self._text({"type": "utt_end", "id": head.id, "bytes": head.total})
                if self._head is not head or self._interrupt_seq is not None:
                    return 0
            self._head = None
            if not head.terminal:
                head.state = SENT if head.state == SENDING else head.state
                head._settled.set()
                self._inflight.append(head)
                self._emit(head, SENT)
            return 0
        return due

    async def _begin(self, head: Utterance, now: float) -> None:
        """First byte of an utterance: its framing, or a legacy lead-in."""
        if head.kind in DROPPABLE:
            self._turn_acked[head.turn] = self._turn_acked.get(head.turn, 0) + 1
        if head.kind in ANSWERS:
            self._turn_answered[head.turn] = self._turn_answered.get(head.turn, 0) + 1
        if self.framing == UTT:
            await self._text({"type": "utt_start", "id": head.id, "kind": head.kind,
                              "turn": head.turn})
        elif head.kind == CLIP and self._cold(now):
            silence = b"\x00" * (BYTES_PER_MS * LEGACY_LEAD_IN_MS)
            self._account(None, len(silence), now)
            await self._binary(silence)

    def _cold(self, now: float) -> bool:
        return self._play_end <= 0.0 or now - self._play_end > COLD_GAP_S

    def _account(self, utt: Optional[Utterance], nbytes: int, now: float) -> None:
        """Advance the playout estimate for ``nbytes`` about to be written."""
        if self._cold(now):
            start = now + self.prime_ms / 1000.0
        else:
            start = max(now, self._play_end)
        if utt is not None and utt.start_offset is None:
            utt.start_offset = self._sent_total
            utt.play_start_est = start
            utt.first_sent_mono = now
            self._emit(utt, SENDING)
        self._play_end = start + nbytes / BYTES_PER_S
        self._sent_total += nbytes
        if utt is not None:
            utt.sent += nbytes
            utt.last_sent_mono = now
            utt.play_end_est = self._play_end

    def _fed_estimate(self, utt: Utterance, now: float) -> int:
        if utt.play_start_est is None or now <= utt.play_start_est:
            return 0
        played = int((now - utt.play_start_est) * BYTES_PER_S) & ~1
        return max(0, min(utt.sent, played))

    def _update_estimates(self, now: float) -> Optional[float]:
        """Legacy: estimate playing/done from the wall clock. utt1: assume an
        overdue `done`. Returns seconds until the next estimate is due."""
        due: Optional[float] = None
        for utt in list(self._inflight) + ([self._head] if self._head is not None else []):
            if utt.terminal or utt.play_start_est is None:
                continue
            if self.framing == LEGACY:
                if utt.state in (SENDING, SENT) and now >= utt.play_start_est:
                    utt.state = PLAYING
                    utt.playing_mono = utt.play_start_est
                    utt.estimated = True
                    self._emit(utt, PLAYING)
                elif utt.state in (SENDING, SENT):
                    due = self._min(due, utt.play_start_est - now)
                if utt.state in (PLAYING, SENT) and utt.fully_sent and utt is not self._head:
                    if now >= (utt.play_end_est or 0.0):
                        utt.fed = utt.total
                        utt.estimated = True
                        self._finish(utt, DONE)
                    else:
                        due = self._min(due, utt.play_end_est - now)
            elif utt.fully_sent and utt is not self._head:
                limit = (utt.play_end_est or now) + ACK_OVERDUE_S
                if now >= limit:
                    logger.warning(f"⚠️ speaker owner: no done from the device for utterance "
                                   f"{utt.id} ({utt.kind}); assuming it played")
                    utt.estimated = True
                    utt.fed = utt.total if utt.fed is None else utt.fed
                    self._finish(utt, DONE)
                else:
                    due = self._min(due, limit - now)
        self._inflight = [u for u in self._inflight if not u.terminal]
        return due if due is None else max(0.001, due)

    @staticmethod
    def _min(a: Optional[float], b: float) -> float:
        return b if a is None else min(a, b)

    def _next(self, now: float) -> Optional[Utterance]:
        """The only place where order is decided (see the module docstring)."""
        for utt in list(self._queue):
            if utt.droppable and utt.deadline is not None and now > utt.deadline:
                self._queue.remove(utt)
                self._finish(utt, DROPPED, "late")
        announce = next((u for u in self._queue if u.kind == ANNOUNCE), None)
        if announce is not None:
            self._queue.remove(announce)
            return announce
        while self._queue:
            utt = self._queue[0]
            if utt.droppable and utt.turn and (
                    self._turn_acked.get(utt.turn)
                    or self._turn_answered.get(utt.turn)
                    or any(v.turn == utt.turn and v.kind in ANSWERS for v in self._queue[1:])):
                self._queue.pop(0)
                self._finish(utt, DROPPED, "one_ack")
                continue
            return self._queue.pop(0)
        return None

    # ------------------------------------------------------------------
    # outcomes and observers
    # ------------------------------------------------------------------

    def _finish(self, utt: Utterance, outcome: str, reason: Optional[str] = None) -> None:
        if utt.terminal:
            return
        utt.state = outcome
        utt.reason = reason if outcome != DONE else None
        utt.end_mono = self._now()
        if outcome == DONE and utt.fed is None:
            utt.fed = utt.total
        if outcome in (DROPPED,) and utt.fed is None:
            utt.fed = 0
        utt._pending.clear()
        utt.closed = True
        utt._settled.set()
        utt._finished.set()
        self._count(outcome, 1)
        try:
            logger.info("🔈 utt " + json.dumps(dict(utt.record(), framing=self.framing),
                                               separators=(",", ":")))
        except Exception:  # pragma: no cover
            pass
        self._emit(utt, outcome)
        self._check_idle()

    def _count(self, outcome: str, n: int) -> None:
        if outcome in self.counters:
            self.counters[outcome] = max(0, self.counters[outcome] + n)

    def _emit(self, utt: Utterance, event: str) -> None:
        for observer in list(self.observers):
            try:
                observer(utt, event)
            except Exception as e:  # pragma: no cover - observers never break audio
                logger.debug(f"speaker owner observer failed: {e!r}")

    def _check_idle(self) -> None:
        busy = self.busy
        if self._was_busy and not busy:
            for observer in list(self.idle_observers):
                try:
                    observer()
                except Exception as e:  # pragma: no cover
                    logger.debug(f"speaker owner idle observer failed: {e!r}")
        self._was_busy = busy

    # ------------------------------------------------------------------
    # the wire
    # ------------------------------------------------------------------

    async def _text(self, obj: dict) -> None:
        if "value" in obj:  # the firmware's phase parser matches any "value"
            raise ValueError("speaker owner messages must not carry a 'value' key")
        try:
            await self._send_text(json.dumps(obj, separators=(",", ":")))
        except Exception as e:
            logger.warning(f"⚠️ speaker owner text send failed: {e!r}")

    async def _binary(self, data: bytes) -> None:
        try:
            await self._send_bytes(data)
        except Exception as e:
            logger.warning(f"⚠️ speaker owner audio send failed: {e!r}")
