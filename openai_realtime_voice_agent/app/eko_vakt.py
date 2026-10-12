"""Full duplex, layer 2 (raawr US-060, D-22): is what the mic heard while Björn spoke his own voice?

With the mic open during a reply, the XMOS echo canceller is the main guard. This is the net under it: speech that starts
while he is replying, or just after, is written out locally (bana 0's speech-to-text) and compared with what HE said
(the engine's own transcript of the turn). Words that are his = echo, ignored. Anything else = the person talking: an interruption.

OFF unless BARGE_IN=1. ANTI_EKO=0 turns only this layer off (to measure the XMOS layer alone: US-060 AC-1).
"""
import difflib
import os
import re
import time

MIN_ORD = 1             # fewer words than this is not speech
LIKA = 0.75             # two words this alike are the same word (the local STT is not exact on his voice)
LANGT = 7               # words shorter than this must match exactly: 'vänta' is not 'väntar', 'sluta' is not 'slutar', 'ja' is not 'jag' (A on #65)
ANDEL = 0.6             # this share of the heard words found among his = echo


NIVA_DBFS = -36.0       # speech quieter than this (loudest 0.25 s) over his reply is room noise, not a person (measured 2026-10-12: echo -41..-52, Henrik -12..-20)

# What the local speech-to-text writes when it is given noise (seen live on the echo capture): never a person.
LUDD = ("tack till elever och personal", "tack för att du tittade", "tack för att ni tittade", "undertexter")


def niva_dbfs() -> float:
    try:
        return float(os.environ.get("EKO_NIVA_DBFS", str(NIVA_DBFS)))
    except ValueError:
        return NIVA_DBFS


def paa() -> bool:
    return os.environ.get("BARGE_IN", "0") == "1"


def nat_paa() -> bool:
    return os.environ.get("ANTI_EKO", "1") != "0"


def svans_s() -> float:
    """Seconds after his reply ended during which the room still carries it."""
    try:
        return float(os.environ.get("EKO_SVANS_MS", "1500")) / 1000
    except ValueError:
        return 1.5


def ord_(text: str) -> list:
    return re.findall(r"[a-zåäöé0-9]+", (text or "").lower())


def ar_eko(hort: "str | None", hans: str) -> bool:
    """True when `hort` (the local STT of what the mic heard) is his own words, or nothing a person said."""
    h = ord_(hort)
    if len(h) < MIN_ORD:
        return True  # nothing intelligible: not an interruption
    sina = set(ord_(hans))
    if not sina:
        return False  # he has said nothing yet: whatever is heard is a person
    traffar = sum(1 for w in h if w in sina or (len(w) >= LANGT and difflib.get_close_matches(w, sina, n=1, cutoff=LIKA)))
    return traffar / len(h) >= ANDEL


def dom(hort: "str | None", ok: bool, hans: str) -> bool:
    """The net's verdict: True = ignore (his own words, or clearly no words). In doubt it is the person: a speech-to-text
    that failed or timed out (ok False) must never throw a real interruption away (A on #65)."""
    if not ok:
        return False
    if any(l in (hort or "").lower() for l in LUDD):
        return True  # what it writes of noise
    return ar_eko(hort, hans)


class Svar:
    """What he has said: the current reply and the last two, as one bag of words."""

    def __init__(self):
        self.nu = ""
        self.tidigare = []

    def mata(self, text: str) -> None:
        self.nu += text or ""

    def klar(self) -> None:
        if self.nu.strip():
            self.tidigare = (self.tidigare + [self.nu])[-2:]
        self.nu = ""

    def text(self) -> str:
        return " ".join(self.tidigare + [self.nu])


def i_svans(idle_sedan: float, nu: "float | None" = None) -> bool:
    """`idle_sedan` is when the phase last turned idle (monotonic); False when it never has."""
    return bool(idle_sedan) and (nu if nu is not None else time.monotonic()) - idle_sedan <= svans_s()
