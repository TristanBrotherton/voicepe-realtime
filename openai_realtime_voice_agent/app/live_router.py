"""Turn router for the GPT-Live runtime: the app decides and runs every tool.

GPT-Live's voice model cannot call a function itself (``session.tools`` is
rejected as an unknown parameter), and in Responses delegation the decision to
delegate is the voice's own. Retained turns showed what that costs: the voice
stayed silent on a household command, said "Done." with no tool, or answered a
list question from stale memory while the backend was still looking.

So the session runs in **client delegation** and this module owns the work:

* when the user's transcript closes (or the voice delegates, whichever comes
  first) the router routes the request exactly once per utterance;
* step 1 is Home Assistant's own local intent engine, used for recognition
  only (``conversation/agent/homeassistant/debug`` never executes). A
  confident match on a safe device domain becomes one call of the same MCP
  tool the backend would have used, so it goes through the same guards;
* otherwise step 2 is a Responses backend we call directly, with the full
  tool set every time: priority service tier, the lowest reasoning effort,
  parallel tool calls, a stable cacheable prefix (instructions and tool
  definitions first, volatile context last) and a prompt cache key;
* every tool runs through the caller's ``execute`` callback, which is the
  service's guarded path (speaker gate, action gate with spoken-yes
  confirmation, per-utterance repeat refusal, liveness, timeline);
* the outcome is a :class:`RouteResult` carrying only what tools verified.

Nothing here speaks. The service turns a result into one
``session.commentary.append`` for the voice to say.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.ha_household import fold, mentions
from app.live_protocol import responses_function_tool
from app.live_tasks import deferred_commentary, explain_ack_commentary, is_deferred_request

logger = logging.getLogger(__name__)

RESPONSES_URL = "https://api.openai.com/v1/responses"
HA_WEBSOCKET_URL = "ws://supervisor/core/websocket"

# Backend defaults. gpt-6-luna accepts reasoning effort none|low|...; it
# rejects "minimal" (measured 2026-10-12). Priority is echoed as tier "fast".
ROUTER_SERVICE_TIER = "priority"
ROUTER_REASONING_EFFORT = "none"
ROUTER_MAX_OUTPUT_TOKENS = 300
ROUTER_MAX_STEPS = 6
ROUTER_CALL_TIMEOUT_S = 25.0
ROUTER_HISTORY_TURNS = 6
HA_INTENT_TIMEOUT_S = 1.2
# No holder of the Core socket legitimately keeps it longer than this (a
# registry read: 5 s connect + 10 s reply). Older is a stuck holder: the lock
# and the socket are replaced so recognition does not stay "busy" for good
# (Workshop 2026-10-15 08:39 onwards: every turn routed to the backend).
HA_SOCKET_LOCK_STALE_S = 30.0
WS_MAX_FRAME_BYTES = 64 * 1024 * 1024

# Sentinels the backend answers with when no tool is wanted.
CHAT = "CHAT"
NO_REQUEST = "NO_REQUEST"

# A conservative per-token estimate used ONLY for the spend cap, so backend
# tokens count against the same daily ceiling as voice time. Deliberately on
# the high side; it is not a price quote.
USD_PER_INPUT_TOKEN = 2.5e-6
USD_PER_CACHED_INPUT_TOKEN = 0.25e-6
USD_PER_OUTPUT_TOKEN = 15e-6
PRIORITY_MULTIPLIER = 2.0

ROUTER_BACKEND_INSTRUCTIONS = """You are the backend of a smart-home voice assistant. The last user message is what the user just said aloud; transcripts can contain recognition errors and fragments.

