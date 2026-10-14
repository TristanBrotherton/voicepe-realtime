"""Bounded, revocable MULTI-SEGMENT Live speech admission.

Live has no audio-done event and can speak an acknowledgement before emitting
its delegation event. Holding a segment until the assembler proves trailing
idle lets us reject that whole acknowledgement, including its early bytes.
An acoustic boundary is NOT semantic completion. Up to 16 ordered segments
(30s each, 120s total) may be admitted per utterance; capacity failure is
terminal until the next user utterance. Already played segments cannot be
retracted. Only a segment entirely outside backend work can be delivered. Buffering also
removes provider jitter from the physical speaker's real-time playout path.

Transcript/audio alignment. GPT-Live timestamps ``session.output_transcript``
deltas on its own session clock, the one its input stream advances, while it
delivers untimed output PCM at no more than real time from ``session.started``.
Input that reaches the endpoint faster than real time therefore puts every
transcript ahead of the PCM that will carry it: the device audio buffered
during the wake handshake and flushed at session start (1.9-2.1 s on the real
Voice PE path), plus the endpoint's own delivery pace (0.94-0.97x measured),
plus short output stalls around delegation. The measured lead at transcript
receipt on the Workshop path was 2,000-2,300 ms, and a fixed wall-clock
budget discarded a complete spoken reply after a successful tool call. A
transcript's audio is therefore awaited as long as PCM keeps arriving and the
shortfall keeps shrinking; admission fails closed only when output stalls for
``alignment_s`` or the shortfall is larger than any session could explain.
"""
import re
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
from app.live_audio import OutputChunk, pcm16_stats
from app.live_protocol import LIVE_SAMPLE_RATE

# The output transcript marks sounds that are not words in brackets or
# asterisks ("[hum]", "*sighs*"). A segment whose transcript is nothing but
# such marks is a noise the voice made, not a reply: on 2026-10-11 a light-on
# confirmation was admitted and played as a hum transcribed "[hum]".
_NON_SPEECH_MARK = re.compile(r"\[[^\]]*\]|\*[^*]*\*")
_WORD = re.compile(r"[^\W_]")


def has_spoken_words(text: str) -> bool:
    """True when ``text`` has a word outside the non-speech marks."""
    return bool(_WORD.search(_NON_SPEECH_MARK.sub(" ", text or "")))


def is_non_speech(text: str) -> bool:
    """A transcript that is only non-speech marks, such as "[hum]"."""
    return bool((text or "").strip()) and not has_spoken_words(text)

# The largest transcript-ahead-of-PCM shortfall a real session can produce:
# the whole pre-start device buffer (PRESTART_AUDIO_MAX_S, 5 s) plus the
# measured delivery deficit over the longest permitted session (about 4.4 % of
# 120 s). Beyond it the timeline is not merely late, it is broken.
ALIGNMENT_MAX_OWED_MS = 12000.


@dataclass
class Reply:
    chunks: list = field(repr=False)
    text: str
    generation: int
    segment: int = 0
    prompt_complete: bool = False


@dataclass
class ReplyStreamStart:
    """A final-answer segment that is safe to begin before its quiet tail."""

    chunks: list = field(repr=False)
    generation: int = 0
    segment: int = 0


@dataclass
class ReplyStreamChunk:
    """One ordered chunk belonging to an already-started reply stream."""

    chunk: OutputChunk = field(repr=False)
    generation: int = 0
    segment: int = 0


@dataclass
class ReplyStreamEnd:
    """The proven quiet boundary for a streamed reply segment."""

    text: str
    generation: int
    segment: int
    nbytes: int
    prompt_complete: bool = False


