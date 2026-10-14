"""Background tasks for the GPT-Live router: a task list, not a slot.

A routed request that outlives a short soft budget, or that the user talks
over once its tool has started, is promoted to a background task. From then
on three things are guaranteed (audit 2026-10-12, improvement 1):

* a started tool is never cancelled and its result is never dropped: the
  operation finishes in the background and its verified result is delivered;
* an exact repeat of a pending request joins that operation instead of
  running the tool again (the impatient "turn on the lamp" said twice);
* the voice says briefly what it is doing and that it will report back, then
  the conversation continues: new utterances route as new operations.

Two kinds of long task (owner's definition, 2026-10-12), both answered by
VOICE on the same device:

1. **A slow lookup in a conversation** ("where is X?" taking 30 s). The
   conversation is active, so the session stays open while the lookup runs,
   up to a hard bound (``hold_session_s``, default 90 s): the voice
   acknowledges naturally and the user can keep talking. Past the bound the
   session closes on its ordinary rules and the result is delivered by a
   short new voice session or an announcement on the same device.
2. **A deferred, triggered request** ("tell me when X leaves work"). The
   watch is registered (a Home Assistant script or automation, or OpenClaw)
   and the session closes normally: no billed idle time while waiting,
   possibly for hours. When the event fires the result is delivered by
   voice on the same device, respecting quiet hours.

**Cost rule (binding).** Every session open goes through the spend gate and
the daily caps; nothing here opens one of its own accord. A deferred task
never holds a session; a lookup holds it only within the hard bound. An
iMessage through the OpenClaw bridge is a fringe fallback: used when the
device cannot be reached, or when the user asked for a text. Everything in
this module is pure logic: the service (``live_service.py``) owns the time,
the socket and the gate.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, List, Optional, Tuple

# Channels a finished result may be delivered through, in the operator's order.
SESSION = "session"      # an open, free Live session: one commentary append
REOPEN = "reopen"        # open a short new session through the spend gate and speak it
ANNOUNCE = "announce"    # the device's TTS announcement lane (the timer/announce path)
IMESSAGE = "imessage"    # a text through the OpenClaw bridge's verified iMessage sender
CHANNELS = (SESSION, REOPEN, ANNOUNCE, IMESSAGE)

# A lookup's result continues the conversation: a short new session first.
DEFAULT_ORDER = (SESSION, REOPEN, ANNOUNCE, IMESSAGE)
# A deferred result hours later is an announcement in the same voice.
DEFAULT_DEFERRED_ORDER = (SESSION, ANNOUNCE, REOPEN, IMESSAGE)

# Defaults, all overridable from the environment (add-on options).
PROMOTE_S = 3.5              # soft budget before a route becomes a background task
FAST_PROMOTE_S = 8.0         # 0.18.30: the budget when no slow tool (agent, web search) ran
HOLD_SESSION_S = 90.0        # a lookup keeps the open session at most this long (hard bound)
PROGRESS_EVERY_S = 25.0      # "still working on X", only while the user is not talking
PROGRESS_MAX = 3
PROGRESS_QUIET_S = 3.0       # no progress line this soon after the user spoke
IMESSAGE_AFTER_IDLE_S = 0.0  # optional: device idle this long -> text first (0 = off)
RESULT_TTL_S = 900.0         # an undeliverable lookup result is dropped (and logged) after this
DEFERRED_MAX_S = 86400.0     # a watch that never fires is forgotten after a day
REOPEN_WAIT_S = 12.0         # a reopen that produced no session by then falls through
SHORT_REQUEST_CHARS = 60


@dataclass(frozen=True)
class TaskPolicy:
    """Everything configurable about background tasks."""

    order: Tuple[str, ...] = DEFAULT_ORDER
    deferred_order: Tuple[str, ...] = DEFAULT_DEFERRED_ORDER
    promote_s: float = PROMOTE_S
    fast_promote_s: float = 0.0  # 0: same as promote_s; from_env gives FAST_PROMOTE_S
    hold_session_s: float = HOLD_SESSION_S
    progress_every_s: float = PROGRESS_EVERY_S
    progress_max: int = PROGRESS_MAX
    progress_quiet_s: float = PROGRESS_QUIET_S
    imessage_after_idle_s: float = IMESSAGE_AFTER_IDLE_S
    result_ttl_s: float = RESULT_TTL_S
    deferred_max_s: float = DEFERRED_MAX_S
    reopen_wait_s: float = REOPEN_WAIT_S
    imessage_to: str = ""  # destination alias known to the OpenClaw bridge
    quiet_hours: str = ""  # "22:00-07:00": no voice delivery of deferred results then

    @classmethod
    def from_env(cls, env=None) -> "TaskPolicy":
        env = os.environ if env is None else env

        def number(name, default, low):
            try:
                return max(low, float(env.get(name, "") or default))
            except (TypeError, ValueError):
                return default

        return cls(
            order=parse_order(env.get("LIVE_TASK_DELIVERY", "")),
            deferred_order=parse_order(env.get("LIVE_TASK_DEFERRED_DELIVERY", ""), DEFAULT_DEFERRED_ORDER),
            # The soft budget is never zero: a route that is not promoted
            # would hold the pre-result gate for its whole duration.
            promote_s=number("LIVE_TASK_PROMOTE_S", PROMOTE_S, 1.0),
            fast_promote_s=number("LIVE_TASK_FAST_PROMOTE_S", FAST_PROMOTE_S, 1.0),
            # The hard bound on how long a lookup may keep the open session.
            hold_session_s=number("LIVE_TASK_HOLD_SESSION_S", HOLD_SESSION_S, 0.0),
            progress_every_s=number("LIVE_TASK_PROGRESS_EVERY_S", PROGRESS_EVERY_S, 5.0),
            progress_max=int(number("LIVE_TASK_PROGRESS_MAX", PROGRESS_MAX, 0)),
            progress_quiet_s=number("LIVE_TASK_PROGRESS_QUIET_S", PROGRESS_QUIET_S, 0.0),
            imessage_after_idle_s=number("LIVE_TASK_IMESSAGE_AFTER_IDLE_S", IMESSAGE_AFTER_IDLE_S, 0.0),
            result_ttl_s=number("LIVE_TASK_RESULT_TTL_S", RESULT_TTL_S, 30.0),
            deferred_max_s=number("LIVE_TASK_DEFERRED_MAX_S", DEFERRED_MAX_S, 60.0),
            reopen_wait_s=number("LIVE_TASK_REOPEN_WAIT_S", REOPEN_WAIT_S, 2.0),
            imessage_to=(env.get("LIVE_TASK_IMESSAGE_TO", "") or "").strip(),
            quiet_hours=(env.get("LIVE_TASK_QUIET_HOURS", "") or "").strip(),
        )

    def plan(self, *, session_ready: bool, idle_s: float, available, deferred: bool = False,
             wants_text: bool = False, quiet: bool = False) -> List[str]:
        """The channels to try now, in order.

        ``available`` is a callable ``channel -> bool`` (is that route wired
        and usable right now). ``session`` is only ever first and only while a
        session is open. Voice is the core path; an iMessage comes first only
        when the user asked for a text, or (optional) when the device has been
        idle for ``imessage_after_idle_s``. During quiet hours no new voice
        route (reopen, announcement) is used for any result, deferred or not
        (longtask review iii): an open conversation may hear it, a text if one
        is configured, otherwise nothing (the caller waits for the end of
        quiet hours).
        """
        plan: List[str] = []
        for channel in (self.deferred_order if deferred else self.order):
            if channel == SESSION:
                if session_ready and available(SESSION):
                    plan.append(SESSION)
                continue
            if available(channel):
                plan.append(channel)
        text_first = wants_text or (self.imessage_after_idle_s > 0 and idle_s >= self.imessage_after_idle_s)
        if IMESSAGE in plan and text_first and not (plan and plan[0] == SESSION):
            plan.remove(IMESSAGE)
            plan.insert(0, IMESSAGE)
        if quiet:
            plan = [c for c in plan if c in (SESSION, IMESSAGE)]
        return plan


def parse_order(raw: str, default: Tuple[str, ...] = DEFAULT_ORDER) -> Tuple[str, ...]:
    """``"session,announce,imessage"`` -> a tuple of known channels, in order."""
    seen: List[str] = []
    for part in (raw or "").split(","):
        name = part.strip().lower()
        if name in CHANNELS and name not in seen:
            seen.append(name)
    return tuple(seen) if seen else default


_QUIET = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")


def in_quiet_hours(spec: str, now: Optional[datetime] = None) -> bool:
    """Is the local time inside ``"22:00-07:00"`` (a window may cross midnight)?
    An empty or unreadable spec is never quiet."""
    match = _QUIET.match(spec or "")
    if not match:
        return False
    start = int(match.group(1)) * 60 + int(match.group(2) or 0)
    end = int(match.group(3)) * 60 + int(match.group(4) or 0)
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def quiet_hours_end(spec: str, now: Optional[datetime] = None) -> float:
    """Seconds until the quiet window ends (0 when not quiet)."""
    if not in_quiet_hours(spec, now):
        return 0.0
    match = _QUIET.match(spec or "")
    end = int(match.group(3)) * 60 + int(match.group(4) or 0)
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    remaining = (end - minute) % (24 * 60)
    return max(60.0, remaining * 60.0 - now.second)


# "Tell me when X leaves work": the request itself says the answer comes later.
_DEFERRED = re.compile(
    r"\b(?:tell|let|notify|alert|remind|ping|warn)\s+(?:me|us)\s+(?:know\s+)?"
    r"(?:when|once|as soon as|if|the moment)\b|\bwatch\s+(?:for|when)\b|\bkeep an eye on\b", re.I)
# "Text me when ...", "send me a message ...": the user asked for a text.
_WANTS_TEXT = re.compile(
    r"\b(?:text|imessage|sms|message)\s+(?:me|us)\b|\bsend\s+(?:me|us)\s+(?:a\s+)?(?:text|message)\b"
    r"|\bby\s+(?:text|imessage|message)\b", re.I)


def is_deferred_request(text: str) -> bool:
    """A request whose answer comes when something happens, not now."""
    return bool(_DEFERRED.search(text or ""))


def wants_text(text: str) -> bool:
    """The user asked to be texted (the one case a text is the first route)."""
    return bool(_WANTS_TEXT.search(text or ""))


# ---------------------------------------------------------------------------
# request identity
# ---------------------------------------------------------------------------

_FILLERS = ("please", "hey", "ok", "okay", "so", "um", "uh", "can you", "could you", "would you")


def normalize_request(text: str) -> str:
    """The join key for "the same request": case, punctuation and politeness
    folded, nothing else. "Turn on the lamp." and "turn on the lamp please"
    join; "turn on the lamp" and "turn off the lamp" never do."""
    folded = unicodedata.normalize("NFKD", (text or "").replace("’", "'").lower())
    folded = folded.encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9']+", folded)
    text = " ".join(words)
    for filler in _FILLERS:
        text = re.sub(rf"(?:^|\s){filler}(?=\s|$)", " ", text)
    return " ".join(text.split())


def short_request(text: str, limit: int = SHORT_REQUEST_CHARS) -> str:
    """A short, speakable reference to the request ("the shopping list question")."""
    text = " ".join((text or "").split()).strip().rstrip(".!?,;:")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


# ---------------------------------------------------------------------------
# what the voice is told
# ---------------------------------------------------------------------------

def promotion_commentary(request: str) -> str:
    return ("This is taking longer than a moment. In a few words, tell the user you are still "
            f"working on it (their request: \"{short_request(request)}\") and that you will tell them "
            "as soon as it is done; they can talk to you or ask anything else meanwhile. "
            "Do not guess the result and do not say it is done.")


def progress_commentary(request: str) -> str:
    return (f"You are still working on the earlier request \"{short_request(request)}\" in the "
            "background. If it fits naturally, mention in a few words that you are still on it; "
            "otherwise say nothing. Do not guess the result.")


def joined_commentary(request: str) -> str:
    return (f"The user repeated a request you are already working on (\"{short_request(request)}\"). "
            "Nothing was started again. Say briefly that you are still on it and will report back; "
            "do not guess the result.")


def deferred_commentary(text: str = "") -> str:
    """The route result for a request that is set up to answer later."""
    return ("This is set up: the home will tell the user, by voice on this device, when it happens. "
            + (text + " " if text else "")
            + "Say that briefly in your own words; do not say it has happened and do not guess when.")


def result_commentary(kind: str, text: str, request: str, source: str = "",
                      deferred: bool = False) -> Optional[str]:
    """The commentary that delivers a finished background task into an open
    session. Mirrors ``RouteResult.commentary`` and adds the reference to the
    earlier request, so the voice frames it as the follow-up it is."""
    from app.live_router import ACTION, CHAT_KIND, CONFIRM, FAILED, LOOKUP, REPLY
    about = f"the earlier request \"{short_request(request)}\""
    text = (text or "").strip()
    if deferred and kind in (ACTION, LOOKUP) and text:
        return (f"The event the user asked to be told about has happened ({about}): {text} "
                "Tell the user now, briefly, in your own words; add nothing that is not stated here.")
    if kind in (ACTION, LOOKUP):
        who = "the household assistant" if source == "openclaw" else "the home system"
        return (f"Verified result from {who} for {about}: {text} Tell the user this now, briefly, in "
                "your own words, as the follow-up you promised; add nothing that is not stated here.")
    if kind == CONFIRM:
        return ("Nothing has been done yet for " + about + ": a spoken yes is required first. "
                "Ask the user this yes/no question and nothing else: " + text)
    if kind == REPLY:
        return ("Say this to the user briefly, as a question or remark, regarding " + about
                + "; nothing has been done: " + text)
    if kind == FAILED:
        return (f"{about[0].upper() + about[1:]} was not completed: "
                + (text or "it could not be carried out.")
                + " Tell the user briefly; do not say it was done.")
    if kind == CHAT_KIND:
        return f"No tool was needed for {about}. Answer it yourself now, briefly."
    return None


def spoken_result(kind: str, text: str, request: str, deferred: bool = False) -> Optional[str]:
    """One plain sentence for the announcement lane or a text message, where
    there is no voice model to word it."""
    from app.live_router import ACTION, CONFIRM, FAILED, LOOKUP, REPLY
    about = short_request(request)
    text = " ".join((text or "").split())
    if deferred and kind in (ACTION, LOOKUP, REPLY) and text:
        return f"You asked me to tell you, {about}: {text}"
    if kind in (ACTION, LOOKUP, REPLY) and text:
        return f"An update on your earlier request, {about}: {text}"
    if kind == CONFIRM:
        return (f"Your earlier request, {about}, still needs a spoken yes before anything is done. "
                "Ask me again when you are ready.")
    if kind == FAILED:
        return (f"Your earlier request, {about}, could not be completed"
                + (f": {text}" if text else ".") + " Nothing was done.")
    return None


# ---------------------------------------------------------------------------
# external completions (OpenClaw, Home Assistant) reach whichever service owns
# the task
# ---------------------------------------------------------------------------

ExternalSink = Callable[[str, str, bool], Awaitable[bool]]
_sinks: List[ExternalSink] = []


def register_sink(sink: ExternalSink) -> None:
    if sink not in _sinks:
        _sinks.append(sink)


def unregister_sink(sink: ExternalSink) -> None:
    try:
        _sinks.remove(sink)
    except ValueError:
        pass


async def deliver_external_result(task_id: str, text: str, ok: bool = True) -> bool:
    """Hand a finished external task to the service that is waiting for it.
    True when a pending task claimed it; False when nobody knows the id."""
    task_id = (task_id or "").strip()
    if not task_id:
        return False
    for sink in list(_sinks):
        try:
            if await sink(task_id, text, ok):
                return True
        except Exception:  # pragma: no cover - one bad sink never blocks the rest
            continue
    return False


@dataclass
class DeliveryAttempt:
    """Bookkeeping for one result's delivery, for logs and tests."""

    channel: str
    ok: bool
    detail: str = ""


@dataclass
class DeliveryLog:
    attempts: List[DeliveryAttempt] = field(default_factory=list)

    def note(self, channel: str, ok: bool, detail: str = "") -> None:
        self.attempts.append(DeliveryAttempt(channel, ok, detail))

    @property
    def delivered_via(self) -> Optional[str]:
        return next((a.channel for a in self.attempts if a.ok), None)