Carry the request out with the tools, or look it up with them:
- Call independent tools in parallel in one step.
- Call an action tool directly with the exact device or area name from the home's list below; look up state first only when the request needs it.
- People speak loosely: match their words to the closest listed device, room or floor ("upstairs bedroom" is the bedroom on the upstairs floor, "the kitchen" with no device means its lights). A request that names no room means the room of the satellite the user is speaking to. Ask only when two or more listed devices fit equally well.
- Prefer a specific tool over the general assistant: when a script or intent tool exists for what was asked (a person's location or arrival time, the car, a "tell me when" watch), call that tool. Use ask_openclaw only when no tool covers the request; it is slow.
- A question about how the user's own software, projects, systems or code work, are designed or stand ("explain how our X handles Y", "what's the status of project Z", "how does the bridge authenticate") is research: call ask_openclaw with mode "explain" and the full question; a follow-up to an earlier explanation is the same, with the earlier topic repeated in the question. If the user then asks for the answer as a text ("text would be great", "send it by text"), call deliver_by_text.
- A request to be told when something happens ("tell me when X leaves") is done once the watch tool has run: say the user will be told, nothing more.
- Never claim an action succeeded unless its tool result confirms it. If a tool fails, say what failed.
- If an action returns confirmation_required, do not act: reply with one short yes/no question. Call confirm_action with that confirm_id only after the user clearly says yes in a later message.
- If the request is ambiguous, reply with one short clarifying question.

Answer formats:
- Not addressed to the assistant (background speech, television, a cut-off fragment with no request): reply exactly NO_REQUEST. A question cut short but still clear (for example "what time") is a request.
- Needs no tool (greeting, small talk, general knowledge you can answer without looking anything up): reply exactly CHAT.
- Otherwise: one or two short sentences for speech stating the verified result. No lists, URLs or markdown. Use ordinary names, never entity ids."""

# Appended to the backend instructions when discussions are enabled.
DISCUSSION_BACKEND_RULE = """
Discussions: list_open_discussions, open_discussion and propose_decision talk through the owner's open project items. Once open_discussion has been called in this conversation, what the user says next is part of that discussion: answer it in one or two spoken sentences from the item (call open_discussion again to get its details back); never reply NO_REQUEST or CHAT to it. Say only an item's topic and question unless the user asks for the details or the recommendation. Before propose_decision, read the decision back in one sentence and ask whether to record it; call propose_decision only after the user's clear yes, then say a text is coming to confirm it with a thumbs-up."""

# Home Assistant built-in intents the router may run locally, mapped to the
# bare MCP tool name that performs them. Anything else goes to the backend.
LOCAL_INTENTS = {
    "HassTurnOn": "HassTurnOn",
    "HassTurnOff": "HassTurnOff",
    "HassLightSet": "HassLightSet",
    "HassListAddItem": "HassListAddItem",
    "HassListCompleteItem": "HassListCompleteItem",
    "HassMediaPause": "HassMediaPause",
    "HassMediaUnpause": "HassMediaUnpause",
    "HassMediaNext": "HassMediaNext",
    "HassMediaPrevious": "HassMediaPrevious",
    "HassSetVolume": "HassSetVolume",
    "HassFanSetSpeed": "HassFanSetSpeed",
    "HassVacuumStart": "HassVacuumStart",
    "HassVacuumReturnToBase": "HassVacuumReturnToBase",
    # The clock: answered by the date/time read tool, never by the voice's guess.
    "HassGetCurrentTime": "GetDateTime",
    "HassGetCurrentDate": "GetDateTime",
}
# Only these entity domains may be acted on through the local path. Locks,
# covers (garage doors, gates), alarm panels, valves, scripts and automations
# always go through the backend, where the action gate asks for a spoken yes.
SAFE_LOCAL_DOMAINS = frozenset({
    "light", "switch", "fan", "media_player", "todo", "vacuum", "input_boolean",
})
NO_TARGET_INTENTS = frozenset({"HassGetCurrentTime", "HassGetCurrentDate"})
# A local action must name its target ("name" or "area" slot) and resolve to
# at most this many entities; a domain-only "turn on the lights" would fan out
# to every exposed light, so it goes to the backend instead.
LOCAL_MAX_TARGETS = 8

_PAST = {
    "HassTurnOn": "turned on", "HassTurnOff": "turned off", "HassLightSet": "set",
    "HassListAddItem": "added", "HassListCompleteItem": "checked off",
    "HassMediaPause": "paused", "HassMediaUnpause": "resumed", "HassMediaNext": "skipped to the next track on",
    "HassMediaPrevious": "went back a track on", "HassSetVolume": "set the volume on",
    "HassFanSetSpeed": "set the speed of", "HassVacuumStart": "started",
    "HassVacuumReturnToBase": "sent home",
}


# ---------------------------------------------------------------------------
# what the voice may say before a verified result exists
# ---------------------------------------------------------------------------

# What the voice may say before a verified result exists: an explicit
# "I'm working on it", and nothing else. A closed grammar of whole phrases,
# not a bag of words: a bag let "Just one.", "Mm hmm.", "Absolutely.", a bare
# "Yes." and a capitalised answer after "Okay," through (reviews B3-B6). Speech
# that is not a complete phrase from this grammar is held until the router's
# verdict; the fallback clip covers the silence.
# Lead-ins carry no answer: no affirmatives ("absolutely", "of course",
# "mm hmm", "right"), which answer a yes/no question (eighth review N27).
_LEAD_INS = tuple(tuple(p.split()) for p in (
    "sure", "ok", "okay", "alright", "all right", "got it",
    "sure thing", "um", "uh", "so",
))
_WAIT_CORE = (
    "one sec", "one second", "just a sec", "just a second", "a sec", "a second",
    "one moment", "just a moment", "a moment", "just one moment", "just one sec",
    "give me a moment", "give me a second", "give me a sec", "bear with me",
    "checking", "let me check", "let me look", "let me see", "let me find out",
    "let me look into it", "looking", "looking into it", "ill check", "ill look",
    "on it", "hang on", "hold on", "working on it", "coming right up", "one moment please",
    # Progress on a slow request (an agent question, a web search): the voice
    # is asked to say it is still working, and says so in words like these.
    "still working on it", "still checking", "still looking", "still on it",
    "just a little longer", "thanks for waiting",
    "thanks for your patience", "this is taking a little longer",
    "this is taking a bit longer", "its taking a little longer", "its taking a bit longer",
    "this one is taking a little longer", "this ones taking a little longer",
    "taking a little longer", "taking a bit longer", "still waiting on it",
    # v35: real acknowledgements the voice said on its own (GPT-LIVE-TOOLS-
    # RECONCILIATION section 4) that the grammar held.
    "let me pull that up", "let me pull it up", "checking live",
)
_WAIT_TAILS = ("", "that", "that for you", "for you", "that up", "that up for you", "now",
               "on that", "the list", "your list", "the lights", "the light", "the weather",
               "your calendar", "the calendar", "the timer", "your timers", "the timers",
               "the music", "your messages")
_WAITS = tuple(sorted({tuple((core + " " + tail).split()) for core in _WAIT_CORE for tail in _WAIT_TAILS},
                      key=len, reverse=True))
_NON_SPEECH = re.compile(r"^\s*(\[[^\]]*\]\s*)+$")

ACK = "ack"          # a complete acknowledgement: may be heard
ACK_PREFIX = "prefix"  # could still become one: keep waiting, play nothing yet
NOT_ACK = "no"       # anything else: hold until the verdict


def _words(text: str) -> List[str]:
    folded = unicodedata.normalize("NFKD", (text or "").replace("\u2019", "'").lower())
    folded = folded.encode("ascii", "ignore").decode()
    return [w.replace("'", "") for w in re.findall(r"[a-z']+|\d+", folded)]


def acknowledgement_state(text: str) -> str:
    """ACK, ACK_PREFIX or NOT_ACK for the voice's speech so far."""
    if _NON_SPEECH.match(text or ""):
        return ACK
    # "One moment, <name>." Only a configured household name counts as a form
    # of address; any other trailing word is part of what was said.
    match = re.search(r",\s*([A-Za-z'\u2019-]+)\s*[.!]?\s*$", text or "")
    if match and _is_form_of_address(match.group(1)):
        text = text[:match.start()] + "."
    words = _words(text)
    if not words:
        return ACK_PREFIX
    return _parse(words, 0, False)


def _parse(words: List[str], i: int, waited: bool) -> str:
    if i == len(words):
        return ACK if waited else ACK_PREFIX
    best = NOT_ACK
    # Lead-ins only before the first waiting phrase: "Let me check.
    # Absolutely." is an answer after an acknowledgement (eighth review B7).
    for unit, is_wait in [(u, False) for u in (() if waited else _LEAD_INS)] + [(u, True) for u in _WAITS]:
        rest = words[i:]
        if tuple(rest[:len(unit)]) == unit:
            state = _parse(words, i + len(unit), waited or is_wait)
            if state == ACK:
                return ACK
            if state == ACK_PREFIX:
                best = ACK_PREFIX
        elif len(rest) < len(unit) and tuple(unit[:len(rest)]) == tuple(rest):
            best = ACK_PREFIX  # the words so far are the start of a phrase
    return best


def is_acknowledgement(text: str) -> bool:
    """True only for a complete acknowledgement ("Sure, one sec.", "Checking.",
    "Let me check that for you."). "It's on.", "Just one.", "Yes.",
    "Absolutely." and a weekday after "Okay," are not."""
    return acknowledgement_state(text) == ACK


# Names the voice may address the household by (the configured speaker
# names). Only these count as a form of address: a capitalised word in a
# transcript is just as often the answer ("Okay, Saturday."), so nothing else
# may be dropped (sixth review B4). Set by main.py from the add-on options.
KNOWN_NAMES: set = set()


def set_known_names(names) -> None:
    KNOWN_NAMES.clear()
    KNOWN_NAMES.update(str(n).strip().lower() for n in names or () if str(n).strip())


def _is_form_of_address(word: str) -> bool:
    """A configured household name, and nothing else."""
    folded = word.replace("\u2019", "'").strip("'-").lower()
    return bool(folded) and folded in KNOWN_NAMES


HELD_SAMPLE_MAX_WORDS = 14
# Capitalised words kept as they are in a held-speech sample: the grammar's
# own vocabulary and common sentence openers. Any other capitalised word,
# wherever it stands, may be a name and is replaced.
_SAMPLE_KEEP = frozenset(
    {w for phrase in _LEAD_INS + _WAITS for w in phrase}
    | set("""i im ill ive id it its the a an this that these those there theres here heres
    yes no yeah sure okay ok alright well so um uh hmm oh ah let lets give just one
    you youre your we were our they he she what whats how hows when where who why which
    can could would will should do does did is are was be been got have has had all
    good great thanks thank sorry please hey hi hello right now then also and but or
    if not nothing done on off in at to for of with about""".split()))


def redact_for_log(text: str, max_words: int = HELD_SAMPLE_MAX_WORDS) -> str:
    """The voice's own pre-result words, safe for an add-on log line.

    Kept: lower-case words, and capitalised words from the acknowledgement
    vocabulary or common openers. Replaced: a configured household name with
    <name>, any other capitalised word with <Name> (device, place and person
    names are capitalised in transcripts, also at a sentence start), and
    numbers with <n>. At most ``max_words`` words; the rest is counted."""
    out: List[str] = []
    words = (text or "").replace("\u2019", "'").split()
    for raw in words[:max_words]:
        core = raw.strip(".,!?;:\"()[]")
        lead = raw[:len(raw) - len(raw.lstrip(".,!?;:\"()["))]
        trail = raw[len(lead) + len(core):]
        possessive = ""
        if core.lower().endswith("'s"):
            core, possessive = core[:-2], core[-2:]
        folded = core.strip("'-").lower()
        if not core:
            token = raw
        elif folded in KNOWN_NAMES:
            token = lead + "<name>" + possessive + trail
        elif any(ch.isdigit() for ch in core):
            token = lead + "<n>" + possessive + trail
        elif core[:1].isupper() and folded.replace("'", "") not in _SAMPLE_KEEP:
            token = lead + "<Name>" + possessive + trail
        else:
            token = raw
        out.append(token)
    if len(words) > max_words:
        out.append(f"(+{len(words) - max_words} words)")
    return " ".join(out)


def is_non_speech(text: str) -> bool:
    """A transcript that is only bracketed non-speech marks ("[hum]")."""
    return bool(_NON_SPEECH.match(text or ""))


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

ACTION = "action"
LOOKUP = "lookup"
CONFIRM = "confirm"
FAILED = "failed"
CHAT_KIND = "chat"
REPLY = "reply"  # backend text with no tool behind it (a clarifying question)
NOTHING = "nothing"
# A tool accepted the work and reports back later (an agent task): truthful
# commentary now, the verified answer when it lands (app/live_tasks.py).
PENDING = "pending"


@dataclass
class ToolRun:
    name: str
    ok: bool
    kind: str
    pending_confirmation: bool = False
    pending: bool = False
    task_id: str = ""
    deferred: bool = False
    explain: bool = False


def tool_run(name: str, outcome: Any) -> ToolRun:
    """A ToolRun from an execute() outcome (duck-typed, like the callers)."""
    return ToolRun(name=name, ok=bool(getattr(outcome, "ok", False)),
                   kind=str(getattr(outcome, "kind", "error")),
                   pending_confirmation=bool(getattr(outcome, "pending_confirmation", False)),
                   pending=bool(getattr(outcome, "pending", False)),
                   task_id=str(getattr(outcome, "task_id", "") or ""),
                   deferred=bool(getattr(outcome, "deferred", False)),
                   explain=bool(getattr(outcome, "explain", False)))


@dataclass
class RouteResult:
    kind: str
    text: str = ""
    source: str = "none"  # ha_local | backend | none
    tools: List[ToolRun] = field(default_factory=list)
    timings: Dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        """At least one tool actually answered for this result."""
        return bool(self.tools)

    @property
    def task_ids(self) -> List[str]:
        """The external task ids this result is still waiting on."""
        return [r.task_id for r in self.tools if r.pending and r.task_id]

    @property
    def deferred(self) -> bool:
        """The answer comes when an event happens (a watch), not after work."""
        return any(r.deferred for r in self.tools)

    @property
    def explain(self) -> bool:
        """A pending explanation (minutes of research; the voice sets expectations)."""
        return any(r.explain for r in self.tools)

    def commentary(self) -> Optional[str]:
        """The one line the voice is given to say, or None for nothing."""
        text = (self.text or "").strip()
        if self.kind in (ACTION, LOOKUP):
            return ("Verified result from the home system: " + text +
                    " Tell the user this briefly in your own words and add nothing that is not stated here.")
        if self.kind == CONFIRM:
            return ("Nothing has been done yet: a spoken yes is required first. Ask the user this yes/no "
                    "question and nothing else: " + text)
        if self.kind == REPLY:
            return ("Say this to the user briefly, as a question or remark; nothing has been done: " + text)
        if self.kind == FAILED:
            return ("The request was not completed: " + (text or "it could not be carried out.") +
                    " Tell the user briefly; do not say it was done.")
        if self.kind == PENDING:
            if self.deferred:
                return deferred_commentary(text)
            if self.explain:
                return explain_ack_commentary()
            return ("The request has been handed to the household assistant, which is still working "
                    "on it and will report back later. " + (text + " " if text else "") +
                    "Tell the user this briefly in your own words; do not guess the result and do "
                    "not say it is done.")
        return None


ExecuteFn = Callable[[str, dict], Awaitable[Tuple[Any, Any]]]


# ---------------------------------------------------------------------------
# Home Assistant local intent recognition (never executes)
# ---------------------------------------------------------------------------

@dataclass
class LocalMatch:
    intent: str
    slots: Dict[str, Any]
    domains: List[str]
    targets: int


class HaIntentRecognizer:
    """Recognise a sentence with Home Assistant's built-in intents, over one
    persistent Core websocket. Uses the debug command, which matches and
    resolves targets but runs nothing."""

    def __init__(self, token: str, url: str = HA_WEBSOCKET_URL, language: str = "en",
                 timeout_s: float = HA_INTENT_TIMEOUT_S, connect=None):
        self.token = token
        self.url = url
        self.language = language
        self.timeout_s = timeout_s
        self._connect = connect
        self._ws = None
        self._lock = asyncio.Lock()
        self._held_since: Optional[float] = None
        self._id = 0
        self._device_id_supported = True
        # The latest recognition's intent and slots, matched or not.
        self.last_heard: Optional[Dict[str, Any]] = None

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def _socket(self):
        if self._ws is not None:
            return self._ws
        connect = self._connect
        if connect is None:  # pragma: no cover - the real socket
            from websockets.asyncio.client import connect as websocket_connect

            def connect(url):
                # Registry lists for the household directory exceed the
                # default 1 MiB frame (4,800 entities: refused, 0.18.30).
                return websocket_connect(url, max_size=WS_MAX_FRAME_BYTES)
        ws = await connect(self.url)
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            await ws.close()
            raise RuntimeError("Home Assistant refused the websocket token")
        self._ws = ws
        return ws

    async def warm(self) -> None:
        """Open the socket ahead of the first request (a keepalive, not a turn)."""
        if not self.available:
            return
        try:
            async with self._held(5.0):
                if self._ws is not None:
                    # A socket idle since the last wake may be dead without
                    # knowing it: ping it, and reopen it if it does not answer.
                    try:
                        self._id += 1
                        ping_id = self._id
                        await self._ws.send(json.dumps({"id": ping_id, "type": "ping"}))

                        async def _pong():
                            while json.loads(await self._ws.recv()).get("id") != ping_id:
                                pass
                        await asyncio.wait_for(_pong(), 1.0)
                        return
                    except Exception:
                        await self._drop()
                await asyncio.wait_for(self._socket(), 5.0)
        except Exception as e:
            logger.info(f"ℹ️ HA intent socket not warmed: {e!r}")

    async def recognize(self, text: str, device_id: Optional[str] = None) -> Optional[LocalMatch]:
        """``device_id`` is the satellite that heard the request: Home
        Assistant uses its area as context, so "the lights" resolves to that
        room (an area slot filled from context) instead of every light."""
        if not self.available or not (text or "").strip():
            return None
        self.last_heard = None
        try:
            # A directory refresh may hold the socket; recognition waits for
            # it only within its own budget.
            lock = await self._acquire(self.timeout_s)
        except asyncio.TimeoutError:
            logger.info("ℹ️ HA intent socket busy; recognising without it this turn")
            return None
        self._held_since = time.monotonic()
        try:
            for attempt in (1, 2):
                try:
                    return await asyncio.wait_for(self._recognize(text, device_id), self.timeout_s)
                except asyncio.TimeoutError:
                    # 0.18.30: a stale socket after a long idle stalled the
                    # first recognition of a wake past the budget, and a
                    # timeout counted as "no match". Retry once on a fresh one.
                    await self._drop()
                    if attempt == 2:
                        logger.info("ℹ️ HA intent recognition timed out twice")
                        return None
                except Exception as e:
                    await self._drop()
                    if attempt == 2:
                        logger.info(f"ℹ️ HA intent recognition unavailable: {e!r}")
                        return None
        finally:
            self._release(lock)
        return None

    async def command(self, payload: dict, timeout_s: float = 10.0) -> dict:
        """Any Core websocket command on the same socket (registry reads for
        the household directory); returns the reply."""
        if not self.available:
            return {"success": False, "error": {"message": "no token"}}
        async with self._held(HA_SOCKET_LOCK_STALE_S):
            try:
                ws = await asyncio.wait_for(self._socket(), 5.0)
                self._id += 1
                msg_id = self._id
                await ws.send(json.dumps(dict(payload, id=msg_id)))

                async def _reply():
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get("id") == msg_id:
                            return reply
                return await asyncio.wait_for(_reply(), timeout_s)
            except Exception:
                await self._drop()
                raise

    async def _acquire(self, timeout_s: float) -> asyncio.Lock:
        """Take the socket lock within ``timeout_s`` or raise TimeoutError.

        Never ``wait_for(lock.acquire())``: on Python 3.11 a timeout landing
        as the acquire completes leaves the lock held with nobody to release
        it. Here a late acquire is released by its own callback, and a lock
        held past ``HA_SOCKET_LOCK_STALE_S`` is replaced first.
        """
        held = self._held_since
        if self._lock.locked() and (held is None or time.monotonic() - held > HA_SOCKET_LOCK_STALE_S):
            # Locked with no holder on record (a leaked acquire) or one far
            # past any real holder's bound.
            logger.warning("⚠️ HA intent socket lock stuck%s: replacing the lock and the socket",
                           f" for {time.monotonic() - held:.0f}s" if held is not None else " with no holder")
            self._lock = asyncio.Lock()
            self._held_since = None
            ws, self._ws = self._ws, None
            if ws is not None:
                asyncio.ensure_future(self._close_quietly(ws))
        lock = self._lock
        acquire = asyncio.ensure_future(lock.acquire())

        def _undo(fut):
            if not fut.cancelled() and fut.exception() is None:
                lock.release()

        try:
            done, _ = await asyncio.wait({acquire}, timeout=timeout_s)
        except BaseException:
            acquire.cancel()
            acquire.add_done_callback(_undo)
            raise
        if acquire in done:
            acquire.result()
            return lock
        acquire.cancel()
        acquire.add_done_callback(_undo)
        raise asyncio.TimeoutError

    def _release(self, lock: asyncio.Lock) -> None:
        if lock is self._lock:
            self._held_since = None
        lock.release()

    @contextlib.asynccontextmanager
    async def _held(self, timeout_s: float):
        lock = await self._acquire(timeout_s)
        self._held_since = time.monotonic()
        try:
            yield
        finally:
            self._release(lock)

    @staticmethod
    async def _close_quietly(ws) -> None:
        try:
            await asyncio.wait_for(ws.close(), 5.0)
        except BaseException:
            pass

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def request(self, msg_id: int, text: str, device_id: Optional[str] = None) -> dict:
        """The debug (recognise-only) request for one sentence."""
        payload = {"id": msg_id, "type": "conversation/agent/homeassistant/debug",
                   "sentences": [text], "language": self.language}
        if device_id and self._device_id_supported:
            payload["device_id"] = str(device_id)
        return payload

    async def _ask(self, ws, text: str, device_id: Optional[str]) -> dict:
        self._id += 1
        msg_id = self._id
        await ws.send(json.dumps(self.request(msg_id, text, device_id)))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == msg_id:
                return reply

    async def _recognize(self, text: str, device_id: Optional[str] = None) -> Optional[LocalMatch]:
        ws = await self._socket()
        reply = await self._ask(ws, text, device_id)
        if (not reply.get("success") and device_id and self._device_id_supported
                and (reply.get("error") or {}).get("code") == "invalid_format"):
            # An older Core without device context on the debug command: ask
            # again without it. Unnamed targets are refused below either way.
            self._device_id_supported = False
            logger.info("ℹ️ HA intent debug does not accept device_id; recognising without room context")
            reply = await self._ask(ws, text, None)
        if not reply.get("success"):
            return None
        results = (reply.get("result") or {}).get("results") or []
        res = results[0] if results else None
        if isinstance(res, dict) and (res.get("intent") or {}).get("name"):
            # What Home Assistant heard, also when it found no target: the
            # backend is told, instead of starting from nothing (0.18.30).
            heard = {str(k): (v.get("value") if isinstance(v, dict) else v)
                     for k, v in {**(res.get("slots") or {}), **(res.get("unmatched_slots") or {})}.items()}
            self.last_heard = {"intent": str(res["intent"]["name"]), "slots": heard,
                               "targets": len(res.get("targets") or {}),
                               "unmatched": sorted((res.get("unmatched_slots") or {}).keys())}
        if not isinstance(res, dict) or not res.get("match") or res.get("unmatched_slots"):
            return None
        slots = {}
        for key, value in (res.get("slots") or {}).items():
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, (str, int, float)):
                slots[str(key)] = value
        targets = res.get("targets") or {}
        domains = sorted({str(entity).split(".", 1)[0] for entity in targets})
        return LocalMatch(intent=str((res.get("intent") or {}).get("name") or ""), slots=slots,
                          domains=domains, targets=len(targets))


