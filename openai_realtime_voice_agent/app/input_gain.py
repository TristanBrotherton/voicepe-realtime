"""Bring a quiet voice up to the level GPT-Live transcribes.

GPT-Live's input transcription judges speech against the level of what it has
heard recently and against an absolute floor. Replaying the device's own audio
into fresh sessions (2026-10-23, about 40 runs) showed that a request at
500-1800 int16 RMS at the device mic (a loudspeaker across the room, a soft
speaker, a person a few metres away) is answered but never transcribed; the
same request four times louder is transcribed every time. A person near the
device measures 3700-8900.

So the mic stream is normalised before it is appended: a slow gain brings
speech-level blocks toward ``TARGET_RMS``, never attenuates, never exceeds
``max_gain``, and follows a peak-hold of the recent level so a voice that gets
louder is not pushed into clipping. Twenty-millisecond blocks, the gain ramped
across each block so there are no steps. Room noise rises by the same factor
(a quiet room at 200-450 RMS becomes at most 800-1800), still well below
speech after the gain. ``max_gain`` 1.0 switches it off.
"""
from __future__ import annotations

import numpy as np

TARGET_RMS = 4000.0
DEFAULT_MAX_GAIN = 4.0
BLOCK_MS = 20
# After speech the level estimate decays toward quiet over about this long, so
# the gain comes back up gradually between utterances instead of jumping.
LEVEL_HALF_LIFE_S = 1.5


class SpeechLevelGain:
    """Automatic gain for PCM16 mono frames of any size (``process``)."""

    def __init__(self, *, sample_rate: int = 24000, target_rms: float = TARGET_RMS,
                 max_gain: float = DEFAULT_MAX_GAIN, block_ms: int = BLOCK_MS,
                 half_life_s: float = LEVEL_HALF_LIFE_S):
        self.target_rms = float(target_rms)
        self.max_gain = max(1.0, float(max_gain))
        self.block = max(1, int(sample_rate * block_ms / 1000))
        self.decay = 0.5 ** ((block_ms / 1000.0) / half_life_s)
        self.level = 0.0       # peak-hold of recent block RMS, decaying
        self.gain = 1.0        # gain at the end of the last block
        self._carry = b""
        self.blocks = 0
        self.boosted_blocks = 0
        self.max_gain_applied = 1.0

    @property
    def enabled(self) -> bool:
        return self.max_gain > 1.0

    def reset_stats(self) -> None:
        self.blocks = 0
        self.boosted_blocks = 0
        self.max_gain_applied = 1.0

    def summary(self) -> str:
        return (f"{self.boosted_blocks}/{self.blocks} blocks boosted, "
                f"up to {self.max_gain_applied:.1f}x (cap {self.max_gain:.1f}x)")

    def process(self, pcm: bytes) -> bytes:
        """Return the gained audio for whole blocks; a partial block is carried
        into the next call, so the output may be shorter (or longer) than this
        input but nothing is lost or reordered."""
        if not self.enabled:
            return pcm
        data = self._carry + bytes(pcm)
        step = self.block * 2
        whole = len(data) - len(data) % step
        self._carry = data[whole:]
        if whole <= 0:
            return b""
        samples = np.frombuffer(data[:whole], dtype=np.int16).astype(np.float32)
        out = np.empty_like(samples)
        n = self.block
        for start in range(0, len(samples), n):
            block = samples[start:start + n]
            rms = float(np.sqrt(np.mean(block * block))) if n else 0.0
            # Judged with this block included (instant attack): a loud block
            # is never boosted by the gain a quiet one earned.
            self.level = max(rms, self.level * self.decay)
            target = self.target_rms / max(self.level, 1.0)
            gain = min(self.max_gain, max(1.0, target))
            if gain != self.gain:
                ramp = np.linspace(self.gain, gain, n, endpoint=False, dtype=np.float32)
                out[start:start + n] = block * ramp
            else:
                out[start:start + n] = block * gain
            self.gain = gain
            self.blocks += 1
            if gain > 1.0:
                self.boosted_blocks += 1
                if gain > self.max_gain_applied:
                    self.max_gain_applied = gain
        np.clip(out, -32768.0, 32767.0, out=out)
        return out.astype(np.int16).tobytes()
