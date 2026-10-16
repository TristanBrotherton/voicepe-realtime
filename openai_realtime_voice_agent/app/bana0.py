"""Bana 0: the fast path for plain home commands (raawr US-016).

When a turn ends, the turn's audio goes to the local Wyoming STT, the text
goes to Home Assistant's own conversation agent -- through raawr-comms, which
stays the only door -- and only if HA did not handle it does the realtime
model get asked to answer. A hit never reaches the model as a request; the
model is only told what was said, so it knows the order is done.

Everything here is a plain function with injected callables, so the pipeline
wiring (main.py / websocket_handler.py) stays a few lines and the tests need
no live HA, STT or model.
"""
import asyncio
import difflib
import inspect
import json
import logging
import os
import random
import re
import time
from typing import Awaitable, Callable, Optional

import httpx
from pipecat.services.openai.realtime import events

from app import ha_api
from app import klockan as klocka

logger = logging.getLogger(__name__)

RATE, WIDTH, CHANNELS = 16000, 2, 1
CHUNK_BYTES = 3200  # 100 ms of 16 kHz PCM16 mono
AUDIO = {"rate": RATE, "width": WIDTH, "channels": CHANNELS}

# Without internet (raawr US-018) the cloud TTS cannot render HA's reply or an
# answer. These two lines are rendered at startup (main._warm_early_acks) and
# cached on disk, so they play offline. Never a question mark: it opens the mic.
# Each line has variants (US-032 AC-8): the same line five times in a row gives at
# least three different ones, never the same twice running (`saga`).
OK_VARIANTER = ("Klart.", "Fixat.", "Ordnat.", "Gjort.", "Då var det klart.")
OFFLINE_VARIANTER = (
    "Jag når inte nätet just nu. Lampor och sånt fungerar ändå.",
    "Nätet är nere just nu. Lampor och sånt funkar ändå.",
    "Inget nät här just nu, men lamporna går att styra.",
)
OK_FALLBACK, OFFLINE_LINE = OK_VARIANTER[0], OFFLINE_VARIANTER[0]
LOKALA_REPLIKER = OK_VARIANTER + OFFLINE_VARIANTER
def text_till_modell_paa() -> bool:
    """Experiment (raawr US-032): on a miss, the model gets the locally heard words
    instead of the audio. OFF by default. LOCAL_TEXT_TO_MODEL=1 turns it on."""
    return os.environ.get("LOCAL_TEXT_TO_MODEL", "0") == "1"


def nej_stanger_paa() -> bool:
    """A whole-turn nej / tack / det var allt closes the session at once. ON by default; NEJ_STANGER=0 turns it off."""
    return os.environ.get("NEJ_STANGER", "1") != "0"


def ping_paa() -> bool:
    """The parallel track (raawr US-047): on a hit the held audio ALSO goes to the Gemini model, after a
    line saying what bana 0 heard and did. OFF by default. BANA0_PING=1 turns it on."""
    return os.environ.get("BANA0_PING", "0") == "1"


def tidigt_slut_paa() -> bool:
    """Gemini: a turn whose early speech-to-text is a complete command ends at "preend" instead of
    waiting the last stretch of silence (raawr US-047). OFF by default. TIDIGT_SLUT=1 turns it on."""
    return os.environ.get("TIDIGT_SLUT", "0") == "1"


# A command that ends on one of these is still going: "tänd lampan i", "släck köket och", "tänd inte", "slå på".
_OFARDIG_SLUT = {"och", "samt", "eller", "men", "i", "på", "av", "vid", "till", "med", "den", "det", "de", "en", "ett",
                 "som", "att", "också", "sen", "sedan", "lite", "typ", "alltså", "inte", "ej", "för", "så", "om", "när",
                 "kan", "ska", "vill", "bara", "nu", "då", "eller"}