# Why a local match was refused. A refusal never acts: the request goes to
# the backend (the guarded path, with the full tool set and the action gate).
REFUSED_NO_MATCH = "no_match"
REFUSED_INTENT = "intent_not_local"
REFUSED_UNRESOLVED = "unresolved_target"
REFUSED_UNSAFE_DOMAIN = "unsafe_domain"
REFUSED_UNNAMED = "unnamed_target"
REFUSED_TOO_MANY = "too_many_targets"
REFUSED_NO_TOOL = "tool_not_offered"
# Refusals about WHICH device was meant: if the backend is unreachable too,
# the user hears that, never a generic failure and never an action.
TARGET_REFUSALS = frozenset({REFUSED_UNRESOLVED, REFUSED_UNNAMED, REFUSED_TOO_MANY})
UNCLEAR_TARGET_TEXT = "I couldn't tell which device you meant."


def _named(value: Any) -> bool:
    return value is not None and bool(str(value).strip())


def local_refusal(match: Optional[LocalMatch], tool_names: List[str]) -> Optional[str]:
    """Why a local match may not act (one of the REFUSED_* reasons), or None."""
    if match is None:
        return REFUSED_NO_MATCH
    bare = LOCAL_INTENTS.get(match.intent)
    if not bare:
        return REFUSED_INTENT
    if match.intent not in NO_TARGET_INTENTS:
        if match.targets < 1 or not match.domains:
            return REFUSED_UNRESOLVED
        if any(domain not in SAFE_LOCAL_DOMAINS for domain in match.domains):
            return REFUSED_UNSAFE_DOMAIN
        # A name, or an area (spoken, or Home Assistant's room context for
        # the asking satellite). A bare domain ("turn off the lights" with no
        # room) would fan out to every exposed light.
        if not (_named(match.slots.get("name")) or _named(match.slots.get("area"))):
            return REFUSED_UNNAMED
        if match.targets > LOCAL_MAX_TARGETS:
            return REFUSED_TOO_MANY
    if not any(n.rsplit("__", 1)[-1] == bare for n in tool_names):
        return REFUSED_NO_TOOL
    return None


