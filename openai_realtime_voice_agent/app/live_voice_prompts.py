"""Spoken prompts rendered in the assistant's own GPT-Live voice.

The runtime speaks a few things outside the conversation: a progress filler
while a slow tool runs, an audible error, the spend-cutoff refusal, a timer
expiry, an announcement. Those used to be synthesized by the speech endpoint
in whichever of its voices was closest, so a household on a Live-only voice
heard two voices in one turn: the filler in one, the reply in another. The
decision (2026-10-11) is that every spoken sound from the device uses the
assistant's voice.

The speech endpoint does not offer the Live-only voices (it answers 400 for
``vesper``), so the only source of that voice is a Live session. This module
opens one in client delegation and hands it each line as a
``session.commentary.append`` (text the voice is to say aloud), and keeps the
audio the session produced for the verbatim line. Until 0.18.25 each line went
in as a user message for a delegated backend to answer, and the frontend
voiced something other than the line most of the time (0-4 of 20 lines per
start); measured on 2026-10-12, commentary is voiced verbatim for most short
lines ("On it.", "Sure, one moment.") about 0.7 s after it is sent:

* the frontend model adds its own interjection before the backend's answer
  ("Hmm.", "Okay.", a lip smack), exactly the acknowledgement the reply
  admission discards in production. The output transcript carries session
  timestamps and the output PCM is a continuous timeline, so the line is cut
  from the transcript deltas whose text equals the requested line — the same
  alignment the admission relies on — and the interjection is discarded;
* a line whose transcript never equals the request, after a bounded number
  of attempts, is not used at all: silence or a named fallback is preferable
  to the voice saying something nobody wrote;
* the session is reserved and settled on the spend ledger like a device
  session, under its own device id, bounded in length, and logged by name.
  It is opened once per voice for the fixed prompt set (cached on the
  add-on's persistent volume) and on demand for a dynamic line;
* each session owns its whole lifecycle (opening, open, expiring, closed).
  Every way a session ends (a failure or a cancel while it opens or renders,
  the endpoint dropping the socket at any point, its bound, the end of its
  linger, a shutdown) goes through one close that closes the socket, stops
  the session's tasks and settles its reservation exactly once for the
  seconds the socket was really open. The bound is the reservation the
  ledger granted, enforced by the session's own timer; a session that has
  begun closing, or is too near its bound for another line, is never reused.

Nothing here touches the conversation path: a render session has no tools,
no device audio, and no history beyond two fixed examples of the task.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from app.live_audio import pcm16_stats
from app.live_cost import SessionCharge
from app.live_protocol import (
    LIVE_SAMPLE_RATE,
    LIVE_URL,
    build_session_start,
    context_append,
    inner_type,
    input_audio_append,
    new_event_id,
    parse_server_event,
    session_close,
)

logger = logging.getLogger(__name__)

RENDER_INSTRUCTIONS = (
    "You are a voice line renderer for a smart speaker. Each line you are given "
    "to say aloud is one line of text. Speak that line exactly, word for word, "
    "with natural warmth, and say nothing else: no greeting, no comment, no question."
)
RENDER_BACKEND_INSTRUCTIONS = (
    "You render spoken lines. Reply with the user's line exactly, word for "
    "word, and nothing else."
)
# Two worked examples seed the session: with them the measured verbatim rate
# on the real endpoint went from about half to every line within two attempts.
RENDER_EXAMPLES = (("Good morning.", "Good morning."),
                   ("Starting the vacuum.", "Starting the vacuum."))

# Bounds. A render session is short and is always closed; these keep a
# misbehaving endpoint from turning the warm-up into an open-ended bill.
MAX_ATTEMPTS = 3
LINE_TIMEOUT_S = 15.0
OPEN_TIMEOUT_S = 10.0
QUIET_AFTER_LINE_S = 1.0
# ``response.completed`` is the delegated backend finishing its text; the
# frontend voices that text only afterwards (in the retained canary turns up
# to about 4 s later). An attempt whose transcript is not yet the line waits this
# long after the last sign of life (completion, audible audio, a transcript
# delta) before it is judged, instead of ending on the interjection's quiet.
ANSWER_SETTLE_S = 6.0
# A render that cannot get this many lines in a row verbatim stops: the rest
# stay unrendered rather than spending the whole session bound on a voice
# that is not following the task.
STOP_AFTER_UNVERIFIED_LINES = 3
MAX_SESSION_S = 150.0
RESERVE_MARGIN_S = 15.0
# Keep the session for this long after the last line so a sequence of dynamic
# lines (an enrollment walk-through, a timer and its follow-up) shares one.
LINGER_S = 15.0

# Audio cut around the verbatim transcript span. Transcript deltas land on
# 200 ms blocks of the session clock and lead the PCM slightly, so the cut
# starts just ahead of the first block and runs on from the last block until
# the output has been inaudible for TAIL_QUIET_MS (bounded by TAIL_MAX_MS).
AUDIBLE_PEAK = 200
LEAD_MS = 40
TAIL_QUIET_MS = 300
TAIL_MAX_MS = 1500
BLOCK_MS = 50
MIN_AUDIBLE_MS = 150
FADE_MS = 10
# The transcript's session clock and the received-audio timeline drift apart
# (the server does not stream every quiet stretch), so a cut placed by the
# transcript alone can start inside the first word: cached "Sure, one
# moment." held only "...ment", heard as "st" (2026-10-16). The start is
# walked back to the onset of the speech it lands in: back while audible,
# until ONSET_QUIET_MS of quiet, at most ONSET_MAX_MS.
ONSET_QUIET_MS = 100
ONSET_MAX_MS = 1500

SILENCE_CHUNK_MS = 100


class LiveVoiceUnavailable(Exception):
    """The Live voice cannot render right now. ``reason`` is a short name."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def normalize_line(text: str) -> str:
    """Fold case, punctuation and spacing, so "I'll" and "I’ll" agree."""
    folded = (text or "").replace("’", "'").lower()
    return " ".join("".join(ch for ch in folded if ch.isalnum() or ch.isspace()).split())