# One order only: any of these inside the words means more may follow or the order is turned around.
_FLER_ELLER_NEJ = {"och", "samt", "eller", "men", "sen", "sedan", "inte", "ej", "så", "när", "om", "kan", "ska", "vill"}
# The words that open a plain light order. "stäng dörren", "ställ klockan på sju" and "vi går på bio" do not.
_LAMPORDER = re.compile(r"^(tänd\w*|släck\w*|(?:slå|sätt|stäng) (?:på|av))\b", re.I)


def komplett_kommando(text: Optional[str]) -> bool:
    """True when the heard words look like a whole order the fast track can act on: ONE order that begins
    with a light verb (tänd, släck, slå på/av ...) and names something, or a clock question. A coarse test:
    "släck tv:n" and "släcker du lampan" pass too; the fast track decides exactly as before, only sooner. At least two
    words, no word that adds or turns the order around ("och", "inte" ...), not ending on a word that asks
    for more. Anything else waits out the silence as before."""
    ord_ = re.findall(r"[\wåäöÅÄÖ]+", (text or "").lower())
    if len(ord_) < 2 or ord_[-1] in _OFARDIG_SLUT:
        return False
    if klocka.ar_klockfraga(text):
        return not (set(ord_) & _FLER_ELLER_NEJ)
    if set(ord_) & _FLER_ELLER_NEJ:
        return False
    m = _LAMPORDER.match(" ".join(ord_))
    if m is None or verb_ur(text) is None:
        return False
    return len(" ".join(ord_)[m.end():].split()) >= 1


def _rensa(text: str) -> str:
    """Words that go into the model's line: no quotes, no control characters or line breaks (G on #42: a
    line break in the heard words could start a new [huset] line)."""
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", (text or "").replace('"', "'")).strip()


def ping_text(hort: str, svar: str) -> str:
    """The line Live gets before the audio. What bana 0 HEARD is in it (not only what it did), so Live
    can tell when it hears something else; measured 2026-10-09 (tools/live_ping_prov.py): with the line
    Live does not repeat the tool call, and a misheard order is corrected from the audio."""
    hort = _rensa(hort)[:200]
    svar = _rensa(svar)[:120]
    return (f"[huset] Snabbspåret hörde \"{hort}\" och gjorde det redan (Home Assistant svarade \"{svar}\"). "
            "Gör inte om det med verktygen. Svara personligt i en mening. "
            "Hör du i ljudet något annat än det snabbspåret hörde, rätta det.")


_INTE_LAMPA = re.compile(r"timer|nedräkning|musik|spela|volym|radio|väder|påminn|alarm|larm|termostat|temperatur", re.I)
_AV = re.compile(r"\b(släck\w*|stäng(?:er)? av|slå av|sätt av|stäng)\b|\bav\b", re.I)
_PA = re.compile(r"\b(tänd\w*|slå på|sätt på|starta)\b|\bpå\b", re.I)


# A question about the state is not an order, also when it names the verb ('Är det tänt?', 'Vilka lampor är
# tända?'). Live 2026-10-17 00:20: 'Är det tänt i kontoret?' turned the whole office on ('tänt' and 'tänd' sound
# alike). A polite request is an order ('Kan du tända...', 'Var snäll och...').
# ponytail: the first word decides; "Vad bra, tänd lampan" reads as a question (refused, he says it again).
_FRAGA = re.compile(r"^\W*(är|vad|vilka|vilken|vilket|hur|finns|lyser|syns|står|när|var|varför|har)\b", re.I)
_BON = re.compile(r"^\W*((kan|kunde|skulle|vill|får|orkar)\s+(du|ni)|(är|var)\s+(du\s+|ni\s+)?snäll)\b", re.I)
# B on #91: a question can also start without a question word. It ends in '?', names a state ('tänt', 'lyser',
# 'brinner', 'på') and has no order verb ('tänd', 'släck'): 'Tänt i kontoret?', 'Brinner det i köket?',
# 'Funkar lampan?'. And "Kan du säga om..." asks to be told, not to switch.
_TILLSTAND = re.compile(r"\b(tänt|tända|tänd|släckt|släckta|lyser|brinner|på|av|igång|funkar|fungerar)\b", re.I)
_BEFALL = re.compile(r"\b(tänd|tända|släck|släcka|slå\s+(på|av)|sätt\s+(på|av)|stäng\s+av|stänga\s+av)\b", re.I)
_SAG_OM = re.compile(r"^\W*(kan|kunde|skulle)\s+(du|ni)\s+(säga|berätta|kolla|se(?!\s+till)|titta)\b", re.I)
# The tools that switch something; a question may read with every other tool.
SKRIV_VERKTYG = frozenset({"HassTurnOn", "HassTurnOff", "HassLightSet", "styr"})