def local_tool_call(match: Optional[LocalMatch], tool_names: List[str]) -> Optional[Tuple[str, dict]]:
    """The one tool call a confident local match maps to, or None (refused)."""
    if local_refusal(match, tool_names) is not None:
        return None
    bare = LOCAL_INTENTS[match.intent]
    tool = next(n for n in tool_names if n.rsplit("__", 1)[-1] == bare)
    if match.intent in NO_TARGET_INTENTS:
        return tool, {}
    return tool, dict(match.slots)


def describe_local(intent: str, slots: Dict[str, Any]) -> str:
    """A verified sentence for a successful local action."""
    target = str(slots.get("name") or slots.get("area") or "it")
    if intent in ("HassListAddItem", "HassListCompleteItem") and slots.get("item"):
        verb = _PAST[intent]
        return f"{verb.capitalize()} {slots['item']} {'to' if intent == 'HassListAddItem' else 'on'} {target}."
    verb = _PAST.get(intent, "did that for")
    extra = ""
    if intent == "HassLightSet" and "brightness" in slots:
        extra = f" to {slots['brightness']} percent"
    return f"{verb.capitalize()} {target}{extra}."


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------

PostFn = Callable[[dict], Awaitable[dict]]


class BackendRouter:
    """A Responses backend with the full tool set, run by the app."""

    def __init__(self, api_key: str, *, model: str, instructions: str, tools: List[dict],
                 service_tier: Optional[str] = ROUTER_SERVICE_TIER,
                 reasoning_effort: Optional[str] = ROUTER_REASONING_EFFORT,
                 max_output_tokens: int = ROUTER_MAX_OUTPUT_TOKENS,
                 cache_key: str = "voicepe-router", max_steps: int = ROUTER_MAX_STEPS,
                 timeout_s: float = ROUTER_CALL_TIMEOUT_S, post: Optional[PostFn] = None,
                 on_usage: Optional[Callable[[dict], None]] = None,
                 static_context: Optional[Callable[[], str]] = None):
        self.api_key = api_key
        self.model = model
        self.instructions = instructions
        # What the home has (app/ha_household.py), appended to the
        # instructions: it changes only when Home Assistant does, so it stays
        # in the cached prefix.
        self.static_context = static_context
        # Converted once and never reordered: the prefix the cache keys on.
        self.tools = [responses_function_tool(t) for t in tools]
        self.tool_names = [t["name"] for t in self.tools]
        self.service_tier = service_tier
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.cache_key = cache_key
        self.max_steps = max(1, int(max_steps))
        self.timeout_s = timeout_s
        self._post = post or self._http_post
        self.on_usage = on_usage
        self._client = None

    def body(self, input_items: List[dict]) -> dict:
        """The request, stable fields first and the volatile input last."""
        instructions = self.instructions
        if self.static_context is not None:
            try:
                extra = (self.static_context() or "").strip()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"router static context failed: {e!r}")
                extra = ""
            if extra:
                instructions += "\n\n" + extra
        body: Dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "tools": self.tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        if self.service_tier:
            body["service_tier"] = self.service_tier
        if self.cache_key:
            body["prompt_cache_key"] = self.cache_key
        body["store"] = False
        body["max_output_tokens"] = self.max_output_tokens
        body["input"] = input_items
        return body

    async def _http_post(self, body: dict) -> dict:  # pragma: no cover - the real API
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        response = await self._client.post(
            RESPONSES_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        if response.status_code >= 400:
            raise RuntimeError(f"backend HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def usage_usd(self, usage: dict, tier: Optional[str]) -> float:
        inp = int(usage.get("input_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        usd = ((inp - cached) * USD_PER_INPUT_TOKEN + cached * USD_PER_CACHED_INPUT_TOKEN
               + out * USD_PER_OUTPUT_TOKEN)
        if tier in ("priority", "fast"):
            usd *= PRIORITY_MULTIPLIER
        return usd

    async def run(self, input_items: List[dict], execute: ExecuteFn
                  ) -> Tuple[str, List[ToolRun], List[dict], List[dict]]:
        """Run the tool loop. Returns (final text, tool runs, step stats, new items).

        ``new items`` are the function calls, their outputs and the final
        assistant message, for the caller's history.
        """
        items = list(input_items)
        runs: List[ToolRun] = []
        steps: List[dict] = []
        new_items: List[dict] = []
        for step in range(self.max_steps):
            started = time.monotonic()
            data = await asyncio.wait_for(self._post(self.body(items)), self.timeout_s)
            usage = data.get("usage") or {}
            stat = {
                "s": round(time.monotonic() - started, 2),
                "in": int(usage.get("input_tokens") or 0),
                "cached": int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                "out": int(usage.get("output_tokens") or 0),
                "tier": data.get("service_tier"),
                "usd": round(self.usage_usd(usage, data.get("service_tier")), 6),
            }
            if self.on_usage is not None:
                try:
                    self.on_usage(stat)
                except Exception as e:  # pragma: no cover - defensive
                    logger.debug(f"router usage observer failed: {e!r}")
            output = data.get("output") or []
            calls = [o for o in output if o.get("type") == "function_call"]
            stat["calls"] = [c.get("name") for c in calls]
            steps.append(stat)
            if data.get("status") == "failed":
                raise RuntimeError(f"backend response failed: {(data.get('error') or {}).get('message')}")
            if not calls:
                text = "".join(
                    part.get("text", "") for o in output if o.get("type") == "message"
                    for part in o.get("content") or [] if isinstance(part, dict))
                new_items.append({"role": "assistant", "content": text.strip() or ""})
                return text.strip(), runs, steps, new_items
            call_items = [{"type": "function_call", "call_id": c.get("call_id"), "name": c.get("name"),
                           "arguments": c.get("arguments") or "{}"} for c in calls]
            items.extend(call_items)
            new_items.extend(call_items)
            outputs = await self._execute_step(calls, execute, runs)
            items.extend(outputs)
            new_items.extend(outputs)
        return "", runs, steps, new_items

    async def _execute_step(self, calls: List[dict], execute: ExecuteFn,
                            runs: List[ToolRun]) -> List[dict]:
        """Run one step's calls concurrently; an identical call runs once."""
        unique: Dict[str, asyncio.Task] = {}
        keyed = []
        for call in calls:
            try:
                arguments = json.loads(call.get("arguments") or "{}")
            except ValueError:
                arguments = {"_invalid_arguments": call.get("arguments")}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            key = json.dumps([call.get("name"), arguments], sort_keys=True, default=str)
            if key not in unique:
                unique[key] = asyncio.ensure_future(execute(str(call.get("name")), arguments))
            keyed.append((call, key))
        await asyncio.gather(*unique.values(), return_exceptions=True)
        outputs = []
        seen = set()
        for call, key in keyed:
            task = unique[key]
            if task.exception() is not None:
                result, outcome = {"error": f"{call.get('name')} could not be run; do not retry."}, None
            else:
                result, outcome = task.result()
            if key not in seen:
                seen.add(key)
                runs.append(tool_run(str(call.get("name")), outcome))
            outputs.append({"type": "function_call_output", "call_id": call.get("call_id"),
                            "output": result if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False, default=str)})
        return outputs


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------

class TurnRouter:
    """Route one utterance: local intent first, else the backend."""

    def __init__(self, backend: BackendRouter, recognizer: Optional[HaIntentRecognizer] = None,
                 context: Optional[Callable[[], str]] = None,
                 history_turns: int = ROUTER_HISTORY_TURNS,
                 device_id: Optional[str] = None, household=None,
                 satellite_ip: Optional[str] = None):
        self.backend = backend
        self.recognizer = recognizer
        self.context = context
        # The satellite this router serves (its address). Its room comes from
        # the household directory (app/ha_household.py), never from Home
        # Assistant's own room context: the debug command filled "the lights"
        # with whichever room it last resolved, for every device (0.18.30).
        self.device_id = device_id or None
        self.household = household
        # The satellite's network address, for its MAC (the device id may be
        # a configured name rather than the address).
        self.satellite_ip = satellite_ip or None
        self.history_turns = max(0, int(history_turns))
        self._history: List[List[dict]] = []
        # The latest utterance's intent-engine outcome, for the fast chat
        # path's veto: (text, decided, {"intent": str or None}).
        self._recognition: Optional[Tuple[str, asyncio.Event, Dict[str, Any]]] = None

    async def intent_matched(self, text: str, wait_s: float) -> Optional[bool]:
        """Did Home Assistant's intent engine match ``text``? None when it is
        still deciding after ``wait_s`` (or has not seen this text yet)."""
        if self.recognizer is None or not getattr(self.recognizer, "available", False):
            return False
        text = (text or "").strip()
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            rec = self._recognition
            if rec is not None and rec[0] == text:
                try:
                    await asyncio.wait_for(rec[1].wait(), max(0.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return None
                return bool(rec[2].get("intent"))
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)

    def note_assistant(self, text: str) -> None:
        """The voice said something on its own (chat); keep it for follow-ups."""
        text = (text or "").strip()
        if text and self._history:
            self._history[-1].append({"role": "assistant", "content": text})

    def reset(self) -> None:
        self._history.clear()

    def note_chat(self, user_text: str) -> None:
        """A turn answered as chat without the backend (the fast classifier):
        keep the user's words so a follow-up has its context."""
        user_text = (user_text or "").strip()
        if user_text:
            self._remember(user_text, [])

    def last_assistant(self) -> str:
        """What the assistant said last in this conversation, or ""."""
        for turn in reversed(self._history):
            for item in reversed(turn):
                if item.get("role") == "assistant" and item.get("content"):
                    return str(item["content"])
            return ""
        return ""

    def _input(self, user_text: str, heard: str = "") -> List[dict]:
        items: List[dict] = []
        for turn in self._history[-self.history_turns:] if self.history_turns else []:
            items.extend(turn)
        note = ""
        if self.context is not None:
            try:
                note = (self.context() or "").strip()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"router context failed: {e!r}")
        room = self.room()
        if room is not None:
            note = (note + "\n" if note else "") + (
                f"The user is speaking to the voice satellite in the {room.name}.")
        if heard:
            note = (note + "\n" if note else "") + heard
        if note:
            items.append({"role": "developer", "content": note})
        items.append({"role": "user", "content": user_text})
        return items

    def room(self):
        """The asking satellite's room, or None when unknown."""
        if self.household is None:
            return None
        try:
            return self.household.room_of(self.satellite_ip)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"router room lookup failed: {e!r}")
            return None

    def _ha_device_id(self) -> Optional[str]:
        """Home Assistant's id for the asking satellite, else its address."""
        if self.household is not None:
            try:
                found = self.household.device_id_of(self.satellite_ip)
            except Exception:  # pragma: no cover - defensive
                found = None
            if found:
                return found
        return self.device_id

    async def _own_room(self, user_text: str, match: Optional[LocalMatch],
                        timings: Dict[str, Any]) -> Optional[LocalMatch]:
        """A match whose room was not spoken acts on the asking satellite's
        room, or not at all. Home Assistant's debug command fills an unspoken
        area with whichever room it resolved last, for any device (verified
        2026-10-13: "turn on the lights" gave Kitchen, then Workshop, for
        both satellites and for none)."""
        if (self.household is None or match is None or _named(match.slots.get("name"))
                or not _named(match.slots.get("area"))):
            return match
        area = str(match.slots["area"])
        spoken = mentions(user_text, area)
        if not spoken and self.household is not None:
            said = self.household.spoken_room(user_text)
            spoken = said is not None and fold(said.name) == fold(area)
        if spoken:
            return match
        room = self.room()
        if room is None:
            timings["room_context"] = "unknown"
            return None
        if fold(room.name) == fold(area):
            timings["room_context"] = room.name
            return match
        # Ask Home Assistant again with the room said out loud.
        timings["room_context"] = f"{room.name} (HA said {area})"
        again = await self.recognizer.recognize(
            f"{user_text.rstrip(' .?!')} in the {room.name}", device_id=self._ha_device_id())
        if again is not None and fold(str(again.slots.get("area") or "")) == fold(room.name):
            return again
        return None

    def _heard_note(self, heard_info: Optional[Dict[str, Any]], refused: Optional[str]) -> str:
        """Tell the backend what Home Assistant's own parser heard."""
        if not heard_info or heard_info.get("question"):
            return ""
        slots = ", ".join(f"{k}={v!r}" for k, v in sorted((heard_info.get("slots") or {}).items()) if v)
        detail = (f"; unmatched: {', '.join(heard_info['unmatched'])}" if heard_info.get("unmatched")
                  else f"; {heard_info.get('targets', 0)} matching devices")
        # A hint only, never an instruction: the parser forces a question
        # into a command shape ("Is boost heat downstairs on?" parsed as
        # HassTurnOn), and "this is a request" made the backend turn it on.
        return (f"Home Assistant's parser guessed {heard_info.get('intent')} ({slots}){detail}"
                + (f"; not run locally ({refused})" if refused else "") +
                ". A hint only: decide from what the user said.")

    async def route(self, user_text: str, execute: ExecuteFn) -> RouteResult:
        started = time.monotonic()
        timings: Dict[str, Any] = {}
        user_text = (user_text or "").strip()
        refused: Optional[str] = None
        heard_info: Optional[Dict[str, Any]] = None
        if self.recognizer is not None and self.recognizer.available:
            t0 = time.monotonic()
            rec = (user_text, asyncio.Event(), {"intent": None})
            self._recognition = rec
            try:
                match = await self.recognizer.recognize(user_text, device_id=self._ha_device_id())
                heard_info = getattr(self.recognizer, "last_heard", None)
                if heard_info and is_state_question(user_text):
                    heard_info = dict(heard_info, question=True)
                # Any recognised intent (also one refused locally below) means
                # a command: the fast chat path is vetoed (v35 merge).
                rec[2]["intent"] = (getattr(match, "intent", None) if match is not None
                                    else (heard_info or {}).get("intent"))
            finally:
                rec[1].set()
            match = await self._own_room(user_text, match, timings)
            timings["ha_intent_ms"] = int((time.monotonic() - t0) * 1000)
            refused = local_refusal(match, self.backend.tool_names)
            if refused not in (None, REFUSED_NO_MATCH):
                # Recognised but not safe to run locally: nothing runs here;
                # the backend (the guarded path) decides or asks.
                timings["local_refused"] = refused
            call = local_tool_call(match, self.backend.tool_names)
            if call is not None:
                name, arguments = call
                timings["local_intent"] = match.intent
                t1 = time.monotonic()
                result, outcome = await execute(name, arguments)
                timings["local_tool_ms"] = int((time.monotonic() - t1) * 1000)
                run = tool_run(name, outcome)
                if run.ok and not run.pending_confirmation and not run.pending:
                    if match.intent in NO_TARGET_INTENTS:
                        text = "Current " + ("time" if match.intent == "HassGetCurrentTime" else "date") + \
                            " from the clock: " + _short(result)
                        kind = LOOKUP
                    else:
                        text = describe_local(match.intent, match.slots)
                        kind = ACTION
                    self._remember(user_text, [
                        {"role": "assistant", "content": text}])
                    timings["route_ms"] = int((time.monotonic() - started) * 1000)
                    return RouteResult(kind, text, "ha_local", [run], timings)
                # A local call that did not succeed (or now needs a yes) is
                # handed to the backend with the full tool set, so it can ask,
                # explain, or try what the user meant. The repeat ledger in the
                # caller refuses an identical second attempt.
                timings["local_fallthrough"] = run.kind
        direct = person_tool(user_text, self.backend.tool_names)
        if direct is not None:
            # "Where is X?" has a direct Home Assistant tool (an ETA intent
            # script reading the car's navigation). It answers in a second or
            # two; the general assistant takes 30-120 s for the same question.
            t1 = time.monotonic()
            result, outcome = await execute(direct, {})
            timings["local_intent"] = "person"
            timings["local_tool_ms"] = int((time.monotonic() - t1) * 1000)
            run = tool_run(direct, outcome)
            spoken = _spoken_text(result)
            if run.ok and not run.pending and spoken:
                text = "From the home's own location tool: " + spoken
                self._remember(user_text, [{"role": "assistant", "content": text}])
                timings["route_ms"] = int((time.monotonic() - started) * 1000)
                return RouteResult(LOOKUP, text, "ha_direct", [run], timings)
            timings["local_fallthrough"] = run.kind
        clock = clock_tool(user_text, self.backend.tool_names)
        if clock is not None:
            # A short time or date question, however the transcript cut it
            # ("What time"), is answered by the clock tool, not a guess.
            t1 = time.monotonic()
            result, outcome = await execute(clock, {})
            timings["local_intent"] = "clock"
            timings["local_tool_ms"] = int((time.monotonic() - t1) * 1000)
            run = tool_run(clock, outcome)
            if run.ok and not run.pending:
                text = "Current date and time from the clock: " + _short(result)
                self._remember(user_text, [{"role": "assistant", "content": text}])
                timings["route_ms"] = int((time.monotonic() - started) * 1000)
                return RouteResult(LOOKUP, text, "clock", [run], timings)
        t2 = time.monotonic()
        if is_state_question(user_text):
            # A state question never changes the house (v35 rig: "Is the test
            # light on?" ran HassLightSet). Household-control calls are
            # refused unrun; the backend answers from a read instead.
            execute = _read_only_execute(execute)
        try:
            text, runs, steps, new_items = await self.backend.run(
                self._input(user_text, self._heard_note(heard_info, refused)), execute)
        except Exception as e:
            timings["backend_error"] = repr(e)[:160]
            timings["route_ms"] = int((time.monotonic() - started) * 1000)
            logger.warning(f"⚠️ router backend failed: {e!r}")
            if refused in TARGET_REFUSALS:
                return RouteResult(FAILED, UNCLEAR_TARGET_TEXT, "backend", [], timings)
            return RouteResult(FAILED, "The assistant could not reach its backend.", "backend", [], timings)
        timings["backend_ms"] = int((time.monotonic() - t2) * 1000)
        timings["backend_steps"] = steps
        refused_runs = [r for r in runs if r.kind == READ_ONLY_REFUSED]
        if refused_runs:
            timings["read_only_refused"] = [r.name for r in refused_runs]
            runs = [r for r in runs if r.kind != READ_ONLY_REFUSED]
        timings["route_ms"] = int((time.monotonic() - started) * 1000)
        self._remember(user_text, new_items)
        upper = text.strip().upper().rstrip(".")
        if not runs and upper == NO_REQUEST:
            if heard_info:
                # Home Assistant parsed a command, so it was one (16:22: a
                # clean "Turn on the kitchen lights" was judged NO_REQUEST and
                # the user heard 20 s of nothing). Say it was not done.
                timings["no_request_vetoed"] = heard_info.get("intent")
                logger.warning("⛔ router: backend said NO_REQUEST for a parsed command; telling the user")
                return RouteResult(FAILED, UNCLEAR_TARGET_TEXT, "backend", [], timings)
            return RouteResult(NOTHING, "", "backend", [], timings)
        if not runs and (upper == CHAT or not text):
            return RouteResult(CHAT_KIND, "", "backend", [], timings)
        if not runs:
            # A clarifying question, or a remark from the conversation so far.
            # With no tool behind it, it may not claim that anything happened.
            # A read-only question answered from what the backend already
            # knows ("The front door is locked.") is a remark, not a claim;
            # for anything else an asserted outcome is not confirmed.
            if claims_success(text) and (is_action_shaped(user_text) or claims_own_action(text)
                                         or claims_action_for_question(text)):
                logger.warning("⛔ router: backend claimed an outcome with no tool this turn; not spoken")
                return RouteResult(FAILED, "That could not be confirmed, so it may not have been done.",
                                   "backend", [], timings)
            return RouteResult(REPLY, text, "backend", [], timings)
        if any(r.pending_confirmation for r in runs):
            return RouteResult(CONFIRM, text, "backend", runs, timings)
        if any(r.pending for r in runs):
            # The work goes on elsewhere: the voice may promise a report,
            # never a result (app/live_tasks.py delivers the real one).
            return RouteResult(PENDING, text if not claims_success(text) else "", "backend", runs, timings)
        if any(r.deferred for r in runs) or (
                is_deferred_request(user_text) and any(r.ok and _is_watch_tool(r.name) for r in runs)):
            # A watch is armed; the answer comes when the event does. The
            # session is not held for it (app/live_tasks.py).
            for run in runs:
                if run.ok and (run.deferred or _is_watch_tool(run.name)):
                    run.deferred = True
            return RouteResult(PENDING, "", "backend", runs, timings)
        if not any(r.ok for r in runs):
            return RouteResult(FAILED, text, "backend", runs, timings)
        actions = [r for r in runs if _is_action_tool(r.name)]
        if actions and not any(r.ok for r in actions):
            # Every action failed; a successful read does not make it done.
            return RouteResult(FAILED, text, "backend", runs, timings)
        if not any(r.ok and _is_action_tool(r.name) for r in runs) and (
                (claims_action(text) or (_HOUSEHOLD_VERB.search(user_text or "") and claims_success(text)))
                if is_action_shaped(user_text) else claims_action_for_question(text)):
            # haintent review (v35 merge): for a request with a household verb
            # ("Did you turn off the lights?") any asserted outcome counts,
            # not only "I ..." (a read cannot verify "They're turned off.").
            # Only reads succeeded, yet the text says something was done.
            logger.warning("⛔ router: backend claimed an outcome with no tool this turn "
                           "(only read tools succeeded); not spoken as verified")
            return RouteResult(FAILED, "That could not be confirmed, so it may not have been done.",
                               "backend", runs, timings)
        actionish = any(_is_action_tool(r.name) for r in runs)
        return RouteResult(ACTION if actionish else LOOKUP, text, "backend", runs, timings)

    def _remember(self, user_text: str, items: List[dict]) -> None:
        if not self.history_turns:
            return
        self._history.append([{"role": "user", "content": user_text}] + list(items))
        del self._history[:-self.history_turns]


_CLOCK = re.compile(
    r"^(?:hey\s+\w+[,.]?\s+)?(?:so\s+|ok(?:ay)?[,]?\s+)?(?:what(?:'s|\s+is)?\s+(?:the\s+)?"
    r"(?:time|date|day)(?:\s+(?:is\s+it|it\s+is|now|today|right\s+now))?"
    r"|what\s+day\s+is\s+(?:it|today)|what\s+time\s+is\s+it|what's\s+today's\s+date)"
    r"\s*(?:please)?\s*[?.!]?$", re.I)


def clock_tool(text: str, tool_names: List[str]) -> Optional[str]:
    """The date/time read tool for a short clock question, else None."""
    if not _CLOCK.match((text or "").strip()):
        return None
    return next((n for n in tool_names if n.rsplit("__", 1)[-1] == "GetDateTime"), None)


# "Where is X" / "when will X be home": a direct tool of the home answers it.
_PERSON = re.compile(
    r"^(?:hey[,.]?\s+(?:\w+[,.]?\s+)?)?(?:so\s+|ok(?:ay)?[,]?\s+)?(?:"
    r"where(?:'s|\s+is)\s+(?P<a>[A-Za-z][A-Za-z'-]*)(?:\s+(?:right\s+)?now|\s+at\s+the\s+moment)?"
    r"|(?:when|what\s+time)\s+(?:is|will|does)\s+(?P<b>[A-Za-z][A-Za-z'-]*)\s+(?:be\s+|get\s+|getting\s+|coming\s+|arriving\s+|arrive\s+)?(?:back\s+)?(?:home|here|back|in)\b"
    r"|is\s+(?P<c>[A-Za-z][A-Za-z'-]*)\s+(?:home|back|on\s+(?:her|his|their)\s+way|coming\s+home)\b"
    r"|how\s+far\s+(?:away\s+)?is\s+(?P<d>[A-Za-z][A-Za-z'-]*)\b"
    r")\s*\??\s*(?:please)?\s*[?.!]?$", re.I)
_PERSON_TOOL = re.compile(r"(?:^|__)(?:intent_script__)?(?P<n>[A-Za-z]+)ETA$|(?:^|__)(?:script__)?\w*answer_(?P<m>[a-z]+)_eta\w*$", re.I)


def person_tool(text: str, tool_names: List[str]) -> Optional[str]:
    """The direct location/ETA tool for the person named in a "where is X"
    question, or None. Prefers an intent script (answers at once) over a
    builder script; never guesses when the name does not match a tool."""
    match = _PERSON.match((text or "").strip())
    if not match:
        return None
    person = next((g for g in match.groups() if g), "").lower().strip("'-")
    if not person or person in ("it", "that", "this", "she", "he", "the", "my", "everyone", "everybody"):
        return None
    intent, script = None, None
    for name in tool_names:
        m = _PERSON_TOOL.search(name)
        if not m:
            continue
        who = (m.group("n") or m.group("m") or "").lower()
        if who != person:
            continue
        if "intent_script" in name and intent is None:
            intent = name
        elif script is None:
            script = name
    return intent or script


_WATCH_TOOL = re.compile(r"(?:^|__)\w*(?:watch|notify|alert_when|tell_when|when_)\w*$", re.I)


def _is_watch_tool(name: str) -> bool:
    return bool(_WATCH_TOOL.search(name or ""))


def _spoken_text(result: Any) -> str:
    """The sentence a direct tool handed back, whatever shape it used."""
    if isinstance(result, str):
        text = result.strip()
        if text[:1] in ("{", "["):
            try:
                result = json.loads(text)
            except ValueError:
                return text
        else:
            return text
    if isinstance(result, dict):
        for key in ("speech", "answer", "response", "text", "message", "result"):
            value = result.get(key)
            if isinstance(value, dict):
                value = value.get("plain", value.get("speech"))
                if isinstance(value, dict):
                    value = value.get("speech")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


_SUCCESS_CLAIM = re.compile(
    r"\b(?:done|turned|switched|added|removed|set|locked|unlocked|opened|closed|started|stopped|"
    r"paused|resumed|cancel(?:l)?ed|is now|are now|all set|"
    # household-context review (v35 merge): "the alarm is armed", "already closed".
    r"armed|disarmed|already)\b", re.I)


def claims_success(text: str) -> bool:
    """Does a tool-less backend reply assert that something happened?"""
    return bool(_SUCCESS_CLAIM.search(text or "")) and not (text or "").strip().endswith("?")


_ACTION_CLAIM = re.compile(
    r"\b(?:done|all set|i've|i have|i(?: just)? (?:turned|switched|added|removed|set|locked|unlocked|"
    r"opened|closed|started|stopped|paused|cancel(?:l)?ed)|(?:is|are) now)\b", re.I)


def claims_action(text: str) -> bool:
    """Does a reply backed only by reads say the assistant did something?
    A state answer ("The door is locked.") is not such a claim."""
    return bool(_ACTION_CLAIM.search(text or ""))


# The assistant saying IT did (or will do) something. "is now"/"are now" are
# left out: in an answer to a question they describe state ("It is now 21").
_OWN_ACTION_CLAIM = re.compile(
    r"\b(?:done|all set|i've|i have|i'll|i will|i(?: just)? (?:turned|switched|added|removed|set|locked|"
    r"unlocked|opened|closed|started|stopped|paused|resumed|cancel(?:l)?ed|checked|put|sent|played|muted))\b",
    re.I)


def claims_own_action(text: str) -> bool:
    """Does a reply say the assistant itself did (or will do) something?"""
    return bool(_OWN_ACTION_CLAIM.search((text or "").replace("’", "'")))


# "is now"/"are now" before a reading ("It is now 21 degrees") describes
# state. Before anything else ("The lights are now off") it stays a claim.
_NOW_READING = re.compile(r"\b(?:is|are)\s+now\s+(?=(?:\d|about\b|around\b|at\b|reading\b|showing\b))", re.I)


def claims_action_for_question(text: str) -> bool:
    """claims_action for a reply backed only by reads to a pure state
    question: identical, except that "is now <a reading>" is not a claim."""
    return claims_action(_NOW_READING.sub(" ", text or ""))


# A question about state. "did/have/has" are not leads: "Did you turn off
# the lights?" asks about an action and stays protected.
_QUESTION_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|and|also|oh)\b[\s,]*)*"
    r"(?:what|what's|whats|where|where's|when|when's|which|who|who's|how|how's|is|isn't|are|aren't|"
    r"was|were|does|doesn't)\b", re.I)
