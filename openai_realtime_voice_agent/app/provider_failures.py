"""What kind of broken is this, and what is the useful answer?

An engine can fail in ways that mean very different things. Out of money is
permanent until someone pays; a dropped socket is over in a second. Answering
both the same way is how a house ends up either silent or paying on the wrong
account for a day.

The classes below are ordered by how sure we are. MONEY and AUTH are checked
first because their phrasings often also contain the words a transient error
uses -- OpenAI delivers a spent quota as a 429 that reads like a rate limit.

If an error message contains no recognized pattern, it is classified as TRANSIENT.
Unrecognized errors are far more likely to be a provider fault (a new error type,
a temporary condition, a backend rollout) than an app fault. Defaulting to retry
is safer than defaulting to silence. Empty messages (nothing to act on) remain APP.
"""
import re
from enum import Enum


class Failure(str, Enum):
    """What to do about an error, not what caused it."""

    MONEY = "money"          # switch engine now; retrying cannot help
    AUTH = "auth"            # switch engine now; this engine will not accept us
    TRANSIENT = "transient"  # try once more on this engine, then switch
    RATE_LIMIT = "rate_limit"  # wait the stated seconds and ask again; never a strike
    APP = "app"              # our own fault; never switch


# Checked in this order. The first list that matches wins, so the permanent
# failures get to claim a message before the transient patterns see it.
# Numeric codes are matched with word boundaries to avoid false matches
# (e.g., "402" in a request ID or "500" in a duration).
_MONEY = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing hard limit",
    "billing_hard_limit_reached",
    "resource_exhausted",
    "quota exceeded",
)
_MONEY_CODES = (r"\b402\b",)
_AUTH = (
    "incorrect api key",
    "invalid_api_key",
    "api key not valid",
    "unauthorized",
    "permission_denied",
    "unauthenticated",
    # A retired preview model refuses the session before it opens. Nothing on
    # our side can fix it at runtime, so it belongs with the permanent ones.
    "is not found for api version",
    "not supported for bidigeneratecontent",
)
_AUTH_CODES = (r"\b401\b", r"\b403\b")
# Tokens-per-minute clears by itself in seconds and OpenAI says exactly how
# many ("Please try again in 2.9s"). Not an engine fault: the answer is to wait
# and ask again on the SAME engine (ConnectionRecovery does), never a strike.
# Checked after MONEY, so a spent quota delivered as a 429 still switches.
_RATE_LIMIT = ("rate limit", "rate_limit_exceeded")
_TRANSIENT = (
    "keepalive ping timeout",
    "going away",
    "no close frame",
    "connectionclosed",
    "connection is closed",
    "realtime receive loop",
    "timeout",
    "internal error",
    # The OpenAI Realtime 60-minute session cap. It can arrive as a proactive
    # error event (code='session_expired', "Your session hit the maximum
    # duration of 60 minutes.") with none of the socket-close markers above --
    # explicit here, rather than relying on the catch-all default below, so it
    # keeps classifying as TRANSIENT even if that default's behaviour ever
    # changes. (ConnectionRecovery in app/websocket_handler.py also checks
    # these two strings directly, as part of its own narrower "is this
    # specifically a dead OpenAI socket worth reset_conversation()" gate --
    # kept separate from this list because that gate additionally needs the
    # "client event" send-flood pairing this generic classifier does not.)
    "session_expired",
    "maximum duration",
)
_TRANSIENT_CODES = (r"\b500\b", r"\b502\b", r"\b503\b", r"\b504\b", r"\b1001\b", r"\b1006\b", r"\b1011\b")

# NOTE: several of the substrings above (keepalive ping timeout, going away,
# no close frame, connectionclosed, connection is closed, realtime receive
# loop) also appear as ConnectionRecovery's own connection-death signatures in
# app/websocket_handler.py (_DEATH_MARKERS, _SESSION_DEAD_MARKERS, and the
# reader_dead check). The two lists answer different questions -- this one
# decides whether the ROUTER should hear about a failure at all (and can
# therefore fail an engine over); that one decides whether OUR OpenAI socket
# is specifically dead enough that calling reset_conversation() makes sense --
# so they are kept as two lists rather than one importing the other.

# Names of the tools this add-on runs itself. An error carrying one of these is
# ours, and swapping engines would only hide it. Tool names must be followed by
# "failed" or ":" to avoid false matches (e.g., "remember" in a provider error).
_OURS = (
    "play_media",
    "search_home",
    "set_timer",
    "cancel_timer",
    "list_timers",
    "remember",
    "forget",
    "list_memories",
    "voice_enrollment",
    "mark_false_wake",
    "ask_openclaw",
    "recall_memory",
    "web_search",
)
_OURS_PATTERNS = tuple(rf"{tool}(?:\s+failed|:)" for tool in _OURS)


def classify(message: str) -> Failure:
    """Decide what to do about one error message.

    Args:
        message: The text of the error, from an exception or an ErrorFrame.

    Returns:
        The Failure class that says what the caller should do.
    """
    text = (message or "").lower()
    if not text:
        return Failure.APP
    if any(re.search(pattern, text) for pattern in _OURS_PATTERNS):
        return Failure.APP
    if any(marker in text for marker in _MONEY):
        return Failure.MONEY
    if any(re.search(pattern, text) for pattern in _MONEY_CODES):
        return Failure.MONEY
    if any(marker in text for marker in _AUTH):
        return Failure.AUTH
    if any(re.search(pattern, text) for pattern in _AUTH_CODES):
        return Failure.AUTH
    if any(marker in text for marker in _RATE_LIMIT):
        return Failure.RATE_LIMIT
    if any(marker in text for marker in _TRANSIENT):
        return Failure.TRANSIENT
    if any(re.search(pattern, text) for pattern in _TRANSIENT_CODES):
        return Failure.TRANSIENT
    return Failure.TRANSIENT


def retry_after_s(message: str, default: float = 1.0, cap: float = 5.0) -> float:
    """The wait a rate-limit error asks for ("try again in 769ms" / "2.941s"), capped."""
    m = re.search(r"try again in\s*([\d.]+)\s*(ms|s)\b", (message or "").lower())
    if not m:
        return default
    seconds = float(m.group(1)) / (1000.0 if m.group(2) == "ms" else 1.0)
    return min(max(seconds, 0.0), cap)