def ar_fraga(text: Optional[str]) -> bool:
    t = (text or "").strip()
    if _SAG_OM.search(t):
        return True
    if _FRAGA.search(t) and not _BON.search(t):
        return True
    # 'tänd' alone ("Är lampan tänd?") is a state only in a sentence that is a question by its first word (above).
    tillstand = [m.group(1).lower() for m in _TILLSTAND.finditer(t)]
    return (t.endswith("?") and any(w != "tänd" for w in tillstand) and not _BEFALL.search(t)
            and not _BON.search(t))  # "Kan du se till att det är tänt?" asks for it to be so


def andrar(namn: str, argument: Optional[dict], lasande: Optional[dict] = None) -> bool:
    """Whether the tool changes something. comms says so per tool (`readOnlyHint`, raawr US-103: tools/list for the
    door's own, las_skill for what anvand_verktyg runs); a tool comms has not described is judged by SKRIV_VERKTYG,
    except one run through anvand_verktyg, which is taken to change something (A: missing = changing)."""
    lasande = lasande or {}
    bas = namn.rsplit("__", 1)[-1]
    genom = bas == "anvand_verktyg"
    if genom:  # comms' generic tool (raawr US-089): judged by the tool it runs
        bas = str((argument or {}).get("namn", "")).rsplit("__", 1)[-1]
    if bas in lasande:
        return not lasande[bas]
    return genom or bas in SKRIV_VERKTYG


def fraga_inte_order(turtext: Optional[str], namn: str, argument: Optional[dict],
                     lasande: Optional[dict] = None) -> Optional[str]:
    """Why a changing tool must not run on this turn, or None: the turn (bana 0's local text) was a question."""
    if not ar_fraga(turtext) or not andrar(namn, argument, lasande):
        return None
    return (f"ignored: he asked a question ('{turtext}'), he did not ask you to change anything. Answer the "
            "question from what you can read; switch nothing.")


def verb_ur(text: str) -> Optional[str]:
    """'Off' or 'On' for a plain light order, else None. Off wins ('släck' before 'på'). Anything that
    names a timer, music or the like is not a light order (the parallel track is for lights only), and a
    question is no order at all."""
    if ar_fraga(text) or _INTE_LAMPA.search(text or ""):
        return None
    if _AV.search(text or ""):
        return "Off"
    if _PA.search(text or ""):
        return "On"
    return None


_GENERISKA = {"lampan", "lamporna", "lampor", "ljuset", "ljusen", "belysningen", "belysning", "taket", "allt", "alla"}


class Atgardslogg:
    """What bana 0 just did, so Live cannot do it again.

    A prompt line is a request; this is the guard: a HassTurnOn/Off from the model for the same verb and the
    same place within TTL_S of a bana 0 hit is answered "already done" and never run. A different verb (Live
    heard 'tänd' where bana 0 put the light out) goes through: that IS the correction."""

    TTL_S = 10.0

    def __init__(self, klocka: Callable[[], float] = time.monotonic) -> None:
        self.klocka = klocka
        self._poster: list = []

    def skriv(self, hort: str, svar: str) -> None:
        verb = verb_ur(hort) or verb_ur(svar)
        if verb:
            self._poster.append((verb, (hort or "").lower(), self.klocka()))

    def redan(self, verktyg: str, argument: dict) -> bool:
        verb = "Off" if verktyg.endswith("TurnOff") else "On" if verktyg.endswith("TurnOn") else None
        ljusinstallning = verktyg.endswith("LightSet")  # brightness/colour right after a hit: the same order again
        argument = argument or {}
        # Every word of area/name/floor counts ("lampan i kontoret" is the place "kontoret"); a word that
        # only names the kind of thing says nothing about the place.
        ord_ = [w for key in ("area", "name", "floor") for w in str(argument.get(key) or "").lower().split()
                if len(w) >= 3 and w not in _GENERISKA]
        if (verb is None and not ljusinstallning) or not ord_:
            return False
        nu = self.klocka()
        self._poster = [p for p in self._poster if nu - p[2] <= self.TTL_S]
        # a word names the place when it and a heard word start the same way ('Kök' against 'köket')
        return any((ljusinstallning or v == verb) and any(w[:5] in h or any(len(x) >= 3 and (x.startswith(w) or w.startswith(x)) for x in h.split()) for w in ord_)
                   for v, h, _ in self._poster)