_REQUEST_FORM = re.compile(
    r"\b(?:can|could|would|will|won't)\s+(?:you|u)\b|\bplease\b|\b(?:do|would)\s+you\s+mind\b|"
    r"\bhow\s+about\b|\bwhat\s+about\b|\bwhy\s+don't\s+you\b|\bwhat\s+if\b|\bis\s+it\s+possible\b|"
    r"\bany\s+way\b|\bare\s+you\s+able\b|\bif\s+(?:so|not)\b|\byou\s+get\s+a\s+chance\b", re.I)
# A household verb anywhere makes the utterance action-shaped (the
# service's _SILENT_TOOL_COMMAND list and more). Inflected forms count for
# the verbs whose participles are not ordinary state words; "locked",
# "closed", "opened" stay state words so "Is the door locked?" is a question.
_HOUSEHOLD_VERB = re.compile(
    r"\b(?:(?:turn|switch|add|remove|delete|complete|start|stop|cancel|play|pause|resume|mute|unmute|"
    r"dim|brighten|vacuum|remember|forget|make|bump|cool|heat|send|kill|remind|raise|lower|increase|"
    r"decrease|skip|shut|activate|deactivate|arm|disarm|clean|put|warm\s+up)"
    r"(?:s|es|ed|d|ing|ped|ping|ned|ning)?|set|setting|check\s+off|checked\s+off|lock|unlock|open|close)\b",
    re.I)


