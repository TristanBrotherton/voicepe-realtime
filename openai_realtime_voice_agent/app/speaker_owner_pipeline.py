"""The pipeline end of the speaker owner (app/speaker_owner.py).

``SpeakerOwnerProcessor`` sits right before the output transport when a
device's ``speaker_owner`` option is on (and is not in the pipeline at all
when it is off). It takes the model's audio frames out of the pipeline and
hands them to the owner as utterances, so they reach the device as soon as
they exist, in order with clips and announcements, instead of through the
transport's real-time pacer.

The service labels each frame (``speaker_kind``: ack, reply, result or
prompt; ``speaker_new``: a new speech segment or a new model response
starts) and ends an utterance with ``SpeakerUtteranceEndFrame`` when the
model's reply ends. A change of kind, a new segment or response and an
ordered batch marker also end it.

A quiet gap alone does not, while the service says the reply is still under
way (``reply_active``: GPT-Live has no response-done event for its voice, so
the service's own view is used: its transcript turn is open, or a routed
result's answer is still owed after an acknowledgement). Then the utterance
stays open across the pause and the device stays in its reply. Only a source
that has stopped sending anything at all (not even the silence GPT-Live
streams between words) ends it early, as truncated, and a pause is bounded.

The transport used to make the Bot speaking frames the phase emitter turns
into device phases. With the audio no longer going through it, this
processor makes them instead, upstream: started when model speech starts
going to the device, stopped when the last model speech has played out.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    ControlFrame,
    EndFrame,
    Frame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app import speaker_owner as so

logger = logging.getLogger(__name__)

# A streamed utterance whose source has been quiet this long, with the reply
# no longer under way, is ended, so the queue never waits behind a reply whose
# end marker was lost. Longer than the model's in-reply pauses (forwarded as
# audio) and its 0.8 s idle threshold.
SOURCE_QUIET_S = 1.5
# No model output at all (GPT-Live streams silence between words, so a live
# source never goes this long without a delta): the source died.
SOURCE_DEAD_S = 4.0
# The longest pause kept open inside a reply that is still under way.
REPLY_PAUSE_MAX_S = 12.0
QUIET_POLL_S = 0.25
BATCH_MARKER_WAIT_S = 30.0


class SpeakerUtteranceEndFrame(ControlFrame):
    """The model's reply ended: the open utterance is complete."""

    def __init__(self, truncated: bool = False):
        super().__init__()
        self.truncated = truncated


def label(frame: Frame, kind: str, new: bool, turn: Optional[str] = None) -> Frame:
    """Mark a model audio frame for the speaker owner."""
    frame.speaker_kind = kind
    frame.speaker_new = bool(new)
    if turn is not None:
        frame.speaker_turn = turn
    return frame


