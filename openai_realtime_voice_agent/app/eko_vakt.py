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
# While he is speaking only these interrupt him, spelt exactly so. Replayed on the live recordings of 2026-10-12/13
# (tools/eko_replay.py): the local speech-to-text writes 'Tack.', 'Tack så mycket.', 'Björn.', 'Vad sluts?' and
# 'Tänd i köket.' of his own voice coming back, so 'tack', 'björn', 'nej', 'hallå' and near-misses are NOT stop words.
STOPPORD = {"vänta", "stopp", "stop", "stoppa", "sluta", "tyst", "tysta", "paus", "pausa", "avbryt"}
UNDER_TAL_S = 1.0       # his voice is in the room this long after the last audio was sent (device buffer 0.4-0.6 s + room)
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


def egen_tur(rum: str = "") -> bool:
    """GEMINI_EGEN_TUR=1: Google's own activity detection and barge-in run the conversation (raawr US-060, Henrik
    2026-10-13: "varför bygga det själva när Google redan gjort researchen"). The mic audio -- echo-cancelled here first
    (SOFT_AEC) -- is streamed as it comes; no local turn detection, no bana 0, no stop-word or level rules. What is left
    of ours is the tool guard, and the gate that sends nothing when nobody woke him. "1" = every room; else room names
    ("kontoret,koket"): the other rooms keep local turns and bana 0. In such a room the mic is not amplified between
    replies either (MjukAec.for_enhet): our gain lifted the room's noise 18 dB before the engine got it (2026-10-13)."""
    v = os.environ.get("GEMINI_EGEN_TUR", "0").strip().lower()
    return v == "1" or (bool(rum) and rum.lower() in [r.strip() for r in v.split(",")])


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


def har_stoppord(text: "str | None") -> bool:
    return any(w in STOPPORD for w in ord_(text))


def under_tal(sedan_svar_s: float, kandidat_s: float, fas_svarar: bool) -> bool:
    """The candidate STARTED while his voice was in the room: he is replying now, or the last reply audio was sent no
    longer ago than the candidate is long plus UNDER_TAL_S."""
    return fas_svarar or sedan_svar_s <= kandidat_s + UNDER_TAL_S


ERLE_TILLIT_DB = 20.0   # the canceller took this much off his echo in the seconds before the speech: what is loud now is not him
ERLE_TILLIT_RAMAR = 80  # ...measured over at least 0.8 s of his voice (the first second of a reply is never trusted)
ERLE_TILLIT_S = 4.0     # ...the last 1.5 s of it (ERLE_TILLIT_SISTA) within this long before the speech began: he pauses between sentences
ERLE_TILLIT_SISTA = 150


def dom(hort: "str | None", ok: bool, hans: str, under_tal: bool = False, erle_fore: "float | None" = None) -> bool:
    """The net's verdict: True = ignore (his own words, or clearly no words).

    `under_tal` (the speech started while his own voice was in the room). Two cases:
    - The canceller is doing its work (`erle_fore` >= ERLE_TILLIT_DB): his echo is some 30 dB down, far under the floor
      the caller sets (eko_vakt.niva_dbfs). A sound over that floor is a person, whatever the speech-to-text makes of it
      -- Henrik's second, louder "vänta" was written 'Tack.' (2026-10-13 09:48). Only his own words are still ignored.
    - It is not, or nobody knows (the start of a reply, a cold canceller): then only a stop word interrupts him.
      Everything else the speech-to-text writes there is his own voice coming back, however it is spelt -- 'Fåla.',
      'Lämmar sa mord.', 'Det är en klockan.', 'Välkommen till mig.' all cut him and started a new reply on top of
      the old one (live 2026-10-12 21:01 and 2026-10-13 07:03). A speech-to-text that failed says nothing about a stop
      word, so it does not interrupt him either.

    Just after he stopped (not `under_tal`) it is the old rule: his own words are ignored, anything else is a person,
    and in doubt (ok False) it is the person -- a short answer to his question ("köket") must get through (A on #65, #70)."""
    slacker = under_tal and erle_fore is not None and erle_fore >= ERLE_TILLIT_DB
    if not ok:
        return under_tal and not slacker
    if ar_eko(hort, hans):
        return True
    if slacker:
        return False  # also when the text is one of the LUDD phrases: Henrik's shouted "vänta" was written as one
    if any(l in (hort or "").lower() for l in LUDD):
        return True  # what it writes of noise
    return under_tal and not har_stoppord(hort)


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


def vakt_oppen(fas: str, idle_sedan: float, sedan_barge_s: "float | None", nu: "float | None" = None) -> bool:
    """The echo veto and the tool guard apply now: he is replying, he stopped a moment ago, or his reply was cut by an
    interruption less than TOOL_VAKT_S ago. The last one is the turn an interruption starts: the phase goes listening ->
    thinking and never idle, so the first two are both false exactly when that turn ends and the model calls a tool
    (A on #72). An interruption on his own echo (the level alone cuts him, MjukAec.person) is then a turn made of his
    own voice with the tools unguarded."""
    return fas == "replying" or i_svans(idle_sedan, nu) or (sedan_barge_s is not None and sedan_barge_s <= TOOL_VAKT_S)


def veto_tur(fas: str, idle_sedan: float, sedan_barge_s: "float | None", turtext: "str | None", hans: str) -> bool:
    """Drop this turn before the model is asked: the guards apply and its words are his own last reply."""
    return vakt_oppen(fas, idle_sedan, sedan_barge_s) and liknar_svar(turtext, hans)


def veto_verktyg(fas: str, idle_sedan: float, sedan_barge_s: "float | None", turtext: "str | None", hans: str) -> "str | None":
    """Why a tool call must not run now, or None (vakt_verktyg, inside the window where the guards apply)."""
    if not vakt_oppen(fas, idle_sedan, sedan_barge_s):
        return None
    return vakt_verktyg(turtext, hans, sedan_barge_s)


# Words that, alone or together, only tell him to hold on: "vänta lite", "stopp stopp", "nej vänta".
BARA_AVBROTT_ORD = STOPPORD | {"lite", "nu", "nej", "nä", "okej", "ok", "björn", "tack", "ett", "tag", "en", "sekund", "stund", "du"}


def bara_avbrott(turtext: "str | None") -> bool:
    """The turn that cut him off said nothing to answer: no words the speech-to-text could write, only hold-on words
    ("vänta lite", "nej tack"), or one word that is a hold-on word misspelt ('Välta!', 'Änta!' for "vänta"). He stops
    and listens; nothing is said back.

    Any OTHER word is something to answer, also alone: "hjälp", "tänd". Henrik 2026-10-13: "ett enda ord som avbryter
    Björn får inte tvingas till 'vänta'. Han ska tystna, lyssna och svara på det jag faktiskt säger, som en människa."
    (The first version took every single word for "vänta", because the speech-to-text also writes his "vänta" as
    'Råd.' or 'Stå.'; those now go to the engine, which hears the audio and answers what was said.)"""
    h = ord_(turtext)
    if not h or all(w in BARA_AVBROTT_ORD for w in h):
        return True
    return len(h) == 1 and bool(difflib.get_close_matches(h[0], STOPPORD, n=1, cutoff=0.75))
