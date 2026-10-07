"""GPT-Live output audio: assemble ``session.output_audio.delta`` for the device.

The official reference is specific about what arrives here
(``learn.microsoft.com/azure/foundry/openai/gpt-live-reference`` §Audio format
and §session.output_audio.delta, and developers.openai.com → Live WebSockets):

* "raw, headerless, mono, signed 16-bit little-endian PCM sampled at 24,000 Hz";
* "Base64-encode the raw PCM bytes — not a WAV file or another audio container";
* "Each sample is two bytes, so the decoded payload must contain an even number
  of bytes";
* every delta carries ``start_ms``/``end_ms`` on the server timeline, and **"a
  gap between ranges represents omitted silence"**;
* "There's no output-audio-done event."

That last pair is the part the first canary got wrong. The server does **not**
stream continuous silence: it omits it and tells you so through the timeline.
An energy gate over the deltas therefore has nothing legitimate to remove — it
can only delete quiet *speech* (releases, fricatives, sentence tails) and
splice the surviving fragments together, which is exactly how a clean 24 kHz
PCM stream turns into something unintelligible at the speaker.

So this module does the opposite of a gate. It is a verifier and an assembler:

Measured against the real endpoint on 2026-10-07 with ``app.live_probe``
(three sessions, 148 / 420 / 480 deltas):

* deltas are a fixed **100 ms / 4800 bytes** except for a short final one
  (1920 bytes observed), always an even length, peak ≈ 13,000 (no clipping);
* ``start_ms``/``end_ms`` are **not sent** on ``session.output_audio.delta``
  even though the reference documents them — 0 of 1,048 deltas carried them.
  They *are* sent on the transcript deltas. Anything keyed on audio-delta
  timing is therefore dead code against the live endpoint;
* the stream is **continuous, silence included**: 80/148, 340/420 and 402/480
  deltas were digital silence, in unbroken runs of several seconds between
  replies. There is no output-audio-done event, so without intervention the
  device would sit in REPLYING forever;
* **during speech the stream runs at 1.02–1.11× real time** (five-second run:
  5.00 s of audio over 4.91 s of wall clock), with in-speech inter-arrival p50
  = 100 ms, p95 = 100–159 ms and session-wide spikes to 341–380 ms.

So the canary's energy gate was wrong in method, and the pacing — not the gate
— is the dominant defect. A 1.0× source gives the device no playout lead to
start a reply with and only ~20–80 ms of lead per second afterwards, while
inter-frame jitter already reaches ~160 ms: the I2S chain starves from the
first word. The relay must supply the lead (see ``OutputLeadBuffer`` and the
Live default for ``output_lead_buffer_ms``); this module must not corrupt the
waveform on the way there. So it:

* decodes strictly and keeps byte alignment across deltas (a stray odd byte is
  carried, never dropped, so a 16-bit sample is never split);
* forwards audio **untouched and in arrival order** — no energy threshold, no
  pre-roll splice, no resampling, no silence insertion, no reordering;
* suppresses only a *run* of inaudible audio longer than ``max_silence_ms``, so
  natural pauses inside a reply (measured: up to ~600 ms between sentences)
  survive byte-for-byte and only genuine idle ends the reply;
* **withholds the head of an idle run instead of forwarding it.** This is the
  2026-10-07 second-canary fix; see "Leading idle runs" below;
* records ``start_ms``/``end_ms`` when present, purely as measurement;
* counts everything a failed turn needs for diagnosis, without keeping a word
  of what anybody said.

Leading idle runs
-----------------

The second canary (2026-10-07 09:48 PDT) still produced audible static on a
reply whose bytes were provably perfect: 18/64 deltas forwarded, 86,400 bytes,
``odd-length 0``, ``undecodable 0``, ``backlog-dropped 0``, pace 0.981x. The
device reported ``underrun 1`` and ``ws_gap_max_ms 4804``. Byte-integrity tests
could not reproduce it because the bytes were never the problem.

Measured cause (``tests/test_live_idle_to_speech.py``, the real assembler ->
``OutputLeadBuffer`` -> the real ``FastAPIWebsocketOutputTransport``): the
suppression rule above forwards the **first** ``max_silence_ms`` of *every*
inaudible run, and GPT-Live streams continuous idle silence between replies. So
800 ms of digital zeros went to the device ahead of every reply. Those are
``TTSAudioRawFrame``s like any other, so they

1. made the output transport emit ``BotStartedSpeaking`` — the device entered
   ``REPLYING`` on silence, before the model had said anything; and
2. armed and drained the 600 ms ``OutputLeadBuffer``, which then had nothing
   left to hold when the real words arrived seconds later.

The instrumented replay shows it exactly: 3 s of idle then speech gave a first
device write at 0.521 s (the lead, spent on zeros) followed by a **3,659 ms gap
with the device already in REPLYING**, then speech arriving at the 1.0x pacer
with no cushion. That is the device-side dry-chain starve the earlier static
investigation documented, reached by a different route.

So an inaudible run is now *held* rather than forwarded while the reply has not
started, and the hold is **discarded whole** once the run proves to be idle:

* an inaudible run arriving while the reply is already in progress is forwarded
  in real time, exactly as before — a 600 ms inter-sentence pause must keep the
  device's audio chain fed, and withholding it would starve the very chain this
  is protecting;
* an inaudible run arriving *before* any speech is buffered (at most
  ``max_silence_ms``, so ~38 KB) and released byte-for-byte with the first
  audible delta, so a short lead-in survives and helps fill the lead;
* once such a run passes ``max_silence_ms`` it is idle, not a pause: the held
  head is discarded along with the rest of the run, and nothing reaches the
  device until the model speaks again.

The reply therefore begins at the device with *speech*, the lead is accumulated
on speech, and no byte of audio is altered, reordered or synthesised.

See also ``monitoring/`` (private): the firmware lesson from the earlier static
investigation was *never* to re-introduce application-level silence injection.
Nothing here synthesises audio; it only forwards or withholds what the server
sent.
"""
import array
import base64
import binascii
import hashlib
import math
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from app.live_protocol import LIVE_SAMPLE_RATE