class LiveReplyAdmission:
    def __init__(self, max_seconds=30, max_turn_seconds=120, alignment_s=2., clock=time.monotonic,
                 max_owed_ms=ALIGNMENT_MAX_OWED_MS):
        self.limit = int(max_seconds * LIVE_SAMPLE_RATE * 2)
        self.turn_limit = int(max_turn_seconds * LIVE_SAMPLE_RATE * 2)
        # How long the output stream may stop making progress towards a
        # timestamped transcript before the wait fails closed. Not a bound on
        # the wait itself: owed PCM arrives at the endpoint's real-time pace.
        self.alignment_s = alignment_s
        self.max_owed_ms = max_owed_ms
        self.clock = clock
        self.generation = 0
        self.reset()

    def reset(self):
        self.generation += 1
        self.delivered = False
        self.pending = set()
        self.enqueued = set()
        self.segments = 0
        self.total_bytes = 0
        self.terminal = False
        self.failure_reason = ""
        self.alignment_deadline = None
        self.boundary_pending = False
        self.transcript_seen = False
        self.transcript_end_ms = None
        self.audio_end_ms = None
        self.response_text = ""
        self.chunks = []
        self.text = ""
        self.nbytes = 0
        self.rejected = False
        self.has_audio = False
        self._recovery_chunks = []
        self._recovery_bytes = 0
        self._recovery_text = ""
        self.streaming_segment = 0
        self.streaming_bytes = 0

    def invalidate(self, reject_segment=True):
        # Do not reset at backend completion: the tail of the acknowledgement
        # is still the same rejected segment until a proven silent boundary.
        if not self.rejected:
            self.generation += 1
        self.pending.clear()
        self.enqueued.clear()
        self.delivered = False
        self.transcript_seen = False
        self.text = ""
        self.response_text = ""
        self.transcript_end_ms = None
        self.audio_end_ms = None
        self.alignment_deadline = None
        self.boundary_pending = False
        self.rejected = reject_segment
        self.chunks.clear()
        self.nbytes = 0
        self._recovery_chunks = []
        self._recovery_bytes = 0
        self._recovery_text = ""
        self.streaming_segment = 0
        self.streaming_bytes = 0

    def transcript(self, delta, busy=False, end_ms=None):
        if self.terminal:
            return
        if not busy:
            self.response_text = (self.response_text + delta)[:8192]
            if isinstance(end_ms, (int, float)):
                self.transcript_end_ms = max(self.transcript_end_ms or 0., end_ms)
        if not self.rejected:
            self.text = (self.text + delta)[:8192]
        elif not busy:
            # Final text can lead its PCM, while the quiet boundary is still
            # being proved. Keep it with the retained tail, not with the ack.
            self._recovery_text = (self._recovery_text + delta)[:8192]

    def fail(self, reason):
        self.invalidate()
        self.terminal = True
        self.failure_reason = reason
        logger.warning("Live reply admission failed closed: %s", reason)

    def can_deliver(self, reply):
        return (not self.terminal and not self.rejected
                and reply.generation == self.generation and reply.segment in self.pending)

    def mark_enqueued(self, reply):
        if self.can_deliver(reply):
            self.pending.remove(reply.segment)
            self.enqueued.add(reply.segment)

    def mark_delivered(self, generation, segment=None):
        if generation == self.generation and self.enqueued and not self.rejected and not self.terminal:
            if segment not in self.enqueued:
                return
            self.enqueued.remove(segment)
            self.delivered = True

    def owed_ms(self):
        """PCM still to arrive before the timestamped transcript is covered."""
        if self.transcript_end_ms is None:
            return 0.
        return max(0., self.transcript_end_ms - (self.audio_end_ms or 0.))

    def check_alignment(self):
        if self.alignment_deadline is not None and self.clock() >= self.alignment_deadline:
            self.fail("transcript/audio alignment timeout: no output progress for "
                      f"{self.alignment_s:.1f}s with {self.owed_ms():.0f} ms still owed")

    def feed(self, chunk: OutputChunk, busy: bool, end_ms=None, pending=False,
             streamable=False):
        self.check_alignment()
        if self.terminal:
            return None
        if isinstance(end_ms, (int, float)) and not busy:
            advanced = self.audio_end_ms is None or end_ms > self.audio_end_ms
            self.audio_end_ms = max(self.audio_end_ms or 0., end_ms)
            if (advanced and self.alignment_deadline is not None
                    and self.transcript_end_ms is not None
                    and self.audio_end_ms < self.transcript_end_ms):
                # Owed PCM is arriving. Keep waiting for it; the budget only
                # bounds a stalled stream, never the endpoint's own pace. A wait
                # for a missing transcript is deliberately not extended: more
                # silence brings no text.
                self.alignment_deadline = self.clock() + self.alignment_s
        if chunk.reason == "idle run exceeded max_silence_ms":
            if self.streaming_segment:
                waiting_for_timed_audio = (self.transcript_end_ms is not None
                    and (self.audio_end_ms is None or self.audio_end_ms < self.transcript_end_ms))
                if waiting_for_timed_audio:
                    if self.owed_ms() > self.max_owed_ms:
                        self.fail("transcript/audio alignment diverged: "
                                  f"{self.owed_ms():.0f} ms owed exceeds {self.max_owed_ms:.0f} ms")
                        return None
                    if self.alignment_deadline is None:
                        self.alignment_deadline = self.clock() + self.alignment_s
                    return None
                segment = self.streaming_segment
                prompt_complete = (self.response_text.rstrip().endswith('?')
                    and self.transcript_end_ms is not None and self.audio_end_ms is not None
                    and self.audio_end_ms >= self.transcript_end_ms)
                event = ReplyStreamEnd(
                    self.text, self.generation, segment,
                    self.streaming_bytes, prompt_complete,
                )
                self.transcript_seen = True
                self.alignment_deadline = None
                self.boundary_pending = False
                self.chunks = []
                self.text = ""
                self.nbytes = 0
                self.has_audio = False
                self.streaming_segment = 0
                self.streaming_bytes = 0
                self._recovery_chunks = []
                self._recovery_bytes = 0
                self._recovery_text = ""
                return event
            if not self.has_audio:
                # Output transcript deltas may lead the corresponding PCM.
                # Do not erase them on unrelated idle audio deltas.
                if busy:
                    self.text = ""
                # Proven digital idle is not a quiet speech onset. Keep any
                # leading transcript, but never spend device playout on zeros.
                self.chunks = []
                self.nbytes = 0
                self._recovery_chunks = []
                self._recovery_bytes = 0
                self._recovery_text = ""
                self.rejected = False
                return None
            reply = None
            if self.has_audio and not (self.rejected or busy):
                waiting_for_timed_audio = (self.transcript_end_ms is not None
                    and (self.audio_end_ms is None or self.audio_end_ms < self.transcript_end_ms))
                if waiting_for_timed_audio and self.owed_ms() > self.max_owed_ms:
                    self.fail("transcript/audio alignment diverged: "
                              f"{self.owed_ms():.0f} ms owed exceeds {self.max_owed_ms:.0f} ms")
                    return None
                if (not self.text.strip() and not self.transcript_seen) or waiting_for_timed_audio:
                    if self.alignment_deadline is None:
                        self.alignment_deadline = self.clock() + self.alignment_s
                    return None
                if is_non_speech(self.text):
                    logger.warning(
                        "🔇 live reply segment discarded: its transcript is only a non-speech "
                        "mark (%d chars), not words", len(self.text.strip()))
                    self.alignment_deadline = None
                    self.boundary_pending = False
                    self.chunks = []
                    self.text = ""
                    self.nbytes = 0
                    self.has_audio = False
                    self._recovery_chunks = []
                    self._recovery_bytes = 0
                    self._recovery_text = ""
                    return None
                if pending:
                    self.boundary_pending = True
                    return None
                self.boundary_pending = False
                self.segments += 1
                if self.segments > 16:
                    self.fail("multi-segment response limit")
                    return None
                prompt_complete = (self.response_text.rstrip().endswith('?')
                    and self.transcript_end_ms is not None and self.audio_end_ms is not None
                    and self.audio_end_ms >= self.transcript_end_ms)
                reply = Reply(self.chunks, self.text, self.generation, self.segments, prompt_complete)
                self.pending.add(self.segments)
                self.transcript_seen = True
                self.alignment_deadline = None
            leading_text = self._recovery_text if self.rejected and not busy else ""
            self.chunks = []
            self.text = leading_text
            self.nbytes = 0
            self.has_audio = False
            self.rejected = False
            self._recovery_chunks = []
            self._recovery_bytes = 0
            self._recovery_text = ""
            return reply
        if not chunk.pcm:
            return None
        self.has_audio |= chunk.audible
        if self.streaming_segment:
            if busy:
                self.fail("backend became busy after streamed reply admission")
                return None
            self.total_bytes += len(chunk.pcm)
            self.streaming_bytes += len(chunk.pcm)
            if self.streaming_bytes > self.limit or self.total_bytes > self.turn_limit:
                self.fail("streamed response PCM capacity exceeded")
                return None
            return ReplyStreamChunk(
                chunk, self.generation, self.streaming_segment
            )
        if busy:
            self.invalidate()
        if self.rejected and not busy:
            # Real Vesper traces contain 1–2s of low-level generated noise
            # (RMS 18–37, occasional peaks to 302) between ack and final result.
            # The assembler intentionally preserves it, so its near-zero idle
            # boundary cannot separate these discarded acknowledgements.
            # A full 400ms quiet tail may end ONLY an already-rejected segment.
            # Preserve that tail verbatim as the new candidate's prelude: no
            # low-level onset/fricative is energy-gated out of an admitted reply.
            stats = pcm16_stats(chunk.pcm)
            if stats.peak <= 512 and stats.rms <= 80:
                self._recovery_chunks.append(chunk)
                self._recovery_bytes += len(chunk.pcm)
                if self._recovery_bytes > self.limit:
                    self._recovery_chunks = []
                    self._recovery_bytes = 0
                    self._recovery_text = ""
                    return None
                if self._recovery_bytes >= LIVE_SAMPLE_RATE * 2 * .4:
                    self.rejected = False
                    self.has_audio = False
                    self.chunks = self._recovery_chunks
                    self.nbytes = self._recovery_bytes
                    self.text = self._recovery_text
                    self._recovery_text = ""
                    self._recovery_chunks = []
                    self._recovery_bytes = 0
                    self._recovery_text = ""
                    return None
            else:
                self._recovery_chunks = []
                self._recovery_bytes = 0
                self._recovery_text = ""
        if not self.rejected:
            if self.alignment_deadline is not None:
                if not self.text.strip():
                    self.fail("new segment before transcript alignment")
                    return None
                if (self.transcript_end_ms is None or
                        (self.audio_end_ms is not None
                         and self.audio_end_ms >= self.transcript_end_ms)):
                    # Timestamped text led PCM across a natural pause. Retain
                    # both segments until the matching audio has actually
                    # arrived; an arbitrary next delta is not sufficient.
                    self.alignment_deadline = None
            self.nbytes += len(chunk.pcm)
            self.total_bytes += len(chunk.pcm)
            if self.nbytes > self.limit or self.total_bytes > self.turn_limit:
                self.fail("response PCM capacity exceeded")
            else:
                self.chunks.append(chunk)
                # Hybrid admission: only a post-backend final answer may start
                # before its trailing quiet boundary. The rejected pre-tool
                # segment has already been separated by the recovery fence
                # above, and confirmation questions remain fully buffered.
                if streamable and has_spoken_words(self.text) and self.has_audio:
                    self.segments += 1
                    if self.segments > 16:
                        self.fail("multi-segment response limit")
                        return None
                    self.streaming_segment = self.segments
                    self.streaming_bytes = self.nbytes
                    self.pending.add(self.segments)
                    chunks = self.chunks
                    self.chunks = []
                    self.nbytes = 0
                    return ReplyStreamStart(
                        chunks, self.generation, self.streaming_segment
                    )
        return None