_VAKTADE = ("HassTurnOn", "HassTurnOff", "HassLightSet")


def skydda_verktyg(service, logg: "Atgardslogg") -> int:
    """Wrap the service's HassTurnOn/Off handlers with the log's check. Returns how many were wrapped."""
    def vakta(namn, orig):
        if getattr(orig, "_bana0_vaktad", False):
            return orig

        async def vaktad(params):
            if logg.redan(namn, params.arguments or {}):
                logger.info(f"⚡ bana0: {namn} {params.arguments} skipped; the fast track did it a moment ago")
                await params.result_callback({"result": "Redan gjort av huset för ett ögonblick sedan. Gör inget mer."})
                return
            await orig(params)

        vaktad._bana0_vaktad = True
        return vaktad

    antal = 0
    for namn, post in list(getattr(service, "_functions", {}).items()):
        if isinstance(namn, str) and namn.endswith(_VAKTADE):
            post.handler = vakta(namn, post.handler)
            antal += 1

    # The tools are fetched again at the wake (main._register_ha_handlers) and replace the handlers: the
    # guard has to follow every later registration too (G on #42, fynd 7).
    orig_register = service.register_function

    def register_function(function_name, handler, *args, **kwargs):
        if isinstance(function_name, str) and function_name.endswith(_VAKTADE):
            handler = vakta(function_name, handler)
        return orig_register(function_name, handler, *args, **kwargs)

    service.register_function = register_function
    return antal


_PASAR: dict = {}  # a shuffled bag per line: every variant once before any twice
_SENAST: dict = {}


async def saga(say: Callable[[str], Awaitable[None]], varianter: tuple) -> str:
    """Say one of `varianter` from a shuffled bag: five in a row are at least three
    different ones and never the same twice running. A variant that cannot be said
    (its clip is missing) gives the next, never silence; raises only when every
    variant failed. Returns the line said."""
    pase = _PASAR.setdefault(varianter, [])
    fel = None
    for _ in range(2 * len(varianter)):
        if not pase:
            pase.extend(random.sample(varianter, len(varianter)))
            if pase[-1] == _SENAST.get(varianter):  # pop() takes the last: not the one just said
                pase.insert(0, pase.pop())
        text = pase.pop()
        try:
            await say(text)
        except Exception as e:
            fel = e
            continue
        _SENAST[varianter] = text
        return text
    raise fel


def stt_adress(value: str) -> Optional[tuple[str, int]]:
    """The `bana0_stt` option, "host" or "host:port" (Wyoming default 10300). Empty or bad = off."""
    value = (value or "").strip()
    if not value:
        return None
    host, sep, port = value.rpartition(":")
    if not sep:
        return value, 10300
    try:
        return (host, int(port)) if host else None
    except ValueError:
        logger.warning(f"bana0: bad bana0_stt {value!r}, bana 0 off")
        return None


def _event(typ: str, data: Optional[dict] = None, payload: bytes = b"") -> bytes:
    header = {"type": typ, "data": data or {}}
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header).encode() + b"\n" + payload