BIG_ENDIAN_HOST = sys.byteorder == "big"

BYTES_PER_SAMPLE = 2
# Peak sample magnitude at or below which a delta carries nothing audible.
# Measured: idle runs are exactly 0, and the frames immediately around speech
# sit at 1-3, while the quietest frames that still carry speech (releases and
# sentence tails) peak at 44-456. 4 separates them with a wide margin and is
# -78 dBFS, so nothing audible can ever be classified as silence.
SILENCE_PEAK = 4
# Consecutive inaudible audio passed through before it is suppressed. Measured
# intra-reply pauses reach ~600 ms between sentences and must survive exactly;
# idle runs between replies are several seconds. 800 ms keeps every natural
# pause and still ends the reply promptly, which matters because the protocol
# has no output-audio-done event.
MAX_SILENCE_MS = 800
# Quiet time after the last forwarded audio that still counts as "the model is
# speaking" (there is no end-of-response event to ask).
SPEECH_HANGOVER_S = 1.0
# A fresh spoken segment: audible audio after this much quiet on the
# device-bound stream.
SEGMENT_GAP_MS = 1200
# Bounded diagnostic ring; one small record per delta.
DELTA_HISTORY = 240
# Reasons a delta correctly produced no device-bound audio. Any other reason
# means the delta itself was malformed and is logged as such.
WITHHELD_REASONS = frozenset({
    "idle run exceeded max_silence_ms",
    "holding a leading pause",
})


@dataclass(frozen=True)
class Pcm16Stats:
    """Cheap, privacy-safe facts about one PCM16 payload."""

    samples: int
    rms: float
    peak: int
    all_zero: bool

    @property
    def duration_ms(self) -> float:
        return 1000.0 * self.samples / LIVE_SAMPLE_RATE

    def inaudible(self, silence_peak: int = SILENCE_PEAK) -> bool:
        """True when no sample in this payload can be heard."""
        return self.samples == 0 or self.peak <= silence_peak