class SpeakerOwnerProcessor(FrameProcessor):
    """Model audio frames -> owner utterances; owner lifecycle -> Bot frames."""

    def __init__(self, owner: so.SpeakerOwner,
                 on_speech_sent: Optional[Callable[[], None]] = None,
                 reply_active: Optional[Callable[[], bool]] = None,
                 source_activity: Optional[Callable[[], Optional[float]]] = None,
                 clock: Callable[[], float] = time.monotonic, **kwargs):
        """``reply_active``: is the model's reply still under way?
        ``source_activity``: monotonic time of the model's last output delta
        of any kind (silence included), or None. Without them a quiet gap of
        SOURCE_QUIET_S ends the utterance."""
        super().__init__(**kwargs)
        self.owner = owner
        self._on_speech_sent = on_speech_sent
        self._reply_active = reply_active
        self._source_activity = source_activity
        self._mono_fn = clock
        self._open: Optional[so.Utterance] = None
        self._last_audio = 0.0
        self._quiet_task: Optional[asyncio.Task] = None
        self._bot_speaking = False
        self._bot_queue: Optional[asyncio.Queue] = None
        self._bot_task: Optional[asyncio.Task] = None
        owner.observers.append(self._on_owner_event)
        owner.idle_observers.append(self._sync_bot_speaking)

    # -- frames --------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame):
            self._feed(frame)
            return
        if isinstance(frame, SpeakerUtteranceEndFrame):
            self.end_stream(truncated=frame.truncated)
            return
        if (isinstance(frame, OutputTransportMessageFrame) and isinstance(frame.message, dict)
                and frame.message.get("type") == "output_batch_end"):
            # An ordered marker the device ties to the audio before it: it
            # must never overtake that audio, which now leaves through the
            # owner. Wait until the owner has sent it.
            last = self._open
            self.end_stream()
            if last is not None:
                await last.wait_settled(BATCH_MARKER_WAIT_S)
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            # The model side of the pipeline is ending. What was received so
            # far still plays (the owner belongs to the device connection);
            # an utterance cut off at its source is flagged as such.
            self.end_stream(truncated=isinstance(frame, CancelFrame))
            await self._stop_tasks()
        await self.push_frame(frame, direction)

    def _feed(self, frame: OutputAudioRawFrame) -> None:
        kind = getattr(frame, "speaker_kind", None) or so.REPLY
        turn = getattr(frame, "speaker_turn", None)
        new = bool(getattr(frame, "speaker_new", False))
        current = self._open
        if current is not None and (current.kind != kind or new or current.terminal
                                    or current.closed):
            self.end_stream()
        if self._open is None:
            self._open = self.owner.open(kind, turn, keep=kind == so.RESULT)
        self.owner.feed(self._open, frame.audio)
        self._last_audio = self._mono_fn()
        self._arm_quiet_watch()

    def end_stream(self, truncated: bool = False) -> None:
        """End the open utterance (``truncated``: its source died)."""
        current, self._open = self._open, None
        if current is not None and not current.closed:
            self.owner.end(current, truncated=truncated)

    def _arm_quiet_watch(self) -> None:
        if self._quiet_task is not None and not self._quiet_task.done():
            return
        self._quiet_task = asyncio.get_running_loop().create_task(self._quiet_watch())

    def _still_active(self) -> bool:
        if self._reply_active is None:
            return False
        try:
            return bool(self._reply_active())
        except Exception:  # pragma: no cover - a lookup never keeps audio open
            return False

    def _source_dead(self, now: float) -> bool:
        if self._source_activity is None:
            return False
        try:
            last = self._source_activity()
        except Exception:  # pragma: no cover
            return False
        return last is None or now - last >= SOURCE_DEAD_S

    async def _quiet_watch(self) -> None:
        paused_logged = None
        try:
            while self._open is not None:
                now = self._mono_fn()
                wait = self._last_audio + SOURCE_QUIET_S - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                current = self._open
                if not self._still_active():
                    logger.info("🔈 speaker owner: model audio went quiet and the reply is over; "
                                "ending its utterance")
                    self.end_stream()
                    return
                if self._source_dead(now):
                    logger.warning("🔈 speaker owner: the model's audio source stopped mid-reply; "
                                   "ending its utterance as truncated")
                    self.end_stream(truncated=True)
                    return
                if now - self._last_audio >= REPLY_PAUSE_MAX_S:
                    logger.warning(f"🔈 speaker owner: the reply paused for {REPLY_PAUSE_MAX_S:.0f} s; "
                                   "ending its utterance")
                    self.end_stream()
                    return
                if paused_logged is not current:
                    paused_logged = current
                    logger.info("🔈 speaker owner: the model paused mid-reply; its utterance "
                                "stays open")
                await asyncio.sleep(QUIET_POLL_S)
        except asyncio.CancelledError:
            raise

    # -- Bot frames ----------------------------------------------------------

    def _on_owner_event(self, utt: so.Utterance, event: str) -> None:
        if utt.kind not in so.SPEECH:
            return
        if event == so.SENDING:
            if self._on_speech_sent is not None:
                try:
                    self._on_speech_sent()
                except Exception as e:  # pragma: no cover - telemetry only
                    logger.debug(f"speech-sent observer failed: {e!r}")
        self._sync_bot_speaking()

    def _sync_bot_speaking(self) -> None:
        speaking = self.owner.speaking and any(
            u.started for u in self.owner.history() if u.kind in so.SPEECH and not u.terminal)
        if speaking == self._bot_speaking:
            return
        self._bot_speaking = speaking
        self._emit(BotStartedSpeakingFrame() if speaking else BotStoppedSpeakingFrame())

    def _emit(self, frame: Frame) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._bot_queue is None:
            self._bot_queue = asyncio.Queue()
        if self._bot_task is None or self._bot_task.done():
            self._bot_task = loop.create_task(self._bot_sender())
        self._bot_queue.put_nowait(frame)

    async def _bot_sender(self) -> None:
        while True:
            frame = await self._bot_queue.get()
            try:
                await self.push_frame(frame, FrameDirection.UPSTREAM)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"speaker owner Bot frame failed: {e!r}")

    async def _stop_tasks(self) -> None:
        for task in (self._quiet_task, self._bot_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._quiet_task = None
        self._bot_task = None

    async def cleanup(self):
        await self._stop_tasks()
        await super().cleanup()
