"""Per-turn latency timeline, correlated across the device and the add-on.

One ``Turn`` exists per user turn (a wake, a button press, or speech in a
follow-up window). Stages are stamped where they happen:

  wake_received        serializer got {"type":"wake"} (device turn id + metadata)
  first_audio_frame    first mic frame after the wake
  speech_started       OpenAI input_audio_buffer.speech_started
  speech_stopped       OpenAI input_audio_buffer.speech_stopped
  response_created     OpenAI response.created
  first_model_audio    first response.output_audio.delta
  first_audio_sent     first reply audio handed to the device socket
  bot_started/stopped  output transport speaking frames
  completed            turn ended (idle, error, interrupt, flag, superseded)

``vad_endpoint_delay_ms`` is measured from OpenAI's own ``audio_end_ms``: the
audio already streamed past the real end of speech when the server VAD
decided the user had finished. It is the objective end-of-turn cost that the
``vad_eagerness`` setting trades against cut-offs.

The device reports what only it can see (wake fire -> mic open, reply audio
received -> first sample accepted by the speaker) in a ``turn_metrics``
message keyed by the same turn id; the add-on merges it before emitting one
structured log line per turn and updating rolling p50/p90 statistics.

Privacy: summaries carry timings, tool names, device/turn ids, wake model
metadata and an outcome — never audio, transcripts or speaker identity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

STAGES = (
    "wake_received",
    "first_audio_frame",
    "speech_started",
    "speech_stopped",
    "response_created",
    "first_model_audio",
    "first_audio_sent",
    "bot_started",
    "bot_stopped",
    "completed",
)

# (name, from-stage, to-stage)
INTERVALS = (
    ("wake_to_first_frame_ms", "wake_received", "first_audio_frame"),
    ("first_frame_to_speech_ms", "first_audio_frame", "speech_started"),
    ("speech_ms", "speech_started", "speech_stopped"),
    ("speech_end_to_response_ms", "speech_stopped", "response_created"),
    ("speech_end_to_first_model_audio_ms", "speech_stopped", "first_model_audio"),
    ("model_audio_to_sent_ms", "first_model_audio", "first_audio_sent"),
    ("speech_end_to_first_audio_sent_ms", "speech_stopped", "first_audio_sent"),
    ("wake_to_first_audio_sent_ms", "wake_received", "first_audio_sent"),
    ("reply_ms", "bot_started", "bot_stopped"),
)

# Integer metrics the firmware may report in turn_metrics. Anything else is
# ignored, so a misbehaving client cannot inject free text into the summary.
DEVICE_FIELDS = (
    "fire_to_mic_ms",
    "wake_to_listening_ms",
    "listening_to_thinking_ms",
    "thinking_to_first_audio_ms",
    "first_audio_to_audible_ms",
    "audible_to_drained_ms",
    "total_ms",
    "ws_gaps",
    "ws_gap_max_ms",
    "underrun",
    "keepalive_frames",
    "seq",
)

STAT_KEYS = (
    "speech_end_to_first_audio_sent_ms",
    "speech_end_to_first_model_audio_ms",
    "vad_endpoint_delay_ms",
    "true_speech_end_to_first_audio_sent_ms",
    "wake_to_first_frame_ms",
    "device.fire_to_mic_ms",
    "device.first_audio_to_audible_ms",
)

_TURN_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_turn_id(raw: object) -> str:
    return _TURN_ID.sub("", str(raw or ""))[:40]


@dataclass
class Turn:
    turn_id: str
    device_id: str
    source: str
    started: float
    stamps: Dict[str, float] = field(default_factory=dict)
    device: Dict[str, int] = field(default_factory=dict)
    meta: Dict[str, object] = field(default_factory=dict)
    tools: List[dict] = field(default_factory=list)
    outcome: str = ""
    vad_endpoint_delay_ms: Optional[int] = None
    speech_audio_ms: Optional[int] = None
    emitted: bool = False

    @property
    def done(self) -> bool:
        return "completed" in self.stamps


class LatencyStats:
    """Rolling p50/p90 over the most recent turns (numbers only)."""

    def __init__(self, window: int = 50):
        self._values: Dict[str, Deque[float]] = {}
        self._window = window

    def add(self, key: str, value: Optional[float]) -> None:
        if value is None:
            return
        self._values.setdefault(key, deque(maxlen=self._window)).append(float(value))

    @staticmethod
    def _pct(sorted_values: List[float], q: float) -> float:
        if not sorted_values:
            return 0.0
        # Nearest-rank percentile: deterministic and easy to reason about.
        rank = max(1, int(round(q * len(sorted_values) + 0.5 - 1e-9)))
        return sorted_values[min(rank, len(sorted_values)) - 1]

    def p50(self, key: str, min_samples: int = 5) -> Optional[float]:
        values = self._values.get(key)
        if not values or len(values) < min_samples:
            return None
        return self._pct(sorted(values), 0.5)

    def snapshot(self) -> Dict[str, dict]:
        out = {}
        for key, values in self._values.items():
            ordered = sorted(values)
            out[key] = {
                "n": len(ordered),
                "p50": round(self._pct(ordered, 0.5), 1),
                "p90": round(self._pct(ordered, 0.9), 1),
            }
        return out


class TurnTimeline:
    """Owns the turns of ONE device connection."""

    DEVICE_METRICS_GRACE_S = 4.0

    def __init__(
        self,
        device_id: str,
        publish: Optional[Callable[[dict, dict], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        stats: Optional[LatencyStats] = None,
        history: int = 20,
    ):
        self.device_id = device_id
        self._publish = publish
        self._clock = clock
        self.stats = stats or LatencyStats()
        self.current: Optional[Turn] = None
        self.history: Deque[Turn] = deque(maxlen=history)
        self._follow_ups = 0
        self._emit_handles: Dict[str, asyncio.TimerHandle] = {}
        # Monotonic counters used by the confirmation gate: a confirmation is
        # valid only after a NEW user utterance and within the same wake.
        self.user_turn_seq = 0
        self.wake_seq = 0

    # -- lifecycle ----------------------------------------------------------
    def begin(self, turn_id: Optional[str] = None, source: str = "wake_word",
              meta: Optional[dict] = None) -> Turn:
        if self.current is not None and not self.current.done:
            self.finish("superseded")
        turn_id = sanitize_turn_id(turn_id) or uuid.uuid4().hex[:12]
        self._follow_ups = 0
        turn = Turn(turn_id=turn_id, device_id=self.device_id, source=source,
                    started=self._clock(), meta=dict(meta or {}))
        if source in ("wake_word", "button", "wake"):
            turn.stamps["wake_received"] = turn.started
            self.wake_seq += 1
        self.current = turn
        return turn

    def ensure_active(self, source: str = "follow_up") -> Turn:
        """Return the live turn, starting a follow-up turn if the last one ended."""
        if self.current is not None and not self.current.done:
            return self.current
        base = self.current.turn_id.split(".f")[0] if self.current else ""
        self._follow_ups += 1
        follow_ups = self._follow_ups
        turn_id = f"{base}.f{follow_ups}" if base else None
        meta = dict(self.current.meta) if self.current else {}
        turn = self.begin(turn_id=turn_id, source=source, meta=meta)
        self._follow_ups = follow_ups
        return turn

    def mark(self, stage: str, at: Optional[float] = None, overwrite: bool = False) -> None:
        turn = self.current
        if turn is None or turn.done:
            return
        if overwrite or stage not in turn.stamps:
            turn.stamps[stage] = self._clock() if at is None else at

    def note_speech_stopped(self, audio_end_ms: Optional[int], audio_start_ms: Optional[int],
                            appended_audio_ms: Optional[float]) -> None:
        """Stamp speech end and derive the VAD endpointing delay."""
        turn = self.ensure_active()
        self.user_turn_seq += 1
        turn.stamps["speech_stopped"] = self._clock()
        if audio_end_ms is not None and appended_audio_ms is not None:
            delay = int(round(appended_audio_ms - audio_end_ms))
            if 0 <= delay < 60000:
                turn.vad_endpoint_delay_ms = delay
        if audio_end_ms is not None and audio_start_ms is not None:
            turn.speech_audio_ms = max(0, int(audio_end_ms - audio_start_ms))

    def tool_started(self, name: str) -> Optional[dict]:
        turn = self.current
        if turn is None:
            return None
        record = {"name": str(name)[:64], "t0": self._clock()}
        turn.tools.append(record)
        return record

    def tool_finished(self, record: Optional[dict], ok: bool = True) -> None:
        if not record:
            return
        record["ms"] = int(round((self._clock() - record.pop("t0")) * 1000))
        record["ok"] = bool(ok)
        self.stats.add(f"tool.{record['name']}", record["ms"])

    def finish(self, outcome: str) -> Optional[Turn]:
        turn = self.current
        if turn is None or turn.done:
            return None
        turn.stamps["completed"] = self._clock()
        turn.outcome = turn.outcome or outcome
        self.history.append(turn)
        self._schedule_emit(turn)
        return turn

    def set_outcome(self, turn_id: str, outcome: str) -> None:
        """Late outcome change (e.g. a false-wake flag after the turn ended)."""
        turn = self.find(turn_id)
        if turn is not None:
            turn.outcome = outcome

    def find(self, turn_id: str) -> Optional[Turn]:
        turn_id = sanitize_turn_id(turn_id)
        candidates = list(self.history)
        if self.current is not None:
            candidates.append(self.current)
        for turn in reversed(candidates):
            if turn.turn_id == turn_id:
                return turn
        return None

    def merge_device_metrics(self, turn_id: str, metrics: dict) -> None:
        """Attach the firmware's turn_metrics and emit if the turn is done."""
        base = sanitize_turn_id(turn_id)
        candidates = list(self.history)
        if self.current is not None and self.current not in candidates:
            candidates.append(self.current)
        target = None
        seq = metrics.get("seq")
        for turn in reversed(candidates):
            if turn.turn_id == base or turn.turn_id.split(".f")[0] == base:
                if seq is None or turn.turn_id == base or turn.turn_id.endswith(f".f{seq}"):
                    target = turn
                    break
                target = target or turn
        if target is None:
            return
        for key in DEVICE_FIELDS:
            value = metrics.get(key)
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, (int, float)) and 0 <= value < 3_600_000:
                target.device[key] = int(value)
        if target.done and not target.emitted:
            handle = self._emit_handles.pop(target.turn_id, None)
            if handle is not None:
                handle.cancel()
            self._emit(target)

    # -- summaries ----------------------------------------------------------
    def summary(self, turn: Turn) -> dict:
        intervals = {}
        for name, start, end in INTERVALS:
            if start in turn.stamps and end in turn.stamps:
                value = (turn.stamps[end] - turn.stamps[start]) * 1000
                if value >= 0:
                    intervals[name] = int(round(value))
        if turn.vad_endpoint_delay_ms is not None:
            intervals["vad_endpoint_delay_ms"] = turn.vad_endpoint_delay_ms
            sent = intervals.get("speech_end_to_first_audio_sent_ms")
            if sent is not None:
                intervals["true_speech_end_to_first_audio_sent_ms"] = sent + turn.vad_endpoint_delay_ms
        if turn.speech_audio_ms is not None:
            intervals["speech_audio_ms"] = turn.speech_audio_ms
        tools = [
            {k: v for k, v in record.items() if k in ("name", "ms", "ok")}
            for record in turn.tools
        ]
        tool_ms = sum(t.get("ms", 0) for t in tools)
        if tools:
            intervals["tool_ms_total"] = tool_ms
        return {
            "turn_id": turn.turn_id,
            "device_id": turn.device_id,
            "source": turn.source,
            "outcome": turn.outcome,
            "intervals": intervals,
            "speech_end_to_first_audio_sent_ms": intervals.get("speech_end_to_first_audio_sent_ms"),
            "device": dict(turn.device),
            "tools": tools,
            "wake": {k: turn.meta[k] for k in ("model", "model_sha", "cutoff", "window", "tier", "fw")
                     if k in turn.meta},
            "complete": all(s in turn.stamps for s in (
                "speech_stopped", "first_model_audio", "first_audio_sent"))
            if turn.outcome == "replied" else None,
        }

    def _schedule_emit(self, turn: Turn) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._emit(turn)
            return
        self._emit_handles[turn.turn_id] = loop.call_later(
            self.DEVICE_METRICS_GRACE_S, self._emit, turn
        )

    def _emit(self, turn: Turn) -> None:
        self._emit_handles.pop(turn.turn_id, None)
        if turn.emitted:
            return
        turn.emitted = True
        summary = self.summary(turn)
        for key in STAT_KEYS:
            if key.startswith("device."):
                self.stats.add(key, summary["device"].get(key.split(".", 1)[1]))
            elif turn.outcome == "replied":
                self.stats.add(key, summary["intervals"].get(key))
        logger.info("⏱️ turn " + json.dumps(summary, sort_keys=True, separators=(",", ":")))
        if self._publish is not None:
            try:
                self._publish(summary, self.stats.snapshot())
            except Exception as e:
                logger.debug(f"latency publish failed: {e!r}")

    def close(self) -> None:
        """Emit any summaries still waiting for device metrics, then stop."""
        pending = [t for t in self.history if t.done and not t.emitted]
        for handle in self._emit_handles.values():
            handle.cancel()
        self._emit_handles.clear()
        for turn in pending:
            self._emit(turn)
