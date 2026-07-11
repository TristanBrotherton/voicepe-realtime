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
import os
import time
import wave
from typing import Awaitable, Callable, Optional

from .speaker_gender import classify_gender
from . import voiceprint

logger = logging.getLogger(__name__)

CAPTURE_SECONDS = 3.0
# Partial captures at least this long are still classified + dumped when a
# session ends before the full window fills — short false wakes are exactly
# the audio we want to harvest for retraining, and they were being lost.
MIN_PARTIAL_SECONDS = 1.0
PROBE_DUMP_DIR = "/share/voice-probes"  # persistent across add-on rebuilds
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
        # Finalize a partial capture if the session ends before the window
        # fills (observed: short false wakes left no capture at all).
        try:
            loop = asyncio.get_running_loop()
            gen = self._capture_gen = getattr(self, "_capture_gen", 0) + 1
            def _later():
                if self._capturing and gen == self._capture_gen:
                    self.finalize_partial()
            loop.call_later(4.0, _later)
        except RuntimeError:
            pass

    def finalize_partial(self) -> None:
        """Classify+dump whatever audio exists if it's at least MIN_PARTIAL_SECONDS."""
        if not self._capturing or self._classifying:
            return
        need = int(MIN_PARTIAL_SECONDS * SAMPLE_RATE * 2)
        if len(self._buf) < need:
            self._capturing = False
            self._buf = bytearray()
            return
        self._capturing = False
        self._classifying = True
        data = bytes(self._buf)
        self._buf = bytearray()
        try:
            asyncio.get_running_loop().create_task(self._classify(data))
        except RuntimeError:
            self._classifying = False

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

    def _identify(self, data: bytes):
        """Voice-print first (verified identity), pitch heuristic fallback.

        Returns (label, name, f0_or_score, voiced_or_level)."""
        try:
            level, name, score = voiceprint.identify(data)
        except Exception as e:
            logger.warning(f"⚠️ voiceprint identify failed: {e!r}")
            level, name, score = "unavailable", None, 0.0
        if level == "match":
            label = "male" if name == self.male_name else ("female" if name == self.female_name else "match")
            return label, name, score, "voiceprint"
        if level == "unknown":
            return "unknown", None, score, "voiceprint"
        if level == "uncertain":
            return "uncertain", name, score, "voiceprint"
        # unavailable -> pitch heuristic
        label, f0, voiced = classify_gender(data)
        return label, self.name_for(label), f0, "pitch"

    async def _classify(self, data: bytes) -> None:
        try:
            # Debug: when add-on recording is enabled, dump the raw capture so
            # thresholds can be tuned offline against real device audio.
            if os.environ.get("ENABLE_RECORDING", "false").strip().lower() == "true":
                try:
                    os.makedirs(PROBE_DUMP_DIR, exist_ok=True)
                    path = f"{PROBE_DUMP_DIR}/probe_{time.strftime('%Y%m%d_%H%M%S')}.wav"
                    with wave.open(path, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(16000)
                        w.writeframes(data)
                    logger.info(f"🎙️ speaker probe capture saved: {path}")
                    # Retention: cap the probe archive at the newest 500 files
                    # (~50 MB) so harvesting can run indefinitely without hygiene.
                    try:
                        files = sorted(
                            f for f in os.listdir(PROBE_DUMP_DIR) if f.endswith(".wav")
                        )
                        for stale in files[:-500]:
                            os.remove(os.path.join(PROBE_DUMP_DIR, stale))
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"⚠️ probe capture dump failed: {e!r}")
            label, name, metric, method = await asyncio.to_thread(self._identify, data)
            self.current_label = label
            self.current_f0 = metric
            self._verdict_at = time.monotonic()
            logger.info(
                f"🗣️ speaker probe [{method}]: {label}"
                f"{f' → {name}' if name else ''} (score={metric:.2f})"
            )
            try:
                from .ha_sensors import PUBLISHER
                await PUBLISHER.speaker(label, name, metric, method)
            except Exception:
                pass
            if self.on_verdict is not None:
                await self.on_verdict(label, name, metric)
        except Exception as e:
            logger.warning(f"⚠️ speaker probe failed (turn continues without it): {e!r}")
        finally:
            self._classifying = False


def verdict_text(probe: "SpeakerProbe", label: str, name: Optional[str], f0: float) -> str:
    """The system-item text injected into the Realtime session."""
    if label == "unknown":
        return (
            "[voice check] The current speaker's voice does not match any enrolled "
            "household member — likely a guest. Stay neutral and courteous: no "
            "names, no sir/ma'am, and do not reference household-specific details "
            "unprompted."
        )
    if name and label in ("male", "female", "match"):
        return (
            f"[voice check] The current speaker's voice matches {name}. "
            f"Address them accordingly."
        )
    other = " or ".join(n for n in (probe.male_name, probe.female_name) if n)
    return (
        f"[voice check] The current speaker's voice was not confidently matched "
        f"(possibly {other}, possibly someone else). Stay neutral: no names, no sir/ma'am."
    )