async def _transkribera(pcm16k: bytes, host: str, port: int) -> Optional[str]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(_event("transcribe", {"language": "sv"}))
        writer.write(_event("audio-start", AUDIO))
        for i in range(0, len(pcm16k), CHUNK_BYTES):
            writer.write(_event("audio-chunk", AUDIO, pcm16k[i:i + CHUNK_BYTES]))
        writer.write(_event("audio-stop"))
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                return None  # server closed without a transcript
            header = json.loads(line)
            # Wyoming may put data and payload after the header line.
            data = header.get("data") or {}
            if header.get("data_length"):
                data = json.loads(await reader.readexactly(header["data_length"]))
            if header.get("payload_length"):
                await reader.readexactly(header["payload_length"])
            if header.get("type") == "transcript":
                return (data.get("text") or "").strip()  # "" = it listened and heard no words
    finally:
        writer.close()


async def transkribera(pcm16k: bytes, host: str, port: int, timeout: float) -> Optional[str]:
    """Raw Wyoming over asyncio streams. None on timeout, error, or empty text."""
    try:
        return (await asyncio.wait_for(_transkribera(pcm16k, host, port), timeout)) or None
    except Exception as e:  # incl. TimeoutError: every failure is a miss
        logger.warning(f"bana0: STT failed: {e!r}")
        return None


async def transkribera_tydligt(pcm16k: bytes, host: str, port: int, timeout: float) -> tuple[Optional[str], bool]:
    """(text, ok). ok=False is a MISS (timeout, error, no transcript): we do not know what was said. ok=True with
    "" is a clear "no words heard". The echo net needs the difference: a miss must count as a person (US-060, A's FAIL on #65)."""
    try:
        text = await asyncio.wait_for(_transkribera(pcm16k, host, port), timeout)
    except Exception as e:
        logger.warning(f"bana0: STT failed: {e!r}")
        return None, False
    return (None, False) if text is None else (text, True)


async def prova(text: str, timeout: float) -> Optional[str]:
    """Ask HA's conversation agent via comms. HA's spoken reply on a hit, else None.

    200 -> response.speech.plain.speech (comms answers 200 with HA's own
    failure text too; that is spoken, never sent to the model). 204 means HA
    did not handle it. Anything else, an error or a timeout is a miss.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                ha_api.url("/conversation/process"),
                headers=ha_api.headers(),
                json={"text": text},
            )
        if r.status_code != 200:
            return None
        speech = r.json()["response"]["speech"]["plain"]["speech"]
        if not isinstance(speech, str):
            return None
        return speech.strip() or None
    except Exception as e:
        logger.warning(f"bana0: comms failed: {e!r}")
        return None


# Whisper invents these on noise and music (kitchen 2026-10-09 21:17: "Ett tack till mina supporters via Patreon!").
# A phrase from subtitle credits is never a command; the turn then goes to the model with the audio as if STT heard nothing.
_PAHITT = ("patreon", "tack för att ni tittade", "tack för att du tittade", "tack för att ni tittat", "tack för att du tittat",
           "amara.org", "thanks for watching", "subtitles by")  # not "undertexter"/"prenumerera": real requests


def brus(text: Optional[str]) -> bool:
    """True when the text is a known Whisper invention or too short to be a request (empty, one letter)."""
    t = " ".join((text or "").lower().split())
    return len(re.sub(r"[\W\d_]+", "", t)) < 2 or any(f in t for f in _PAHITT)


# The speech-to-text is given a hint of common house phrases (the wyoming-raawr bridge, STT_INITIAL_PROMPT, HEN-362),
# and Whisper hands the hint back when what it hears is noise or his own voice from the speaker. Written of his
# own echo where nobody said it, 2026-10-12/13: 'Tänd i köket.' / 'Tänd i követ.' three times in replays of live
# recordings and once live (07:04:17), 'Vad är klockan?' twice, 'Björn.' once. And live 2026-10-12 21:40
# 'Tänd i köket.' -> HA 'Slog på lampan' (whether anyone said it is not known). A text that is the hint and nothing else is therefore no
# proof that anyone said it. The same default as the bridge; set STT_INITIAL_PROMPT here too if the bridge's is changed.
STT_LEDTRAD = os.environ.get("STT_INITIAL_PROMPT", "Vad är klockan? Tänd i köket. Björn.")
LEDTRAD_LIKA = 0.8      # 'Tänd i követ.' is still the hint; 'Tänd i köket och hallen' is not


def _meningar(text: Optional[str]) -> list:
    return [m for m in (" ".join(re.findall(r"[a-zåäöé0-9]+", d.lower())) for d in re.split(r"[.!?]+", text or "")) if m]


def ur_ledtraden(text: Optional[str]) -> bool:
    """True when every sentence of the text is one of the speech-to-text's own hint phrases, give or take a letter:
    the text may be invented. Anything said beyond the hint ("tänd i köket och hallen") makes it a person's words."""
    fraser = _meningar(STT_LEDTRAD)
    hort = _meningar(text)
    return bool(fraser and hort) and all(
        any(difflib.SequenceMatcher(None, m, f).ratio() >= LEDTRAD_LIKA for f in fraser) for m in hort)


