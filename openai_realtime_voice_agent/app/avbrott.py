"""A barge-in must be speech, not room sound (raawr kitchen, 2026-10-10).

While Björn is speaking the microphone stays open, so Henrik can answer "Något mer?" before he has finished. But a TV,
a radio or a person two rooms away must never cut Björn's answer. When the local detector says "start" during a reply,
the speech is collected as a CANDIDATE instead of becoming a turn at once. It becomes a turn (and so an interruption)
only when it has lasted `min_ms` and is loud enough, `min_dbfs` RMS over the candidate. If the speech ends first, or
stays too quiet, it is thrown away: no turn, no interruption, Björn goes on. Every decision is one journal line, with
the length and the level, so the two numbers can be tuned from real rooms instead of guessed.
"""
import logging
import math
import os

logger = logging.getLogger(__name__)


def _env_float(namn: str, standard: float) -> float:
    try:
        return float(os.environ.get(namn, standard))
    except ValueError:
        return standard


def min_ms() -> float:
    return _env_float("AVBROTT_MIN_MS", 500.0)


def min_dbfs() -> float:
    return _env_float("AVBROTT_MIN_DBFS", -40.0)


def paa() -> bool:
    return os.environ.get("AVBROTT_TROSKEL", "1") != "0"  # 0 = every start is a barge-in, as before


def dbfs(pcm16: bytes) -> float:
    """RMS level of 16-bit mono PCM in dB below full scale (-96 for silence)."""
    n = len(pcm16) // 2
    if n == 0:
        return -96.0
    import numpy as np

    x = np.frombuffer(pcm16[: n * 2], dtype=np.int16).astype(np.float64) / 32768.0
    rms = math.sqrt(float((x * x).mean()))
    return -96.0 if rms <= 0 else max(-96.0, 20.0 * math.log10(rms))


class Kandidat:
    """Speech that started while Björn speaks: all audio kept (for the turn, if it is one), speech time and level
    counted over the frames the detector called speech (not the silence it waits out before it says "end")."""

    def __init__(self, sample_rate: int = 16000):
        self.rate = sample_rate
        self.ljud = bytearray()
        self.tal = bytearray()

    def lagg_till(self, pcm16: bytes, tal: bool = True) -> None:
        self.ljud += pcm16
        if tal:
            self.tal += pcm16

    @property
    def tal_ms(self) -> float:
        return len(self.tal) / 2 / self.rate * 1000.0

    def niva(self) -> float:
        return dbfs(bytes(self.tal))

    def godkand(self) -> bool:
        return self.tal_ms >= min_ms() and self.niva() >= min_dbfs()

    def rad(self) -> str:
        return f"{self.tal_ms:.0f} ms tal, {self.niva():.1f} dBFS (krav {min_ms():.0f} ms, {min_dbfs():.0f} dBFS)"
