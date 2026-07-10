"""Per-wake speaker context: voice-type (male/female) detection + shared state.

v1 of speaker identification for a two-person household with one male and one
female voice: on every wake, capture the first ~2.5 s of command audio, run the
pitch-based gender classifier (speaker_gender.py, pure numpy, off the event
loop), and hand the verdict to a callback that injects it into the OpenAI
Realtime session as a system conversation item. The current verdict is also
kept as module-visible state so tool gating (main.py register_function) can
enforce speaker-restricted tools no matter what the model tries.

Deliberately NOT biometric identity: a male guest classifies as the male
resident. Good enough for name-aware conversation and convenience gating;
upgrade path is swapping the classifier for enrolled voice prints without
touching the plumbing here.

Enabled only when both speaker names are configured (empty names = feature off,
zero overhead beyond an attribute check per audio frame).
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from .speaker_gender import classify_gender

logger = logging.getLogger(__name__)

CAPTURE_SECONDS = 2.5
SAMPLE_RATE = 16000
CAPTURE_BYTES = int(CAPTURE_SECONDS * SAMPLE_RATE * 2)  # PCM16 mono
# A verdict older than this is stale (device asleep between turns); the gate
# then fails closed ("uncertain") rather than trusting yesterday's voice.
VERDICT_TTL_SECONDS = 120.0


class SpeakerProbe:
    """Captures post-wake audio and classifies the speaker's voice type."""

    def __init__(self, male_name: str, female_name: str):
        self.male_name = male_name
        self.female_name = female_name
        self._buf = bytearray()
        self._capturing = False
        self._classifying = False
        self.current_label: str = "unknown"     # male / female / uncertain / unknown
        self.current_f0: float = 0.0
        self._verdict_at: float = 0.0
        # async callback(label, name_or_None, f0) — set by websocket_handler
        self.on_verdict: Optional[Callable[[str, Optional[str], float], Awaitable[None]]] = None

    @property
    def enabled(self) -> bool:
        return bool(self.male_name or self.female_name)

    def name_for(self, label: str) -> Optional[str]:
        if label == "male":
            return self.male_name or None
        if label == "female":
            return self.female_name or None
        return None

    def gate_speaker(self) -> str:
        """Label for tool gating: expires stale verdicts (fails closed)."""
        if time.monotonic() - self._verdict_at > VERDICT_TTL_SECONDS:
            return "unknown"
        return self.current_label

    def start_capture(self) -> None:
        """Called on every device wake: begin a fresh capture window."""
        if not self.enabled:
            return
        self._buf = bytearray()
        self._capturing = True

    def feed(self, pcm: bytes) -> None:
        """Called from the serializer for every inbound audio frame. O(1)-ish."""
        if not self._capturing:
            return
        self._buf += pcm
        if len(self._buf) >= CAPTURE_BYTES and not self._classifying:
            self._capturing = False
            self._classifying = True
            data = bytes(self._buf[:CAPTURE_BYTES])
            self._buf = bytearray()
            try:
                asyncio.get_running_loop().create_task(self._classify(data))
            except RuntimeError:
                # no running loop (shouldn't happen in the transport path)
                self._classifying = False

    async def _classify(self, data: bytes) -> None:
        try:
            label, f0, voiced = await asyncio.to_thread(classify_gender, data)
            self.current_label = label
            self.current_f0 = f0
            self._verdict_at = time.monotonic()
            name = self.name_for(label)
            logger.info(
                f"🗣️ speaker probe: {label}"
                f"{f' → {name}' if name else ''} (median_f0={f0:.0f}Hz, voiced={voiced})"
            )
            if self.on_verdict is not None:
                await self.on_verdict(label, name, f0)
        except Exception as e:
            logger.warning(f"⚠️ speaker probe failed (turn continues without it): {e!r}")
        finally:
            self._classifying = False


def verdict_text(probe: "SpeakerProbe", label: str, name: Optional[str], f0: float) -> str:
    """The system-item text injected into the Realtime session."""
    if name:
        return (
            f"[voice check] The current speaker's voice matches {name} "
            f"({'male' if label == 'male' else 'female'} voice; heuristic, not verified). "
            f"Address them accordingly."
        )
    other = " or ".join(n for n in (probe.male_name, probe.female_name) if n)
    return (
        f"[voice check] The current speaker's voice was not confidently matched "
        f"(possibly {other}, possibly someone else). Stay neutral: no names, no sir/ma'am."
    )