# A whole turn that only says "no, that is all": the session closes at once, like "stopp" (Henrik 2026-10-11).
_NEJ = {"nej", "nä", "nej tack", "nä tack", "tack", "tack så mycket", "tack tack", "det var allt", "det var det",
        "det var allt tack", "nej det var allt", "nej tack det var allt", "nej det var det", "nej tack det är bra",
        "det är bra", "det är bra tack", "nej det är bra", "inget mer", "nej inget mer",
        # the words of US-060 AC-9 ("tack, det var allt") were not here: the session stayed open on them (offline 2026-10-13)
        "tack det var allt", "tack det var det", "tack det är bra", "tack det räcker", "det räcker"}


def ar_nej(text: Optional[str]) -> bool:
    """True when the whole turn is a no-thanks / that-is-all, nothing else."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split()) in _NEJ


async def tur(
    pcm: bytes,
    *,
    stt: Callable[[bytes, float], Awaitable[Optional[str]]],
    timeout_stt: float,
    timeout_comms: float,
    skicka_svar_till_modellen: Callable[[str], Awaitable[None]],
    skapa_svar: Callable[[], Awaitable[None]],
    efter_miss: Optional[Callable[[], None]] = None,
    efter_traff: Optional[Callable[[], None]] = None,
    klockan: Optional[Callable[[str], Awaitable[None]]] = None,
    skapa_svar_med_text: Optional[Callable[[str], Awaitable[None]]] = None,
    ping: Optional[Callable[[str, str], Awaitable[None]]] = None,
    tider=None,
    avsluta: Optional[Callable[[], Awaitable[None]]] = None,
    eko_veto: Optional[Callable[[str], bool]] = None,
    kasta_tur: Optional[Callable[[], Awaitable[None]]] = None,
    bara_avbrott: Optional[Callable[[Optional[str]], bool]] = None,
    vanta: Optional[Callable[[], Awaitable[None]]] = None,
) -> str:
    """One finished user turn. Returns 'nej', 'klockan', 'bana0' on a hit, 'vanta', 'modell' otherwise.

    `bara_avbrott(text)` True = this turn cut his reply off and said nothing to answer ("vänta"). `vanta()` then
    hands the turn to the model with its answer muted (full duplex, US-060): no house, no closing, nothing said.

    `avsluta()` closes the session when the whole turn was a nej / tack / det var allt: no
    comms, no model answer. If it fails, the turn goes on as before.

    `klockan(text)` says the time from cached clips (US-032 AC-7): a clock question
    reaches neither comms nor the model. If it fails, the turn goes on as before.

    `stt(pcm, timeout)` is typically transkribera bound to host/port.
    On a hit HA's own reply is NOT spoken (the owner 2026-10-03: "hellre tyst
    än den torra"): the model is told what was done and confirms in its own
    words, with no tools (`skicka_svar_till_modellen`). `efter_traff()` is the
    net when the model stays silent (vakta_bekraftelse, US-018).
    `efter_miss()` runs after the model was asked (vakta_natet, US-018).
    """
    pahittat = False
    try:
        text = await stt(pcm, timeout_stt) if pcm else None
        logger.info(f"bana0: heard {text!r}")
        if text and brus(text):
            logger.info(f"bana0: {text!r} is noise (a known invention or too short), treated as nothing heard")
            text = None
        if tider is not None:
            tider.mark("stt")
        if eko_veto is not None and text and kasta_tur is not None and eko_veto(text):
            # His own last reply came back through the mic as a "turn" (raawr US-060, Kontoret 2026-10-12 21:39):
            # the model is never asked to answer it. Nothing is said, nothing is done.
            try:
                await kasta_tur()
                logger.warning(f"bana0: {text!r} is his own last reply coming back; the turn is dropped")
                return "eko"
            except Exception as e:
                logger.warning(f"bana0: dropping the echo turn failed, it goes on: {e!r}")
        if bara_avbrott is not None and vanta is not None and bara_avbrott(text):
            # Before the nej below: his "vänta" is often written 'Tack.', and that would close the session.
            try:
                await vanta()
                logger.info(f"bana0: {text!r} only cut him off: he stops and listens, nothing is answered")
                return "vanta"
            except Exception as e:
                logger.warning(f"bana0: holding on after the interruption failed, the turn goes on: {e!r}")
        if avsluta is not None and ar_nej(text):
            try:
                await avsluta()
                logger.info(f"bana0: nej {text!r}, the session closes")
                return "nej"
            except Exception as e:
                logger.warning(f"bana0: closing on a nej failed, the turn goes on: {e!r}")
        if klockan is not None and klocka.ar_klockfraga(text):
            try:
                await klockan(text)
                logger.info(f"bana0: clock {text!r}")
                return "klockan"
            except Exception as e:
                logger.warning(f"bana0: the clock failed, the turn goes on: {e!r}")
        pahittat = ur_ledtraden(text)
        if pahittat:
            # The house is never touched on this text alone. The model gets the AUDIO, not the text: it hears for
            # itself whether anyone asked, and a real "tänd i köket" is then done by the model (a second or two later).
            logger.warning(f"bana0: {text!r} is the speech-to-text's own hint and nothing else: it may be invented. "
                           "Not sent to the house; the model answers from the audio")
        svar = await prova(text, timeout_comms) if text and not pahittat else None
        if tider is not None and text:
            tider.mark("comms")
    except Exception as e:
        logger.warning(f"bana0: turn failed, model answers: {e!r}")
        svar = None
    if not svar:
        try:
            if skapa_svar_med_text is not None and text and not pahittat:
                await skapa_svar_med_text(text)
            else:
                await skapa_svar()
        except Exception as e:  # no model socket: efter_miss says why
            logger.warning(f"bana0: asking the model failed: {e!r}")
        if efter_miss is not None:
            efter_miss()
        return "modell"
    # HA already acted: the model may only confirm it, never do it again.
    logger.info(f"bana0: hit {text!r} -> {svar!r}")
    try:
        if ping is not None:  # the parallel track: the model hears the audio too, after a line about the hit
            await ping(text, svar)
        else:
            await skicka_svar_till_modellen(gjort(text, svar))
    except Exception as e:
        logger.warning(f"bana0: telling the model failed after a hit: {e!r}")
    if efter_traff is not None:
        efter_traff()
    return "bana0"


def gjort(text: str, svar: str) -> str:
    """What the model is told after a hit: the order, HA's reply, and what to do."""
    return (
        f"Användaren sa: \"{text}\". Huset har REDAN gjort det; Home Assistant svarade: "
        f"\"{svar}\". Bekräfta kort med egna ord, en mening. Anropa inga verktyg och gör inget mer."
    )