def pcm16_stats(pcm: bytes, sample_rate: int = LIVE_SAMPLE_RATE) -> Pcm16Stats:
    """RMS/peak/all-zero for mono PCM16 little-endian bytes."""
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    if usable <= 0:
        return Pcm16Stats(0, 0.0, 0, True)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if BIG_ENDIAN_HOST:  # pragma: no cover - the wire format is little-endian
        samples.byteswap()
    peak = 0
    total = 0
    for value in samples:
        total += value * value
        magnitude = -value if value < 0 else value
        if magnitude > peak:
            peak = magnitude
    rms = math.sqrt(total / len(samples))
    return Pcm16Stats(len(samples), rms, peak, peak == 0)


@dataclass
class AudioDelta:
    """One observed ``session.output_audio.delta`` (no audio retained)."""

    arrival_s: float
    start_ms: Optional[int]
    end_ms: Optional[int]
    nbytes: int
    rms: float
    peak: int
    all_zero: bool


@dataclass
class OutputChunk:
    """What to hand the pipeline for one delta."""

    pcm: bytes = b""
    started_segment: bool = False
    audible: bool = False
    gap_ms: int = 0
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.pcm)


@dataclass
class OutputAudioMetrics:
    """Per-session output-audio counters. Numbers only — never content."""

    deltas: int = 0
    forwarded_deltas: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    segments: int = 0
    undecodable: int = 0
    odd_length: int = 0
    carried_bytes: int = 0
    silent_deltas: int = 0
    silence_suppressed_deltas: int = 0
    silence_suppressed_ms: float = 0.0
    # Inaudible deltas that did reach the device: an in-reply pause forwarded in
    # real time, or a short lead-in released with the first word.
    silence_released_deltas: int = 0
    silence_released_ms: float = 0.0
    # Of those, the ones released out of the leading hold. They travel inside
    # the chunk for the first audible delta, so chunks < forwarded deltas by
    # exactly this much.
    silence_released_from_hold_deltas: int = 0
    # Inaudible deltas discarded out of the leading hold because their run
    # turned out to be idle. Before the 2026-10-07 fix these were forwarded and
    # spent the device's playout lead on zeros; a non-zero count here is the
    # lead being protected, not a fault.
    idle_head_discarded_deltas: int = 0
    idle_head_discarded_ms: float = 0.0
    # Inaudible deltas still held, awaiting speech or the idle threshold.
    silence_held_deltas: int = 0
    untimed_deltas: int = 0
    timeline_gaps: int = 0
    timeline_gap_ms_total: int = 0
    timeline_gap_ms_max: int = 0
    timeline_overlaps: int = 0
    peak: int = 0
    first_delta_s: Optional[float] = None
    last_delta_s: Optional[float] = None
    # Audio seconds delivered per wall-clock second between the first and last
    # delta. GPT-Live is a real-time model: a ratio near 1.0 means the device
    # can never build a playout lead on its own and the relay must provide one.
    realtime_ratio: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return dict(self.__dict__)


