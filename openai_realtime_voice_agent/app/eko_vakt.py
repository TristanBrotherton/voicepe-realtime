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
LAGST_ORD = 3           # fewer non-matching words than this over his reply count as echo unless one is a stop word
STOPPORD = {"vänta", "stopp", "stop", "sluta", "tyst", "nej", "björn", "hallå", "paus", "avbryt", "tack"}
LIKNAR_MIN = 3          # liknar_svar needs this many words: "köket" / "tänd köket" answering his own question is never an echo (A on #70)
ANDEL = 0.5             # this share of the heard words found among his = echo


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


ORD_PER_S = 1.0         # at least this many of his words per second of reply audio, once the transcript can have caught up
SVAR_UNDANTAG_S = 2.0   # the transcript may trail the audio by about this long


def utskrift_efter(nuvarande_ord: int, svar_sek: float) -> bool:
    """His own transcript of THIS reply has not caught up with the audio already sent: what the mic hears over it is far
    more likely to be him than a person. (Kontoret 2026-10-12: 'Vilken dörr.' heard over a reply whose text had not come.)"""
    if svar_sek <= 0.3:
        return False
    return nuvarande_ord == 0 or nuvarande_ord < ORD_PER_S * max(0.0, svar_sek - SVAR_UNDANTAG_S)


def dom(hort: "str | None", ok: bool, hans: str) -> bool:
    """The net's verdict: True = ignore (his own words, or clearly no words). In doubt it is the person: a speech-to-text
    that failed or timed out (ok False) must never throw a real interruption away (A on #65)."""
    if not ok:
        return False
    if any(l in (hort or "").lower() for l in LUDD):
        return True  # what it writes of noise
    if ar_eko(hort, hans):
        return True
    # A real interruption over his reply is a stop word or a sentence. One or two odd words ('Fåla.') are what the
    # local STT writes of his own voice coming back (Kontoret 2026-10-12 live test): not allowed to cut him.
    h = ord_(hort)
    return len(h) < LAGST_ORD and not any(w in STOPPORD for w in h)


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

    def nu_ord(self) -> int:
        return len(ord_(self.nu))


def i_svans(idle_sedan: float, nu: "float | None" = None) -> bool:
    """`idle_sedan` is when the phase last turned idle (monotonic); False when it never has."""
    return bool(idle_sedan) and (nu if nu is not None else time.monotonic()) - idle_sedan <= svans_s()


TOOL_VAKT_S = float(os.environ.get("TOOL_VAKT_S", "8"))


def liknar_svar(turtext: "str | None", hans: str) -> bool:
    """The turn's own words are (mostly) what he said himself a moment ago: his voice came back through the mic.
    Needs MIN_ORD words, so a short real command ("tänd lampan") is never taken for an echo."""
    h = ord_(turtext)
    sina = set(ord_(hans))
    if len(h) < LIKNAR_MIN or not sina:
        return False
    traffar = sum(1 for w in h if w in sina or (len(w) >= LANGT and difflib.get_close_matches(w, sina, n=1, cutoff=LIKA)))
    return traffar / len(h) >= ANDEL


def vakt_verktyg(turtext: "str | None", hans: str, sedan_barge_s: "float | None", x_s: "float | None" = None) -> "str | None":
    """Why a tool call must NOT run, or None. (1) The turn sounds like his own last reply: his echo, not an order.
    (2) The turn was started by an interruption less than `x_s` seconds ago and we have no text for it: unknown
    speech that came right after his own voice is not allowed to open a door."""
    x_s = TOOL_VAKT_S if x_s is None else x_s
    if liknar_svar(turtext, hans):
        return "ignored: this sounded like your own last reply, not an order. Do nothing."
    if sedan_barge_s is not None and sedan_barge_s <= x_s and not (turtext or "").strip():
        return "ignored: unclear speech right after an interruption. Do nothing; ask if it was meant."
    return None
