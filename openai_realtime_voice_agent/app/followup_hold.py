"""Hold the device's follow-up mic window open while the user is still talking.

The firmware closes its follow-up window on a fixed timer
(``follow_up_listen_seconds``) and the only thing that cancels that timer is a
``listening`` phase from the add-on. Under GPT-Live the add-on has no VAD: the
one speech signal it gets is the input transcript, and that arrives at the END
of the utterance (lab, 140 turns: the device's ``listening`` landed 2.4-3.3 s
after the wake for phrases spoken over 1.8-3.1 s). So a follow-up begun late in
the window was cut off mid-sentence by the device, the half utterance flushed,
and the paid session closed on the cut-off before any transcript could come.
The session gate had the same blind spot: mic audio does not extend the
"follow-up quiet" bound, only a transcript does.

The add-on hears the microphone itself, so it can tell the device sooner. While
the device sits in its follow-up window, a cheap energy onset detector runs over
the inbound frames; when sustained speech-level sound starts, the add-on emits
``listening`` (firmware 1.3.1 cancels its window timer on that phase and keeps
the mic open until the turn ends) and moves the session to its ordinary idle
bounds, so the transcript that proves the utterance has time to arrive.

The hold is bounded. If no user turn opens within ``hold_max_s`` the mic is
released with an ``idle`` (the firmware closes the orphaned mic gate on it) and
the gate closes the session a few seconds later, so room noise can never keep a
session billed. A wake, a stop or a flush from the device disarms the hold at
once. The detector is pure and clock-free; the coordinator owns the one timer.
"""
from __future__ import annotations

import array
import asyncio
import logging
import math
import os
import time
from collections import deque
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_MS = 20
# Speech-level sound must be louder than both an absolute floor and a multiple
# of the room's measured floor. Int16 RMS over 20 ms blocks, measured on a
# Voice PE's own mic stream: a quiet room sits at 210-440 (5th-75th
# percentile), speech near the device at 3700-8900 (95th-99th). 600 is about
# -35 dBFS: above the room, a sixth of quiet speech.
MIN_RMS = 600.0
NOISE_RATIO = 3.0
# How long sound must stay above the threshold before it counts as speech,
# and how many quiet blocks a run may contain (a stop consonant, a breath).
ONSET_MS = 200
MAX_DIPS = 2
# The room's floor is the quietest block of the last FLOOR_WINDOW_S of mic
# audio (speech has gaps between words; a fan or a fridge hum never drops, so
# it becomes the floor and never counts as speech). Learned from every frame
# the mic sends, held or not, so the window opens with the floor already known.
FLOOR_WINDOW_S = 2.0
FLOOR_MIN = 20.0

# The hold never outlives this without a user turn: a long rambling follow-up
# is transcribed when it ends, and anything longer than this is not one.
HOLD_MAX_S = 12.0