def ingen_bekraftelse_an(liveness, asked: float) -> bool:
    """The model has not spoken since the hit and he has not started a new utterance.

    Not claim_silence_ack: bana 0 forces the phase idle right after a hit,
    which counts as "turn over" there and silenced the net every time
    (live 2026-10-04 17:14, Gemini: the lamp went off, nothing was said).
    """
    return not liveness.model_spoke_since(asked, 0.0) and liveness.user_started_at <= asked


async def vakta_bekraftelse(
    *,
    vanta_s: float,
    claim: Callable[[], bool],
    say_ok: Callable[[], Awaitable[None]],
) -> bool:
    """After a hit: if the model has said nothing after `vanta_s`, say one of OK_VARIANTER.

    Offline the model never answers; the room still hears it was done, in the
    engine's own voice (cached clip). True when the clip was said.
    """
    await asyncio.sleep(vanta_s)
    if not claim():
        return False
    logger.warning("bana0: no confirmation from the model, saying it is done")
    try:
        await say_ok()
    except Exception as e:
        logger.warning(f"bana0: fallback confirmation failed: {e!r}")
        return False
    return True


async def natet_nere(probe: Callable[[str], bool], engines, timeout: float) -> bool:
    """True when NO engine's API answers within `timeout`.

    An async probe is awaited on this loop. A sync probe still runs in a
    thread, so a blocking GET cannot freeze the loop. Every engine, not just
    the one running: an xAI-only outage is a failover for the router, not
    "no internet" for the room. A probe that hangs or raises counts as down
    for that engine.
    """
    async def one(engine) -> bool:
        try:
            if inspect.iscoroutinefunction(probe):
                return bool(await asyncio.wait_for(probe(engine), timeout))
            return bool(await asyncio.wait_for(asyncio.to_thread(probe, engine), timeout))
        except Exception:
            return False

    engines = [e for e in dict.fromkeys(engines) if e]
    if not engines:
        return False
    return not any(await asyncio.gather(*(one(e) for e in engines)))