def _ms_to_bytes(ms: float) -> int:
    return (max(0, int(ms * LIVE_SAMPLE_RATE * 2 / 1000)) // 2) * 2


def _fade(pcm: bytes) -> bytes:
    """A short linear fade at both ends so a cut never clicks."""
    samples = bytearray(pcm)
    n = len(samples) // 2
    fade = min(n // 2, int(FADE_MS * LIVE_SAMPLE_RATE / 1000))
    if fade <= 0:
        return bytes(samples)
    for i in range(fade):
        for index in (i, n - 1 - i):
            value = int.from_bytes(samples[2 * index:2 * index + 2], "little", signed=True)
            value = int(value * (i + 1) / (fade + 1))
            samples[2 * index:2 * index + 2] = value.to_bytes(2, "little", signed=True)
    return bytes(samples)


def verbatim_suffix(transcripts: List[Tuple[str, object, object]], line: str) -> Optional[int]:
    """Index of the first delta of the suffix that is exactly ``line``, or None."""
    target = normalize_line(line)
    if not target:
        return None
    for index in range(len(transcripts)):
        if normalize_line("".join(t[0] for t in transcripts[index:])) == target:
            return index
    return None


def _first_audible_ms(timeline: bytes, start: int = 0) -> Optional[float]:
    block = _ms_to_bytes(BLOCK_MS)
    start = max(0, start - start % 2)
    for i in range(start, len(timeline), block):
        if pcm16_stats(timeline[i:i + block]).peak > AUDIBLE_PEAK:
            return i * 1000.0 / _ms_to_bytes(1000)
    return None


def _speech_onset(timeline: bytes, lo: int) -> int:
    """``lo`` moved back to where the speech it lands in begins (see ONSET_*)."""
    block = _ms_to_bytes(BLOCK_MS)
    lo = max(0, min(lo, len(timeline)))
    lo -= lo % 2
    floor = max(0, lo - _ms_to_bytes(ONSET_MAX_MS))
    quiet_ms, cut = 0.0, lo
    pos = lo
    while pos - block >= floor:
        stats = pcm16_stats(timeline[pos - block:pos])
        pos -= block
        if stats.peak > AUDIBLE_PEAK:
            quiet_ms = 0.0
            cut = pos
        else:
            quiet_ms += stats.duration_ms
            if quiet_ms >= ONSET_QUIET_MS:
                break
    return max(0, cut - _ms_to_bytes(LEAD_MS)) if cut < lo else lo


def cut_line(timeline: bytes, transcripts: List[Tuple[str, object, object]],
             line: str, attempt_start: int = 0) -> Optional[bytes]:
    """The PCM of ``line`` inside a session's output timeline, or None.

    ``transcripts`` are this attempt's ``session.output_transcript.delta``
    events as ``(text, start_ms, end_ms)``. The line is accepted only when a
    suffix of those deltas, joined, is exactly the requested text once
    normalized; everything before that suffix (the model's own interjection)
    is discarded with its audio.
    """
    index = verbatim_suffix(transcripts, line)
    if index is None:
        return None
    starts = [t[1] for t in transcripts[index:] if isinstance(t[1], (int, float))]
    ends = [t[2] for t in transcripts[index:] if isinstance(t[2], (int, float))]
    if not (starts and ends):
        return None
    span = (float(min(starts)), float(max(ends)))
    # Calibrate the transcript clock to the received audio: the attempt's first
    # transcript word is the first audible audio of THIS attempt (from
    # ``attempt_start``, its byte offset in the session timeline).
    first = next((t[1] for t in transcripts if isinstance(t[1], (int, float))), None)
    onset = _first_audible_ms(timeline, attempt_start)
    offset = (onset - float(first)) if (first is not None and onset is not None) else 0.0
    start_ms, end_ms = span[0] + offset, span[1] + offset
    lo = _ms_to_bytes(max(0.0, start_ms - LEAD_MS))
    lo = _speech_onset(timeline, lo)
    hi = _ms_to_bytes(max(0.0, end_ms))
    quiet_ms = 0.0
    block = _ms_to_bytes(BLOCK_MS)
    while hi < len(timeline) and hi - _ms_to_bytes(end_ms) < _ms_to_bytes(TAIL_MAX_MS):
        stats = pcm16_stats(timeline[hi:hi + block])
        hi += block
        quiet_ms = 0.0 if stats.peak > AUDIBLE_PEAK else quiet_ms + stats.duration_ms
        if quiet_ms >= TAIL_QUIET_MS:
            break
    hi = min(hi, len(timeline))
    if hi <= lo:
        return None
    segment = timeline[lo:hi]
    audible_ms = sum(
        pcm16_stats(segment[i:i + block]).duration_ms
        for i in range(0, len(segment), block)
        if pcm16_stats(segment[i:i + block]).peak > AUDIBLE_PEAK
    )
    if audible_ms < MIN_AUDIBLE_MS:
        return None
    return _fade(bytes(segment))


@dataclass
class _Attempt:
    transcripts: List[Tuple[str, object, object]] = field(default_factory=list)
    completed: bool = False
    completed_mono: Optional[float] = None
    first_audible_mono: Optional[float] = None
    last_audible_mono: Optional[float] = None
    last_transcript_mono: Optional[float] = None

    def last_activity(self) -> Optional[float]:
        marks = [m for m in (self.completed_mono, self.last_audible_mono,
                             self.last_transcript_mono) if m is not None]
        return max(marks) if marks else None

    def timed_deltas(self) -> int:
        return sum(1 for t in self.transcripts
                   if isinstance(t[1], (int, float)) and isinstance(t[2], (int, float)))


# A render session only ever moves forward through these states.
OPENING = "opening"    # reservation held; connecting or waiting for session.started
OPEN = "open"          # started; rendering a line or lingering for the next one
EXPIRING = "expiring"  # its close has begun (bound, idle, lost, cancel, shutdown)
CLOSED = "closed"      # socket closed, its tasks done, reservation settled once

# How often a session's timer looks at its bound and its linger. The timer,
# not the render loop, is what enforces the bound: a render stuck on a send
# that never returns is still closed at the deadline.
TIMER_TICK_S = 0.25
CLOSE_STEP_TIMEOUT_S = 3.0

_ENDED = object()


class _RenderSession:
    """One Live socket and everything it owns.

    The session is the only thing that closes itself. Whoever asks first
    (the renderer on a failure or a cancel, the reader on EOF, the timer at
    the bound or the end of the linger, the shutdown) starts one close task;
    every later request awaits that same task. The close task cancels the
    reader, the silence feed and the timer, closes the socket, and settles
    the ledger reservation exactly once with the seconds the socket was
    really open, even if it is itself cancelled on the way. A session that
    has begun closing is never handed out again.
    """

    def __init__(self, renderer: "LiveVoiceRenderer"):
        self.renderer = renderer
        self.clock = renderer.clock
        self.state = OPENING
        self.close_reason = ""
        self.socket = None
        self.reservation = None
        self.bound_s = MAX_SESSION_S
        self.opened_mono = self.clock()
        self.lost_mono: Optional[float] = None
        self.ended_mono: Optional[float] = None
        self.session_id = ""
        self.events: asyncio.Queue = asyncio.Queue()
        self.timeline = bytearray()
        self.position_ms = 0.0
        self.reader: Optional[asyncio.Task] = None
        self.silence: Optional[asyncio.Task] = None
        self.timer: Optional[asyncio.Task] = None
        self.opener: Optional[asyncio.Task] = None
        self.idle_until: Optional[float] = None
        self.lines = 0
        self.settled = False
        self.done = asyncio.Event()
        self._closing: Optional[asyncio.Task] = None

    # -- what the session is worth --------------------------------------

    @property
    def open_seconds(self) -> float:
        """Seconds the socket has really been open: nothing before a socket
        was bought, frozen when it was lost or when its close finished."""
        if self.socket is None:
            return 0.0
        end = self.ended_mono if self.ended_mono is not None else self.lost_mono
        if end is None:
            end = self.clock()
        return max(0.0, end - self.opened_mono)

    @property
    def deadline_mono(self) -> float:
        return self.opened_mono + self.bound_s

    def remaining(self) -> float:
        return self.deadline_mono - self.clock()

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    @property
    def lost(self) -> bool:
        return self.lost_mono is not None

    def mark_lost(self) -> None:
        """The socket is dead: it bills nothing from now on, and the session
        stops being open at once (no further line is sent on it)."""
        if self.lost_mono is None and self.ended_mono is None:
            self.lost_mono = self.clock()
        self.begin_close("lost")

    def fail(self) -> None:
        """The endpoint reported an error or closed the session, but the
        socket is still up: the session stops being open at once and bills
        until its close has finished."""
        self.begin_close("lost")

    # -- opening ----------------------------------------------------------

    def reserve(self, ledger, min_bound_s: float = 0.0) -> None:
        verdict = ledger.check()
        if not verdict.allowed:
            raise LiveVoiceUnavailable("spend cutoff", verdict.reason)
        # The reader is this session's own seconds, never whichever session
        # happens to be current later.
        reservation = ledger.reserve(
            MAX_SESSION_S + RESERVE_MARGIN_S,
            lambda: self.open_seconds,
            minimum_seconds=OPEN_TIMEOUT_S + RESERVE_MARGIN_S,
        )
        if reservation is None:
            raise LiveVoiceUnavailable("spend reservation refused")
        self.reservation = reservation
        # The ledger may grant less than asked; the session's bound is what
        # was granted, less the margin that covers the close exchange.
        self.bound_s = min(MAX_SESSION_S,
                           ledger.reservation_seconds(reservation) - RESERVE_MARGIN_S)
        if self.bound_s < min_bound_s:
            # Not enough budget left for one line: no socket is bought, and
            # the close releases the reservation.
            raise LiveVoiceUnavailable(
                "spend reservation refused",
                f"{self.bound_s:.0f}s left is less than one line")

    async def open(self, connect, url: str, api_key: str, start: dict) -> None:
        async with asyncio.timeout(OPEN_TIMEOUT_S):
            socket = await connect(
                uri=url,
                additional_headers={"Authorization": f"Bearer {api_key}"},
                max_size=16 * 1024 * 1024,
            )
        # Billing and the bound run from the moment a socket exists.
        self.opened_mono = self.clock()
        self.socket = socket
        if self._closing is not None:
            raise LiveVoiceUnavailable("unreachable", "closed while opening")
        self.reader = asyncio.create_task(self._read(), name="live-render-read")
        await socket.send(json.dumps(start))
        async with asyncio.timeout(OPEN_TIMEOUT_S):
            self.session_id = await self._wait_started()
        if self.state != OPENING:
            raise LiveVoiceUnavailable("unreachable", "closed while opening")
        self.state = OPEN
        self.opener = None
        self.silence = asyncio.create_task(self._silence(), name="live-render-silence")
        self.timer = asyncio.create_task(self._timer(), name="live-render-timer")

    async def _wait_started(self) -> str:
        while True:
            evt = await self.events.get()
            if evt is None:
                raise RuntimeError("socket closed before session.started")
            if evt.get("type") == "session.started":
                return str((evt.get("session") or {}).get("id") or "")
            if evt.get("type") == "error":
                raise RuntimeError(json.dumps(evt.get("error") or evt))

    # -- the session's own tasks --------------------------------------------

    async def _read(self) -> None:
        try:
            async for message in self.socket:
                evt = parse_server_event(message)
                if evt is not None:
                    self.events.put_nowait(evt)
                    if self.state == OPEN and evt.get("type") in ("error", "session.closed"):
                        # Ends the session at once, also while it lingers;
                        # a render in progress still sees the event.
                        self.fail()
        except asyncio.CancelledError:
            raise  # our own close; the socket was not lost
        except Exception as e:
            self.events.put_nowait({"type": "error", "error": {"message": repr(e)}})
        # EOF or a transport error: the socket bills nothing from here on,
        # and the session closes now rather than at the end of a linger.
        self.events.put_nowait(None)
        self.mark_lost()

    async def _silence(self) -> None:
        chunk = base64.b64encode(
            b"\x00" * (LIVE_SAMPLE_RATE * 2 * SILENCE_CHUNK_MS // 1000)).decode("ascii")
        try:
            while self.state == OPEN:
                await self.socket.send(json.dumps(input_audio_append(chunk)))
                await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _timer(self) -> None:
        """Close the session at its bound, or at the end of its linger."""
        while self.state == OPEN:
            now = self.clock()
            if now >= self.deadline_mono:
                self.begin_close("bound")
                return
            if self.idle_until is not None and now >= self.idle_until:
                self.begin_close("idle")
                return
            wake = self.deadline_mono if self.idle_until is None else min(self.deadline_mono, self.idle_until)
            await asyncio.sleep(max(0.0, min(TIMER_TICK_S, wake - now)))

    # -- handing the session out --------------------------------------------

    def claim(self, headroom_s: float) -> bool:
        """Take the session for a render, if it can still render a line."""
        if not self.is_open or self.lost or self.remaining() < headroom_s:
            return False
        self.idle_until = None
        return True

    def linger(self, seconds: float) -> None:
        self.idle_until = self.clock() + seconds

    # -- closing -------------------------------------------------------------

    def begin_close(self, reason: str) -> asyncio.Task:
        """Start the one close of this session (the first reason wins)."""
        if self._closing is None:
            self.close_reason = reason
            if self.state != CLOSED:
                self.state = EXPIRING
            self.events.put_nowait(None)  # wake a render waiting for events
            self._closing = asyncio.get_running_loop().create_task(
                self._close_sequence(), name="live-render-close")
        return self._closing

    async def close(self, reason: str) -> None:
        """Close and wait for it. A cancel of the caller does not stop the
        close: it runs on to its settlement by itself."""
        await asyncio.shield(self.begin_close(reason))

    async def _close_sequence(self) -> None:
        try:
            current = asyncio.current_task()
            tasks = [t for t in (self.timer, self.silence, self.reader)
                     if t is not None and not t.done() and t is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=CLOSE_STEP_TIMEOUT_S)
            if self.socket is not None:
                if not self.lost:
                    try:
                        async with asyncio.timeout(CLOSE_STEP_TIMEOUT_S):
                            await self.socket.send(json.dumps(session_close()))
                    except Exception:
                        pass
                try:
                    async with asyncio.timeout(CLOSE_STEP_TIMEOUT_S):
                        await self.socket.close()
                except Exception:
                    pass
        finally:
            # A session closed on purpose bills to the end of the close
            # exchange; a lost one stopped when the socket died.
            if self.ended_mono is None:
                self.ended_mono = self.lost_mono if self.lost_mono is not None else self.clock()
            self.state = CLOSED
            seconds = self.open_seconds
            self._settle(seconds)
            self.renderer._forget(self)
            self.done.set()
            if self.socket is not None:
                logger.info(
                    "🎙️ Live voice render session closed (%s): %d line(s) in %.1fs",
                    self.close_reason, self.lines, seconds,
                )

    def _settle(self, seconds: float) -> None:
        if self.settled:
            return
        self.settled = True
        ledger = self.renderer.ledger
        if ledger is None or self.reservation is None:
            return
        ledger.settle(self.reservation, SessionCharge(
            device_id=self.renderer.device_id, session_id=self.session_id,
            seconds=seconds, reason=f"prompt render ({self.close_reason})",
            closed_at=self.renderer.wall()))

    # -- audio --------------------------------------------------------------

    def feed_audio(self, delta_b64) -> Optional[Tuple[float, float, int]]:
        try:
            raw = base64.b64decode(delta_b64)
        except (TypeError, ValueError):
            return None
        if len(raw) % 2:
            raw = raw[:-1]
        stats = pcm16_stats(raw)
        start = self.position_ms
        self.position_ms += stats.duration_ms
        self.timeline.extend(raw)
        return start, self.position_ms, stats.peak


class LiveVoiceRenderer:
    """Render lines of text as PCM in the Live voice; see the module docstring.

    The renderer holds at most one current session and hands it out only
    while it is open, not lost, and has room for a line before its bound.
    Every session it created is tracked until its close has settled.
    """

    def __init__(self, api_key: str, *, model: str, backend_model: str, voice: str,
                 reasoning_effort: Optional[str] = None, ledger=None, device_id: str = "prompt-render",
                 connect: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, max_attempts: int = MAX_ATTEMPTS,
                 line_timeout_s: float = LINE_TIMEOUT_S, linger_s: float = LINGER_S,
                 base_url: str = LIVE_URL):
        self.api_key = api_key
        self.model = model
        self.backend_model = backend_model
        self.voice = voice
        self.reasoning_effort = reasoning_effort
        self.ledger = ledger
        self.device_id = device_id
        self.clock = clock
        self.wall = wall
        self.max_attempts = max(1, int(max_attempts))
        self.line_timeout_s = line_timeout_s
        self.linger_s = max(0.0, linger_s)
        self.base_url = base_url
        self._connect = connect
        self._lock = asyncio.Lock()
        self._session: Optional[_RenderSession] = None
        self._sessions: Set[_RenderSession] = set()
        self._closed = False
        self.sessions = 0
        self.rendered = 0
        self.unverified = 0

    # -- session lifecycle -------------------------------------------------

    def _session_start(self) -> dict:
        history = []
        for request, answer in RENDER_EXAMPLES:
            history.append({"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": request}]})
            history.append({"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": answer}]})
        return build_session_start(
            model=self.model, instructions=RENDER_INSTRUCTIONS,
            backend_model=self.backend_model,
            backend_instructions=RENDER_BACKEND_INSTRUCTIONS, tools=[],
            voice=self.voice, reasoning_effort=self.reasoning_effort,
            history=history, sample_rate=LIVE_SAMPLE_RATE,
            client_delegation=True,
        )

    def _headroom(self) -> float:
        """A line attempt starts only with a whole line timeout left before the bound."""
        return min(self.line_timeout_s, MAX_SESSION_S / 2)

    def _forget(self, session: _RenderSession) -> None:
        self._sessions.discard(session)
        if self._session is session:
            self._session = None

    async def _acquire(self) -> _RenderSession:
        if self._closed:
            raise LiveVoiceUnavailable("shut down")
        current = self._session
        if current is not None:
            if current.claim(self._headroom()):
                return current
            # Lost, closing, or too near its bound: closed, never reused.
            if current.lost:
                reason = "lost"
            elif current.is_open:
                reason = "bound"
            else:
                reason = current.close_reason or "replaced"
            await current.close(reason)
            if self._closed:
                # The shutdown landed while the old session was closing.
                raise LiveVoiceUnavailable("shut down")
        session = _RenderSession(self)
        session.opener = asyncio.current_task()
        self._sessions.add(session)
        connect = self._connect
        if connect is None:  # pragma: no cover - the real socket
            from websockets.asyncio.client import connect as websocket_connect
            connect = websocket_connect
        try:
            if self.ledger is not None:
                session.reserve(self.ledger, min_bound_s=self._headroom())
            await session.open(connect, self.base_url, self.api_key, self._session_start())
        except BaseException as e:
            # Whatever ended the open (a refusal, a failure, a timeout, a
            # cancel from the enrollment stop button or a shutdown), the
            # session closes: its socket if one was bought, its reader, and
            # its reservation, settled once for the seconds really open.
            await session.close("open failed" if isinstance(e, Exception) else "cancelled")
            if isinstance(e, LiveVoiceUnavailable):
                raise
            if isinstance(e, Exception):
                raise LiveVoiceUnavailable("unreachable", repr(e))
            raise
        self._session = session
        self.sessions += 1
        logger.info(
            "🎙️ Live voice %r render session opened (%s); every spoken prompt is "
            "rendered here so the device never mixes voices", self.voice, session.session_id,
        )
        return session

    async def close(self, reason: str = "shutdown") -> None:
        """Close every session this renderer has, including one still opening.
        After a shutdown the renderer opens nothing more."""
        if reason == "shutdown":
            self._closed = True
        sessions = list(self._sessions)
        for session in sessions:
            if session.state == OPENING:
                opener = session.opener
                if opener is not None and not opener.done() and opener is not asyncio.current_task():
                    opener.cancel()
            else:
                session.begin_close(reason)
        for session in sessions:
            try:
                async with asyncio.timeout(OPEN_TIMEOUT_S):
                    await session.done.wait()
            except TimeoutError:
                logger.warning("⚠️ Live voice render session did not finish closing (%s)", reason)

    # -- rendering ----------------------------------------------------------

    async def render(self, lines: Iterable[str]) -> Dict[str, bytes]:
        """PCM for each line the session rendered verbatim, keyed by line.

        Lines the voice did not render verbatim within ``max_attempts`` are
        absent from the result. Raises :class:`LiveVoiceUnavailable` when no
        session could be had at all (unreachable endpoint, spend cutoff).
        """
        wanted = [line for line in dict.fromkeys(lines) if (line or "").strip()]
        results: Dict[str, bytes] = {}
        if not wanted:
            return results
        async with self._lock:
            session = await self._acquire()
            try:
                unverified_in_a_row = 0
                for index, line in enumerate(wanted):
                    if not session.is_open or session.remaining() < self._headroom():
                        break
                    if unverified_in_a_row >= STOP_AFTER_UNVERIFIED_LINES:
                        logger.warning(
                            "⚠️ Live voice rendered none of %d line(s) in a row verbatim; "
                            "stopping this render with %d line(s) left unrendered",
                            unverified_in_a_row, len(wanted) - index,
                        )
                        break
                    pcm = await self._render_line(session, line)
                    if pcm is not None:
                        results[line] = pcm
                        self.rendered += 1
                        unverified_in_a_row = 0
                    else:
                        self.unverified += 1
                        unverified_in_a_row += 1
            except BaseException as e:
                if not isinstance(e, Exception):
                    await session.close("cancelled")
                    raise
                # Whatever failed (the endpoint closing the socket, a transport
                # error, an error event), the session is done: close it, settle
                # the real seconds, and let the caller keep what was rendered.
                at_bound = session.close_reason == "bound"
                await session.close("lost")
                if not at_bound and not results:
                    if isinstance(e, LiveVoiceUnavailable):
                        raise
                    raise LiveVoiceUnavailable("unreachable", repr(e))
                self._log_unfinished(session, len(results), len(wanted))
                return results
            if not session.is_open:
                await session.close(session.close_reason)
            elif session.remaining() < self._headroom():
                await session.close("bound")
            elif self.linger_s > 0:
                session.linger(self.linger_s)
            else:
                await session.close("done")
            self._log_unfinished(session, len(results), len(wanted))
        return results

    @staticmethod
    def _log_unfinished(session: _RenderSession, rendered: int, wanted: int) -> None:
        if rendered >= wanted or session.close_reason not in ("lost", "bound"):
            return
        if session.close_reason == "bound":
            logger.warning(
                "⚠️ Live voice render session reached its %.0fs bound with %d "
                "line(s) left; they stay unrendered for now", session.bound_s, wanted - rendered,
            )
        else:
            logger.warning(
                "⚠️ Live voice render session lost after %d of %d line(s); "
                "the rest stay unrendered for now", rendered, wanted,
            )

    async def _send(self, session: _RenderSession, payload: dict) -> None:
        if not session.is_open:
            raise LiveVoiceUnavailable("unreachable", f"render session {session.close_reason or session.state}")
        try:
            await session.socket.send(json.dumps(payload))
        except Exception as e:
            if session.is_open:
                session.mark_lost()
            raise LiveVoiceUnavailable("unreachable", f"socket closed mid-render: {e!r}")

    @staticmethod
    async def _next_event(session: _RenderSession, timeout_s: float):
        """The next event, ``None`` on a quiet tick, ``_ENDED`` once the
        session's events are exhausted (EOF, or its close has begun)."""
        try:
            async with asyncio.timeout(max(0.0, timeout_s)):
                evt = await session.events.get()
        except TimeoutError:
            return None
        return _ENDED if evt is None else evt

    async def _render_line(self, session: _RenderSession, line: str) -> Optional[bytes]:
        for attempt in range(1, self.max_attempts + 1):
            if not session.is_open:
                return None
            if session.remaining() < self._headroom():
                logger.info(
                    "ℹ️ Live voice render session is at its %.0fs bound before "
                    "attempt %d; the line stays unrendered for now", session.bound_s, attempt,
                )
                return None
            attempt_state = _Attempt()
            attempt_start = len(session.timeline)
            await self._send(session, context_append("commentary", line, None,
                                                     event_id=new_event_id("line")))
            # The attempt ends at the line timeout or the session deadline;
            # the session's timer closes it at the deadline regardless.
            deadline = min(self.clock() + self.line_timeout_s, session.deadline_mono)
            ended = False
            while self.clock() < deadline:
                evt = await self._next_event(session, 0.2)
                if evt is _ENDED:
                    ended = True
                    break
                if evt is not None:
                    try:
                        self._observe(session, attempt_state, evt)
                    except LiveVoiceUnavailable:
                        # An error or close after the line was spoken: the
                        # verbatim line is kept, the caller sees ``lost``.
                        pcm = await asyncio.to_thread(cut_line, bytes(session.timeline),
                                                      list(attempt_state.transcripts), line,
                                                      attempt_start)
                        if pcm is None:
                            raise
                        session.lines += 1
                        return pcm
                if self._attempt_finished(attempt_state, line):
                    break
            # The quiet after the attempt may still carry the line's own
            # transcript deltas; the drain keeps them for the cut.
            drain_error: Optional[LiveVoiceUnavailable] = None
            if not ended and session.is_open:
                try:
                    await self._drain_quiet(session, attempt_state)
                except LiveVoiceUnavailable as e:
                    # The socket died in the quiet after the line. A verbatim
                    # line is still good; the caller sees ``lost`` and closes.
                    drain_error = e
            # Transcript timestamps are absolute on the session clock, which
            # is the output PCM timeline from session.started; the cut
            # therefore reads the whole session timeline, never a slice.
            # Off the event loop: the cut scans the whole session timeline in
            # Python, and a device session may be live on this loop (Workshop
            # canary: reply audio paced at 0.64x real time while a warm-up ran).
            pcm = await asyncio.to_thread(cut_line, bytes(session.timeline),
                                          list(attempt_state.transcripts), line, attempt_start)
            if drain_error is not None and pcm is None:
                raise drain_error
            if pcm is not None:
                session.lines += 1
                return pcm
            if session.lost or session.close_reason == "lost":
                raise LiveVoiceUnavailable("unreachable", "socket closed mid-render")
            if not session.is_open:
                return None
            logger.info(
                "ℹ️ Live voice did not render a prompt verbatim (attempt %d of %d, "
                "%d transcript chars in %d delta(s), %d timed, backend %s)",
                attempt, self.max_attempts,
                len("".join(t[0] for t in attempt_state.transcripts).strip()),
                len(attempt_state.transcripts), attempt_state.timed_deltas(),
                "completed" if attempt_state.completed else "not completed",
            )
        return None

    def _attempt_finished(self, attempt: _Attempt, line: str) -> bool:
        """Whether an attempt can be judged now.

        A transcript that is already the line ends the attempt once the voice
        has been quiet for ``QUIET_AFTER_LINE_S``. Otherwise the attempt runs
        on until the backend has completed and nothing (audio, transcript,
        completion) has happened for ``ANSWER_SETTLE_S``: the backend's
        completion precedes the frontend voicing its text, so the quiet after
        an interjection is not the end of the answer.
        """
        now = self.clock()
        if (verbatim_suffix(attempt.transcripts, line) is not None
                and attempt.last_audible_mono is not None
                and now - attempt.last_audible_mono >= QUIET_AFTER_LINE_S):
            return True
        last = attempt.last_activity()
        return attempt.completed and last is not None and now - last >= ANSWER_SETTLE_S

    def _observe(self, session: _RenderSession, attempt: _Attempt, evt: dict) -> None:
        kind = evt.get("type")
        if kind == "session.output_audio.delta":
            fed = session.feed_audio(evt.get("delta"))
            if fed is not None and fed[2] > AUDIBLE_PEAK:
                now = self.clock()
                attempt.last_audible_mono = now
                if attempt.first_audible_mono is None:
                    attempt.first_audible_mono = now
        elif kind == "session.output_transcript.delta":
            attempt.transcripts.append((str(evt.get("delta") or ""), evt.get("start_ms"), evt.get("end_ms")))
            attempt.last_transcript_mono = self.clock()
        elif kind == "session.commentary.appended" or (
                kind == "response.event" and inner_type(evt) in ("response.completed", "response.failed",
                                                                  "response.incomplete")):
            # The commentary acknowledgement is the "line is in" signal; the
            # voice speaks it after that.
            attempt.completed = True
            attempt.completed_mono = self.clock()
        elif kind == "error":
            session.fail()
            raise LiveVoiceUnavailable("unreachable", json.dumps(evt.get("error") or evt)[:200])
        elif kind == "session.closed":
            session.fail()
            raise LiveVoiceUnavailable("unreachable", "session closed by the endpoint")

    async def _drain_quiet(self, session: _RenderSession, attempt: Optional[_Attempt] = None,
                           limit_s: float = 6.0) -> None:
        """Let the output go quiet so one line's tail never lands in the next.

        Transcript deltas seen here belong to the attempt that just ended and
        are kept on it, never dropped: the line may be voiced in this quiet.
        Bounded by the session deadline as well as ``limit_s``. A close, an
        error or EOF seen here is not discarded: it raises, so the session is
        closed and settled instead of lingering dead with its reservation.
        """
        last = self.clock()
        stop = min(last + limit_s, session.deadline_mono)
        while self.clock() < stop:
            evt = await self._next_event(session, 0.1)
            if evt is _ENDED:
                if session.lost or session.close_reason == "lost":
                    raise LiveVoiceUnavailable("unreachable", "socket closed after a line")
                return  # closed at its bound or by a shutdown
            if evt is not None:
                kind = evt.get("type")
                if kind == "session.output_audio.delta":
                    fed = session.feed_audio(evt.get("delta"))
                    if fed is not None and fed[2] > AUDIBLE_PEAK:
                        last = self.clock()
                elif kind == "session.output_transcript.delta":
                    if attempt is not None:
                        attempt.transcripts.append((str(evt.get("delta") or ""),
                                                    evt.get("start_ms"), evt.get("end_ms")))
                    last = self.clock()
                elif kind == "error":
                    session.fail()
                    raise LiveVoiceUnavailable("unreachable", json.dumps(evt.get("error") or evt)[:200])
                elif kind == "session.closed":
                    session.fail()
                    raise LiveVoiceUnavailable("unreachable", "session closed by the endpoint")
            if self.clock() - last >= QUIET_AFTER_LINE_S:
                return