def _env_seconds(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


class SpeechOnsetDetector:
    """Energy onset over PCM16 mono frames of any size. ``feed`` returns True
    once per arming when sustained speech-level sound begins."""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, min_rms: float = MIN_RMS,
                 noise_ratio: float = NOISE_RATIO, onset_ms: int = ONSET_MS,
                 max_dips: int = MAX_DIPS):
        self.block_samples = max(1, sample_rate * BLOCK_MS // 1000)
        self.min_rms = float(min_rms)
        self.noise_ratio = float(noise_ratio)
        self.onset_blocks = max(1, int(math.ceil(onset_ms / BLOCK_MS)))
        self.max_dips = max(0, int(max_dips))
        self._recent = deque(maxlen=max(1, int(FLOOR_WINDOW_S * 1000 / BLOCK_MS)))
        self._carry = b""
        self.reset()

    def reset(self) -> None:
        """A new window: forget the run, keep what was learned about the room."""
        self._loud = 0
        self._dips = 0
        self._carry = b""
        self.fired = False
        self.fed_ms = 0
        self.last_rms = 0.0

    @property
    def noise(self) -> float:
        """The room's floor: the quietest recent block (int16 RMS)."""
        return max(FLOOR_MIN, min(self._recent)) if self._recent else FLOOR_MIN

    @property
    def threshold(self) -> float:
        return max(self.min_rms, self.noise_ratio * self.noise)

    def _blocks(self, pcm: bytes):
        data = self._carry + bytes(pcm)
        step = self.block_samples * 2
        whole = len(data) - len(data) % step
        self._carry = data[whole:]
        if whole <= 0:
            return
        samples = array.array("h")
        samples.frombytes(data[:whole])
        n = self.block_samples
        for start in range(0, len(samples), n):
            block = samples[start:start + n]
            self.fed_ms += BLOCK_MS
            rms = math.sqrt(sum(x * x for x in block) / n)
            self.last_rms = rms
            yield rms

    def observe(self, pcm: bytes) -> None:
        """Learn the floor from a frame without looking for an onset."""
        for rms in self._blocks(pcm):
            self._recent.append(rms)

    def feed(self, pcm: bytes) -> bool:
        """Feed one inbound frame. True on the block where the onset is met."""
        if self.fired:
            self.observe(pcm)
            return False
        for rms in self._blocks(pcm):
            # Judged against the floor before this block joins it, so a loud
            # block cannot raise its own bar.
            loud = rms >= self.threshold or (self._loud and rms >= self.min_rms)
            self._recent.append(rms)
            if loud:
                self._loud += 1
                if self._loud >= self.onset_blocks:
                    self.fired = True
                    return True
                continue
            if self._loud:  # a short dip inside a run (a stop consonant, a breath)
                self._dips += 1
                if self._dips > self.max_dips:
                    self._loud = 0
                    self._dips = 0
        return False


class FollowUpHold:
    """Per-device coordinator: arms on the idle that opens a follow-up window,
    holds on a speech onset, releases on a bound. Fed from the serializer's
    audio tap (``feed``) and the phase emitter (``note_phase``)."""

    def __init__(self, *, hold_listening: Callable[[], Awaitable[bool]],
                 release_idle: Callable[[], Awaitable[None]],
                 may_hold: Optional[Callable[[], bool]] = None,
                 on_onset: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None,
                 spawn: Optional[Callable[[Awaitable], object]] = None,
                 hold_max_s: Optional[float] = None, clock=time.monotonic,
                 detector: Optional[SpeechOnsetDetector] = None):
        self._hold_listening = hold_listening
        self._release_idle = release_idle
        self._may_hold = may_hold
        self._on_onset = on_onset
        self._on_release = on_release
        self._spawn = spawn or (lambda coro: asyncio.get_running_loop().create_task(coro))
        self.hold_max_s = (hold_max_s if hold_max_s is not None
                           else _env_seconds("FOLLOW_UP_HOLD_MAX_S", HOLD_MAX_S, 2.0, 60.0))
        self._clock = clock
        self.detector = detector or SpeechOnsetDetector()
        self.armed = False
        self.held = False
        self._armed_mono = 0.0
        self.heard_mono: Optional[float] = None
        self._saw_reply = False
        self._own_listening = 0
        self._release_task: Optional[asyncio.Task] = None
        self._closed = False
        self.holds = 0
        self.releases = 0

    # -- what the phase emitter sends the device ----------------------------

    def note_phase(self, value: str, extra: Optional[dict] = None) -> None:
        if self._closed:
            return
        if value == "replying":
            self._saw_reply = True
            self.disarm("reply")
        elif value == "idle":
            if (extra or {}).get("followup") is False or not self._saw_reply:
                # A silent turn, a bare connect, our own release: no window.
                self.disarm("idle without a follow-up window")
                return
            self._saw_reply = False
            self.arm()
        elif value == "listening":
            if self._own_listening:
                self._own_listening -= 1
                return
            self._turn_progressed("listening")
        else:
            self._turn_progressed(value)

    def arm(self) -> None:
        """The device is opening its follow-up window: watch for speech."""
        self._cancel_release()
        self.detector.reset()
        self.armed = True
        self.held = False
        self._armed_mono = self._clock()

    def disarm(self, why: str = "") -> None:
        """Nothing to hold any more (a wake, a stop, a flush, a new reply)."""
        was_held = self.held
        self._cancel_release()
        self.armed = False
        self.held = False
        if was_held and why:
            logger.info(f"🎤 follow-up hold ended ({why})")

    def note_wake(self) -> None:
        self._saw_reply = False
        self.disarm("wake")

    def note_flush(self) -> bool:
        """The device's window timer fired. True when speech was heard in it
        (the ``listening`` lost the race): the session must then wait for the
        transcript instead of closing on the cut-off."""
        heard = self.held
        self.disarm("flush" if heard else "")
        return heard

    def speech_heard_within(self, seconds: float) -> bool:
        return self.heard_mono is not None and self._clock() - self.heard_mono <= seconds

    def _turn_progressed(self, phase: str) -> None:
        if self.held:
            since = (self._clock() - (self.heard_mono or self._clock())) * 1000
            logger.info(f"🎤 follow-up hold: the turn went on ({phase}) {since:.0f} ms after speech was heard")
        self._cancel_release()
        self.armed = False
        self.held = False

    # -- the microphone --------------------------------------------------------

    def feed(self, pcm: bytes) -> None:
        """Audio tap: every forwarded inbound frame, in the receive path."""
        if self._closed:
            return
        if not self.armed or self.held:
            self.detector.observe(pcm)  # keep the room's floor current
            return
        if not self.detector.feed(pcm):
            return
        if self._may_hold is not None and not self._may_hold():
            # No paid session to keep: a window streaming into nothing.
            self.armed = False
            return
        self.held = True
        self.heard_mono = self._clock()
        self.holds += 1
        into = (self.heard_mono - self._armed_mono) * 1000
        logger.info(f"🎤 follow-up: speech heard {into:.0f} ms into the window "
                    f"(rms {self.detector.last_rms:.0f}, floor {self.detector.noise:.0f}); "
                    "holding the mic open")
        self._spawn(self._hold())

    async def _hold(self) -> None:
        self._own_listening += 1
        try:
            sent = await self._hold_listening()
        except Exception as e:  # never let the hold break the audio path
            logger.warning(f"⚠️ follow-up hold could not send listening: {e!r}")
            sent = False
        if not sent:
            self._own_listening = max(0, self._own_listening - 1)
            self.held = False
            self.armed = False
            return
        if self._on_onset is not None:
            try:
                self._on_onset()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"follow-up onset observer failed: {e!r}")
        self._cancel_release()
        self._release_task = asyncio.get_running_loop().create_task(self._release_after(self.hold_max_s))

    async def _release_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if not self.held or self._closed:
            return
        self.held = False
        self.armed = False
        self.releases += 1
        logger.info(f"🎤 follow-up hold released after {seconds:.0f}s: no transcript came; "
                    "closing the mic")
        try:
            await self._release_idle()
        except Exception as e:
            logger.warning(f"⚠️ follow-up hold could not release the mic: {e!r}")
        if self._on_release is not None:
            try:
                self._on_release()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"follow-up release observer failed: {e!r}")

    def _cancel_release(self) -> None:
        task, self._release_task = self._release_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def close(self) -> None:
        self._closed = True
        self.armed = False
        self.held = False
        task, self._release_task = self._release_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
