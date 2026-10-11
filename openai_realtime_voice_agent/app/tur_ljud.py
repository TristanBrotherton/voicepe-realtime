"""Keep each turn's mic audio as its own wav, to compare ears on real speech (raawr D-97).

OFF unless TUR_LJUD_SPARA=1. At most TUR_LJUD_MAX files (default 40) sit in TUR_LJUD_KATALOG
(default /data/tur_ljud); when the folder is full nothing more is saved and the journal says so once.
The audio is bana 0's copy of the turn (16 kHz mono, about 0.3 s more than the engine holds).
It is Henrik's voice: turn the flag off and delete the folder when the comparison is done.
"""
import itertools
import logging
import os
import time
import wave

logger = logging.getLogger(__name__)
_full_sagd = False
_nr = itertools.count(1)


def paa() -> bool:
    return os.environ.get("TUR_LJUD_SPARA", "0") == "1"


def katalog() -> str:
    return os.environ.get("TUR_LJUD_KATALOG", "/data/tur_ljud")


def tak() -> int:
    try:
        return max(0, int(os.environ.get("TUR_LJUD_MAX", "40")))
    except ValueError:
        return 40


def spara(pcm16k: bytes, rum: str) -> "str | None":
    """Save one turn. The path, or None when off, empty, full or failed. Never raises."""
    global _full_sagd
    if not paa() or not pcm16k:
        return None
    try:
        dir_ = katalog()
        os.makedirs(dir_, exist_ok=True)
        if sum(f.endswith(".wav") for f in os.listdir(dir_)) >= tak():
            if not _full_sagd:
                _full_sagd = True
                logger.warning(f"🎙️ tur_ljud: {dir_} holds {tak()} files, saving no more")
            return None
        path = os.path.join(dir_, f"tur_{rum}_{time.strftime('%Y%m%d_%H%M%S')}_{next(_nr):04d}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm16k)
        logger.info(f"🎙️ tur_ljud: saved {os.path.basename(path)} ({len(pcm16k) / 32000:.1f} s)")
        return path
    except Exception as e:  # keeping a file must never break a turn
        logger.warning(f"🎙️ tur_ljud: not saved: {e!r}")
        return None