class LiveOutputAudio:
    """Verify and assemble GPT-Live output audio for the device transport.

    ``feed`` takes the base64 ``delta`` plus the event's ``start_ms``/``end_ms``
    and returns an :class:`OutputChunk`. The returned PCM is a byte-exact,
    in-order subsequence of what the server sent; nothing is synthesised,
    resampled or re-ordered.
    """

    def __init__(
        self,
        *,
        sample_rate: int = LIVE_SAMPLE_RATE,
        max_silence_ms: int = MAX_SILENCE_MS,
        silence_peak: int = SILENCE_PEAK,
        speech_hangover_s: float = SPEECH_HANGOVER_S,
        segment_gap_ms: int = SEGMENT_GAP_MS,
        clock: Callable[[], float] = time.monotonic,
        capture: Optional["OutputAudioCapture"] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.max_silence_ms = max_silence_ms
        self.silence_peak = silence_peak
        self.speech_hangover_s = speech_hangover_s
        self.segment_gap_ms = segment_gap_ms
        self._clock = clock
        self.capture = capture
        self.metrics = OutputAudioMetrics()
        self.history: Deque[AudioDelta] = deque(maxlen=DELTA_HISTORY)
        self._digest = hashlib.sha256()
        self._carry = b""
        self._turn_mark: Dict[str, object] = self.metrics.as_dict()
        self._previous_end_ms: Optional[int] = None
        self._silence_run_ms = 0.0
        # A leading inaudible run, held until speech resumes (release it) or the
        # run proves idle (discard it). Bounded by max_silence_ms.
        self._pending: List[bytes] = []
        self._pending_ms = 0.0
        # True between the first audible delta of a reply and the idle run that
        # ends it. Only an inaudible run outside a reply is held.
        self._in_speech = False
        self._last_forward_mono: Optional[float] = None
        # Segment boundaries follow *audible* audio only. Forwarding a natural
        # pause must not make the next word look like a new reply.
        self._last_voice_mono: Optional[float] = None
        self._t0: Optional[float] = None

    # -- state the service exposes as response_active -----------------------

    @property
    def speaking(self) -> bool:
        if self._last_forward_mono is None:
            return False
        return (self._clock() - self._last_forward_mono) < self.speech_hangover_s

    @property
    def segments(self) -> int:
        return self.metrics.segments

    @property
    def digest(self) -> str:
        """SHA-256 of every forwarded byte — identity without content."""
        return self._digest.hexdigest()

    def reset(self) -> None:
        """Drop per-reply assembly state (interruption, reconnect).

        Counters and the digest survive: they describe the session, and a
        diagnosis of the next failed turn needs them.
        """
        self._carry = b""
        self._previous_end_ms = None
        self._silence_run_ms = 0.0
        self._discard_pending(idle=False)
        self._in_speech = False
        self._last_forward_mono = None
        self._last_voice_mono = None

    # -- the one entry point -------------------------------------------------

    def feed(self, delta_b64: object, start_ms: object = None, end_ms: object = None) -> OutputChunk:
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        pcm, reason = self._decode(delta_b64)
        if reason:
            self.metrics.undecodable += 1
            return OutputChunk(reason=reason)

        self.metrics.deltas += 1
        self.metrics.bytes_in += len(pcm)
        if self.metrics.first_delta_s is None:
            self.metrics.first_delta_s = round(now - self._t0, 4)
        self.metrics.last_delta_s = round(now - self._t0, 4)

        gap_ms = self._observe_timeline(start_ms, end_ms)
        pcm = self._align(pcm)
        stats = pcm16_stats(pcm, self.sample_rate)
        self.history.append(AudioDelta(
            arrival_s=round(now - self._t0, 4),
            start_ms=start_ms if isinstance(start_ms, int) else None,
            end_ms=end_ms if isinstance(end_ms, int) else None,
            nbytes=len(pcm), rms=round(stats.rms, 1), peak=stats.peak,
            all_zero=stats.all_zero,
        ))
        if stats.peak > self.metrics.peak:
            self.metrics.peak = stats.peak
        if self.capture is not None:
            self.capture.offer(pcm, stats)

        if not pcm:
            return OutputChunk(gap_ms=gap_ms, reason="empty after alignment")

        # Inaudible audio. A pause *inside* a reply is forwarded untouched and in
        # real time, because the device's audio chain must not be starved
        # between two sentences. A run *outside* a reply is held instead and
        # discarded once it passes max_silence_ms, so idle zeros never reach the
        # device, never put it into REPLYING and never spend its playout lead
        # (see "Leading idle runs" in the module docstring). The protocol has no
        # output-audio-done event, so this threshold is also what ends a reply.
        inaudible = stats.inaudible(self.silence_peak)
        if inaudible:
            self.metrics.silent_deltas += 1
            self._silence_run_ms += stats.duration_ms
            if self._silence_run_ms > self.max_silence_ms:
                # Idle, not a pause: the reply is over. Discard the head of this
                # same run along with it — that head is the audio that used to
                # arrive at the device as a silent false start.
                self._in_speech = False
                self._suppress(stats.duration_ms)
                self._discard_pending(idle=True)
                return OutputChunk(gap_ms=gap_ms, reason="idle run exceeded max_silence_ms")
            if not self._in_speech:
                self._pending.append(pcm)
                self._pending_ms += stats.duration_ms
                self.metrics.silence_held_deltas = len(self._pending)
                return OutputChunk(gap_ms=gap_ms, reason="holding a leading pause")
            self.metrics.silence_released_deltas += 1
            self.metrics.silence_released_ms += stats.duration_ms
        else:
            self._silence_run_ms = 0.0
            self._in_speech = True
            if self._pending:
                # A short lead-in: release it byte-for-byte ahead of the first
                # word, in order, as one chunk. It is real model output and it
                # helps fill the lead it no longer steals.
                held, held_ms = self._take_pending()
                self.metrics.silence_released_deltas += len(held)
                self.metrics.silence_released_from_hold_deltas += len(held)
                self.metrics.silence_released_ms += held_ms
                self.metrics.forwarded_deltas += len(held)
                pcm = b"".join(held) + pcm

        started = self._segment_boundary(now, gap_ms, silent=inaudible)
        self._last_forward_mono = now
        self.metrics.forwarded_deltas += 1
        self.metrics.bytes_out += len(pcm)
        self._digest.update(pcm)
        self._update_ratio()
        return OutputChunk(pcm=pcm, started_segment=started,
                           audible=not inaudible, gap_ms=gap_ms)

    # -- internals -----------------------------------------------------------

    def _suppress(self, duration_ms: float, deltas: int = 1) -> None:
        self.metrics.silence_suppressed_deltas += deltas
        self.metrics.silence_suppressed_ms += duration_ms

    def _take_pending(self) -> Tuple[List[bytes], float]:
        held, held_ms = self._pending, self._pending_ms
        self._pending, self._pending_ms = [], 0.0
        self.metrics.silence_held_deltas = 0
        return held, held_ms

    def _discard_pending(self, *, idle: bool) -> None:
        """Drop held inaudible audio. Never heard, so nothing is lost."""
        held, held_ms = self._take_pending()
        if not held:
            return
        self._suppress(held_ms, deltas=len(held))
        if idle:
            self.metrics.idle_head_discarded_deltas += len(held)
            self.metrics.idle_head_discarded_ms += held_ms

    def _decode(self, delta_b64: object) -> Tuple[bytes, str]:
        if not isinstance(delta_b64, str) or not delta_b64:
            return b"", "delta missing or not a string"
        try:
            return base64.b64decode(delta_b64, validate=True), ""
        except (TypeError, ValueError, binascii.Error):
            return b"", "delta is not valid base64"

    def _align(self, pcm: bytes) -> bytes:
        """Keep 16-bit sample alignment across deltas.

        The documentation requires an even payload. If one ever is not, the
        stray byte is *carried* into the next delta instead of dropped: a
        dropped byte shifts every later sample by one and turns the rest of the
        reply into loud noise, which is the classic signature of this class of
        bug.
        """
        if self._carry:
            pcm = self._carry + pcm
            self._carry = b""
        if len(pcm) % BYTES_PER_SAMPLE:
            self.metrics.odd_length += 1
            self.metrics.carried_bytes += 1
            self._carry = pcm[-1:]
            pcm = pcm[:-1]
        return pcm

    def _observe_timeline(self, start_ms: object, end_ms: object) -> int:
        if not isinstance(start_ms, int) or not isinstance(end_ms, int):
            self.metrics.untimed_deltas += 1
            return 0
        gap = 0
        if self._previous_end_ms is not None:
            difference = start_ms - self._previous_end_ms
            if difference > 0:
                gap = difference
                self.metrics.timeline_gaps += 1
                self.metrics.timeline_gap_ms_total += difference
                self.metrics.timeline_gap_ms_max = max(
                    self.metrics.timeline_gap_ms_max, difference
                )
            elif difference < 0:
                self.metrics.timeline_overlaps += 1
        self._previous_end_ms = max(end_ms, self._previous_end_ms or end_ms)
        return gap

    def _segment_boundary(self, now: float, gap_ms: int, silent: bool) -> bool:
        if silent:
            return False
        idle = self._last_voice_mono is None or (
            (now - self._last_voice_mono) * 1000.0 >= self.segment_gap_ms
        )
        self._last_voice_mono = now
        if idle or gap_ms >= self.segment_gap_ms:
            self.metrics.segments += 1
            return True
        return False

    def _update_ratio(self) -> None:
        first, last = self.metrics.first_delta_s, self.metrics.last_delta_s
        if first is None or last is None or last - first < 0.5:
            return
        produced = self.metrics.bytes_in / (self.sample_rate * BYTES_PER_SAMPLE)
        self.metrics.realtime_ratio = round(produced / (last - first), 3)

    # -- diagnosis -----------------------------------------------------------

    def take_turn_report(self) -> Dict[str, object]:
        """Counters accumulated since the previous call: one reply's worth.

        Session totals cannot answer "was *that* reply broken?", which is the
        only question a household report ever starts from.
        """
        current = self.metrics.as_dict()
        report: Dict[str, object] = {}
        for key, value in current.items():
            previous = self._turn_mark.get(key)
            if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
                report[key] = round(value - previous, 3) if isinstance(value, float) else value - previous
            else:
                report[key] = value
        # Absolute, not differential: these describe the stream, not a count.
        report["peak"] = self.metrics.peak
        report["silence_held_deltas"] = self.metrics.silence_held_deltas
        report["realtime_ratio"] = self.metrics.realtime_ratio
        report["first_delta_s"] = self.metrics.first_delta_s
        report["last_delta_s"] = self.metrics.last_delta_s
        self._turn_mark = current
        return report

    def snapshot(self, recent: int = 12) -> Dict[str, object]:
        """Everything needed to diagnose a bad reply, with no audio content."""
        data = self.metrics.as_dict()
        data["digest"] = self.digest[:16]
        data["recent_deltas"] = [
            {"t": d.arrival_s, "start_ms": d.start_ms, "end_ms": d.end_ms,
             "bytes": d.nbytes, "rms": d.rms, "peak": d.peak, "zero": d.all_zero}
            for d in list(self.history)[-recent:]
        ]
        if self.capture is not None:
            data["capture"] = self.capture.snapshot()
        return data

    def failure_reasons(self) -> List[str]:
        """Reasons this session's output audio is *malformed*. Empty = clean.

        Deliberately excludes the real-time delivery pace: at ~1.0x that is
        normal for GPT-Live, not a fault in the stream, and the mitigation
        (a relay-side playout lead) is decided once at startup rather than
        re-reported every turn.
        """
        reasons = []
        m = self.metrics
        if m.undecodable:
            reasons.append(f"{m.undecodable} output_audio deltas could not be decoded")
        if m.odd_length:
            reasons.append(
                f"{m.odd_length} deltas had an odd byte length "
                f"({m.carried_bytes} bytes carried forward to keep 16-bit alignment)"
            )
        if m.timeline_overlaps:
            reasons.append(
                f"{m.timeline_overlaps} deltas overlapped the previous server time range"
            )
        return reasons


@dataclass
class OutputAudioCapture:
    """A bounded ring of raw model-output PCM, for format diagnosis only.

    Enabled explicitly (``live_audio_capture_ms``) and capped, because the raw
    model output *is* the assistant's voice. It is never written anywhere by
    this class: a caller asks for the bytes deliberately, and the default is
    off. The format facts a static/garbled report needs — rate, alignment,
    peak, zero runs, a hash — come from :class:`OutputAudioMetrics` instead and
    are safe to log on every turn.
    """

    limit_bytes: int
    pcm: bytearray = field(default_factory=bytearray)
    offered_bytes: int = 0
    truncated: bool = False

    def offer(self, pcm: bytes, stats: Pcm16Stats) -> None:
        self.offered_bytes += len(pcm)
        if self.limit_bytes <= 0:
            return
        room = self.limit_bytes - len(self.pcm)
        if room <= 0:
            self.truncated = True
            return
        self.pcm.extend(pcm[:room])
        if len(pcm) > room:
            self.truncated = True

    def snapshot(self) -> Dict[str, object]:
        return {
            "limit_bytes": self.limit_bytes,
            "held_bytes": len(self.pcm),
            "offered_bytes": self.offered_bytes,
            "truncated": self.truncated,
        }

    def wav(self, sample_rate: int = LIVE_SAMPLE_RATE) -> bytes:
        """The held PCM as a WAV container (for an operator-requested dump)."""
        pcm = bytes(self.pcm)
        header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
        header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        header += b"data" + struct.pack("<I", len(pcm))
        return header + pcm