def is_read_only_question(text: str) -> bool:
    """A question about state ("what's the temperature", "is the door
    locked", "what's on the list") with no request in it anywhere. Strict:
    used for false-success protection, where treating a request as a
    question is the unsafe mistake ("Is the garage open?" stays protected)."""
    normalized = " ".join((text or "").replace("’", "'").split())
    if not normalized or _HOUSEHOLD_VERB.search(normalized) or _REQUEST_FORM.search(normalized):
        return False
    # Every clause (split on commas too) must itself be a question, after
    # any leading filler ("Hey, is the door locked?"). "Which lights are on,
    # lights off" is not.
    clauses = [s.strip() for s in re.split(r"[.?!;,]+", normalized) if s.strip()]
    while clauses and _FILLER.fullmatch(clauses[0]):
        clauses.pop(0)
    return bool(clauses) and all(_QUESTION_LEAD.match(c) for c in clauses)


def is_state_question(text: str) -> bool:
    """Lenient: may this utterance change the house? False only for a
    question. Used to refuse control tools, where treating a question as a
    request is the unsafe mistake. Adds to is_read_only_question every yes/no
    question opened by a form of "be", whatever nouns follow: a device NAME
    can hold a household word ("Is boost heat downstairs on?" turned Boost
    Heat on in the 0.18.34 Workshop real-audio trial). A clause that is not a
    question ("..., turn it off") still makes it a request."""
    if is_read_only_question(text):
        return True
    normalized = " ".join((text or "").replace("’", "'").split())
    if not normalized or _REQUEST_FORM.search(normalized):
        return False
    clauses = [s.strip() for s in re.split(r"[.?!;,]+", normalized) if s.strip()]
    while clauses and _FILLER.fullmatch(clauses[0]):
        clauses.pop(0)
    return bool(clauses) and all(
        _QUESTION_LEAD.match(c) and (not _HOUSEHOLD_VERB.search(c) or _BE_QUESTION.match(c)) for c in clauses)