async def vakta_natet(
    *,
    natet_nere: Callable[[], Awaitable[bool]],
    claim: Callable[[], bool],
    say: Callable[[str], Awaitable[None]],
) -> bool:
    """After a miss: if the model's engine cannot be reached, say one of OFFLINE_VARIANTER.

    `claim()` is the turn's once-only silence slot (TurnLiveness.claim_silence_ack):
    false when the model already spoke or the "Ett ögonblick" ack took it, so
    the room never hears both. True when the line was said.
    """
    try:
        if not await natet_nere():
            return False
    except Exception as e:
        logger.warning(f"bana0: engine probe raised: {e!r}")
    if not claim():
        return False
    logger.warning("bana0: the engine is unreachable, saying so")
    try:
        await saga(say, OFFLINE_VARIANTER)
    except Exception as e:
        logger.warning(f"bana0: offline line failed: {e!r}")
        return False
    return True


async def be_om_bekraftelse(service, besked: str) -> None:
    """After a hit: tell the model what was done and ask for a short spoken confirmation.

    A system item, then response.create with no tools, so the model cannot
    act on the order a second time.
    """
    await service.send_client_event(events.ConversationItemCreateEvent(
        item=events.ConversationItem(
            type="message", role="system",
            content=[events.ItemContent(type="input_text", text=besked)],
        )
    ))
    await service.send_client_event(events.ResponseCreateEvent(
        response=events.ResponseProperties(tool_choice="none")
    ))


async def redan_besvarat(service, fraga: str, svar: str) -> None:
    """After the clock (US-032 AC-7): the model heard the question; tell it it is
    answered, with no response.create, so the next turn does not answer it again."""
    await service.send_client_event(events.ConversationItemCreateEvent(
        item=events.ConversationItem(
            type="message", role="system",
            content=[events.ItemContent(type="input_text", text=(
                f"Användaren frågade \"{fraga}\" och har redan fått svaret: \"{svar}\". "
                "Svara inte på den frågan igen."
            ))],
        )
    ))


async def be_om_svar(service) -> None:
    """Bana 0 missed: ask the model to answer the turn (turn detection has create_response off)."""
    await service.send_client_event(events.ResponseCreateEvent())