# A yes/no state question: "is/are/was/were ... ?" (not "is it possible to").
_BE_QUESTION = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|and|also|oh)\b[\s,]*)*(?:is|isn't|are|aren't|was|were)\b(?!\s+you\b)", re.I)


_FILLER = re.compile(r"(?:(?:hey|hi|ok|okay|so|um|uh|oh|and|also|well)\s*)+", re.I)


def is_action_shaped(text: str) -> bool:
    """True unless the utterance is plainly a read-only question.

    Deliberately conservative: anything that is not a question about state
    ("kitchen lights off", "turn on the lamp", "can you lock the door",
    "which lights are on, turn them off", "did you add milk") counts as
    action-shaped, so a claimed outcome for it is never spoken as done. Any
    household verb anywhere makes it action-shaped; "Is the garage open?"
    therefore is too (a tool-less state answer to it is reported as not
    confirmed, the base behaviour)."""
    return not is_read_only_question(text)


_READ_TOOLS = re.compile(r"(?:^|__)(?:Get|get_|list_|recall|web_search|GetLiveContext|GetDateTime)", re.I)


def _is_action_tool(name: str) -> bool:
    return not _READ_TOOLS.search(name or "")


# Tools a read-only question may not run: Home Assistant intents that change
# state (every Hass* intent except the Get* reads) and confirm_action. Scripts
# and agent tools (ask_openclaw, ETA scripts) stay allowed: they answer
# questions.
_HOUSE_CONTROL_TOOL = re.compile(r"(?:^|__)(?:Hass(?!Get)|confirm_action$)", re.I)
READ_ONLY_REFUSED = "read_only_refused"


@dataclass
class _Refused:
    ok: bool = False
    kind: str = READ_ONLY_REFUSED


def _read_only_execute(execute: ExecuteFn) -> ExecuteFn:
    async def guarded(name: str, arguments: dict):
        if _HOUSE_CONTROL_TOOL.search(name or ""):
            logger.warning(f"⛔ router: {name} refused for a read-only question; nothing was changed")
            return ({"error": "The user asked a question; nothing may be changed. Not run. "
                              "Answer from a read tool (GetLiveContext) instead."}, _Refused())
        return await execute(name, arguments)
    return guarded


def _short(result: Any, limit: int = 300) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text[:limit]
