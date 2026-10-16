"""GPT-Live runtime: a pipecat LLMService speaking the /v1/live/sessions protocol.

Pipecat 0.0.97 (pinned; see pyproject.toml) predates OpenAI GPT-Live and has
no ``OpenAILiveLLMService``. Upgrading to pipecat 1.x would rewrite every
compatibility hook this add-on carries for the Realtime path, so the Live
protocol is implemented directly here, behind the same ``LLMService``
surface the rest of the pipeline already uses:

* ``InputAudioRawFrame`` in  → ``session.input_audio.append`` (continuous;
  silence is filled in while the device mic is closed, as the documentation
  asks for a continuous microphone stream);
* ``session.output_audio.delta`` → ``TTSAudioRawFrame`` out, byte-exact and in
  order (``app/live_audio.py``), with only long runs of inaudible audio
  withheld so the output transport can derive Bot started/stopped frames the
  way it does for Realtime. Output audio is handled on its own task so a slow
  device socket can never delay a delegated function call;
* transcript deltas → user/assistant turns → ``UserStarted/StoppedSpeaking``,
  ``TranscriptionFrame`` (upstream), ``TTSTextFrame`` + full-response
  brackets (downstream), feeding the phase emitter, transcript log, context
  aggregators and the per-turn timeline unchanged;
* Responses delegation: the backend's function calls run through pipecat's
  own ``run_function_calls`` → the shared ``GuardedToolsMixin`` (speaker gate,
  action gate, liveness, slow-tool ack), results return as
  ``response.item.create`` + ``response.create``.

It also implements the provider-neutral ``SessionControls`` the device
handler uses (app/session_controls.py), and ``reset_conversation()`` for
ConnectionRecovery: a replacement session seeded from recent text history.

Official references: developers.openai.com/api/docs/guides/{voice-websockets,
live-delegation, live-conversations, live-migration}.
"""
import asyncio
import inspect
import base64
import json
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AggregationType,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputTransportMessageFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContextFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.utils.time import time_now_iso8601

from app.live_audio import WITHHELD_REASONS, LiveOutputAudio, OutputAudioCapture, OutputChunk
from pipecat.frames.frames import StartInterruptionFrame
from app.live_evidence import TurnEvidence
from app.live_router import (
    ACK,
    ACK_PREFIX,
    ACTION as ROUTE_ACTION,
    CHAT_KIND,
    FAILED as ROUTE_FAILED,
    LOOKUP as ROUTE_LOOKUP,
    NOTHING as ROUTE_NOTHING,
    CONFIRM as ROUTE_CONFIRM,
    PENDING as ROUTE_PENDING,
    REPLY as ROUTE_REPLY,
    RouteResult,
    ToolRun,
    acknowledgement_state,
    claims_action,
    claims_success,
    clock_tool,
    is_non_speech,
    redact_for_log,
)
from app.live_classifier import CHAT as CLASSIFIED_CHAT
from app import live_tasks
from app.live_tasks import is_deferred_request
from app.live_tasks import (
    ANNOUNCE, IMESSAGE, REOPEN, SESSION, DeliveryLog, TaskPolicy, explain_ack_commentary, in_quiet_hours,
    joined_commentary, normalize_request, progress_commentary, promotion_commentary, quiet_hours_end,
    result_commentary, short_request, spoken_result, text_delivered_commentary, text_message_body, wants_text,
    explain_offer_commentary,
)
from app.live_reply import (
    LiveReplyAdmission,
    Reply,
    ReplyStreamChunk,
    ReplyStreamEnd,
    ReplyStreamStart,
)

from app.live_protocol import (
    LIVE_SAMPLE_RATE,
    LIVE_URL,
    DelegationLedger,
    backend_usage,
    build_session_start,
    chunk_text,
    context_append,
    function_call_output,
    history_to_session_input,
    input_audio_append,
    inner_type,
    new_event_id,
    parse_server_event,
    response_create,
    session_close,
)
from app.tool_arguments import sanitize_tool_arguments, tool_schema_index
from app.tool_guards import GuardedToolsMixin
from app.tool_results import ActionLedger, classify_result, explain_for_model
from app.voice_runtime import LiveConfig

logger = logging.getLogger(__name__)

# Quiet time that closes a transcript turn. Fragments land on ~200 ms frame
# boundaries, so this must clear ordinary pauses inside a sentence.
USER_TURN_GAP_S = 0.8
ASSISTANT_TURN_GAP_S = 1.5
# Transcript fragments with contiguous source-audio timestamps are one
# utterance even if network delay exceeds USER_TURN_GAP_S.
USER_TRANSCRIPT_CONTINUATION_MS = 250
# A further input-transcript fragment that opens within this many seconds of
# the previous fragment's close, while nothing has answered that fragment (no
# reply audio heard, no tool result), is the same request. GPT-Live delivered
# "Turn off Voice PE Live Test Light." as "Turn off" and "PE Live Test Light"
# 1.5 s apart and not contiguous on the session clock (retained Workshop turn
# c0ffee02-38): the second fragment opened a fresh turn, dropped the action
# requirement the first had raised, and the model's tool-free "Done." was
# admitted. Kitchen follow-up c0ffee01-23.f2 lost 2.4 s of speech the same way
# to a four-character trailing fragment, so the silent rescue judged four
# characters. Wall clock, because the timestamp continuation above covers
# only contiguous source audio.
USER_FRAGMENT_MERGE_S = 2.0
# Background conversation, not a request: people talking near the device
# after a false wake keep extending one unanswered "request" fragment by
# fragment (Workshop 2026-10-15: 10+ merged fragments, 300+ characters, two
# sessions of 71 s and 93 s answering a private conversation). Once a request
# has been extended this many times AND grown this long with nothing
# answered, the session is closed silently. A real request rarely pauses
# four times; a long question that does is far under the length.
BACKGROUND_MIN_MERGES = 4
BACKGROUND_MIN_CHARS = 160
# The same room talk can also arrive as separate utterances, each routed and
# answered "Sorry, I didn't catch that" (Workshop rig 2026-10-16: a spoken
# monologue got two not-caught replies and a chat reply), and across sessions:
# the same talk false-woke the device again between them. The second
# utterance with no request in it within BACKGROUND_NOT_CAUGHT_WINDOW_S, in
# any session, closes the session silently: a person who was misheard heard
# the first "say it again" and can wake it again.
BACKGROUND_MAX_NOT_CAUGHT = 2
BACKGROUND_NOT_CAUGHT_WINDOW_S = 90.0
_NOT_CAUGHT_REPLY = re.compile(
    r"\b(?:didn'?t|did not|couldn'?t|could not) (?:catch|hear|get) (?:that|you|it)\b"
    r"|\b(?:say|repeat) (?:it|that) again\b", re.IGNORECASE)
# After a user turn ends: no speech, no delegation → treat as a silent
# (admission) response so the phase machine can close the turn early.
#
# This must not be shorter than the responsiveness budget the program calls
# critical for speech end → first audio (14 s; tests/test_live_service.py
# pins it). It was 4.0 s:
# the 2026-10-11 Workshop canary (turn c0ffee02-32, "Say only the word yes")
# heard the request, the model produced no tool, and its reply audio began
# later than 4 s, so the turn was closed as silent and 218 audio deltas were
# discarded on arrival. The retained turns measure 6–9 s to first audio, so a
# 4 s window silenced every tool-free reply the provider was slow to start.
# Tool turns were unaffected only because a running tool suspends this check.
SILENT_RESPONSE_S = 14.0
# How long the output stream may stop making progress while a candidate reply
# is still held, before the hold is resolved locally.
#
# Complete-reply admission needs a proven quiet boundary, and the ONLY producer
# of one is a further ``session.output_audio.delta`` — the protocol has no
# output-audio-done event. A hold waiting on continuation evidence is released
# only when that evidence completes. So an output stream that simply stops, or
# a continuation that never resolves, left an assembled reply held with nothing
# in this runtime watching it: `_on_delegation_created` cancels the silent-turn
# watchdog and nothing re-arms it, and the alignment watchdog arms only when a
# timestamped transcript leads its PCM. The turn then died on the phase
# emitter's generic 15 s thinking watchdog as `outcome: "error"` with no reply
# and no diagnosis (retained Workshop turn c0ffee02-17).
#
# This bound must therefore stay comfortably inside PhaseEmitter's
# THINKING_TIMEOUT_S (15 s) so the runtime that knows WHY the reply is stuck is
# the one that closes the turn. It only runs while no backend work is in
# flight: a tool or delegation holds turn liveness open on its own and is
# bounded separately by DELEGATION_TIMEOUT_S.
REPLY_RESOLUTION_S = 6.0

# Firmware turn dispositions this house has actually observed, from 12 real
# `turn_metrics` reports across 7 retained device-wire captures: "reply" (11)
# and "superseded" (1, a turn the device itself abandoned). Only "reply" may
# confirm a drain. The set exists so a value from OUTSIDE this sample is
# reported as the unknown it is rather than silently withholding a good drain.
KNOWN_DRAIN_REASONS = frozenset({"reply", "superseded"})

# A silent Live turn is normally the admission policy correctly ignoring an
# accidental wake. A clearly actionable household command is different: the
# backend can safely decide the exact tool and arguments from the completed
# transcript. Keep this deliberately conservative so television fragments do
# not become tool work merely because the voice frontend stayed silent.
_SILENT_TOOL_COMMAND = re.compile(
    r"\b(?:add|remove|delete|complete|check\s+off|turn|switch|set|start|stop|"
    r"cancel|lock|unlock|open|close|play|pause|mute|unmute|dim|brighten|"
    r"vacuum|remember|forget)\b",
    re.IGNORECASE,
)
_SILENT_TOOL_QUERY = re.compile(
    r"\b(?:what|what's|where|when|is|are|how)\b.*\b(?:list|light|switch|"
    r"thermostat|temperature|climate|fan|lock|door|garage|timer|alarm|"
    r"vacuum|media|music|car|time|date)\b",
    re.IGNORECASE,
)
_SILENT_WEB_PREFIX = re.compile(
    r"^(?:please\s+)?search\s+(?:the\s+)?web\b",
    re.IGNORECASE,
)
_SILENT_WEB_LOOKUP = re.compile(
    r"^(?:please\s+)?search\s+(?:the\s+)?web\s+for\s+"
    r"today(?:['’]?s)\s+weather\s+in\s+[a-z][a-z'-]*[?.!]?$",
    re.IGNORECASE,
)


def should_rescue_silent_tool_request(text: str) -> bool:
    """True only for a settled transcript with clear household-tool intent."""
    normalized = " ".join((text or "").split())
    if len(normalized) < 4:
        return False
    # A transcript that begins as a web search must satisfy the complete,
    # conservative web grammar. Never let postfix words such as "cancel",
    # "stop", or "remember" fall through into the generic command rescue.
    if _SILENT_WEB_PREFIX.search(normalized):
        return bool(_SILENT_WEB_LOOKUP.fullmatch(normalized))
    return bool(
        _SILENT_TOOL_COMMAND.search(normalized)
        or _SILENT_TOOL_QUERY.search(normalized)
    )
# Graceful close: the server drains for up to ~10 s; the device is gone by
# then, so wait less.
SESSION_CLOSE_TIMEOUT_S = 3.0
# Silence filler while the device mic is closed (100 ms chunks).
SILENCE_CHUNK_MS = 100
SILENCE_AFTER_MS = 250
# The device can begin forwarding the post-wake utterance while OpenAI is
# still acknowledging session.start. Buffer a bounded window locally; the
# protocol forbids sending it before session.started, but dropping it loses
# the beginning (or all) of a short household command.
PRESTART_AUDIO_MAX_S = 5
# A delegation that never finishes must not hold the thinking watchdog open
# forever (tools are bounded individually; this bounds the backend itself).
DELEGATION_TIMEOUT_S = 150.0
# Consecutive session-start failures before auto-reconnects are refused.
MAX_STARTUP_FAILURES = 3
# Assembled output-audio chunks allowed to wait for the device transport. At
# 100 ms per chunk this is ~30 s of reply audio; reaching it means the device
# link has stalled, not that the model is fast.
MAX_PENDING_AUDIO_CHUNKS = 300
# ---------------------------------------------------------------------------
# Router mode (client delegation; app/live_router.py)
# ---------------------------------------------------------------------------
# Before a verified result exists, the voice's audio is forwarded this long
# behind its arrival, so its transcript (which arrives about 0.1-0.2 s ahead of
# the audio on the real endpoint) can stop anything that is not a plain
# acknowledgement before the user hears it.
ACK_LAG_S = 0.3
# Improvement 5: on top of the lag, acknowledgement audio is released only up
# to the session-timeline end of the last transcript delta at which the
# voice's words formed a complete acknowledgement. Audio positions come from
# the decoded PCM byte count since session.started (the same fallback the
# reply admission uses); transcript deltas carry end_ms on that timeline. A
# loaded event loop that delivers the transcript late can then only delay
# acknowledgement audio, never let the words after it through. Without
# timestamps the lag rule alone applies, as in v33.
GATE_TIMELINE = True
# Prefixed to the result commentary when held speech was dropped unheard.
DROPPED_SPEECH_NOTE = "The user did not hear what you said just before this; do not refer to it."
# Spoken when the router finds no request after the user already heard an
# acknowledgement (they did ask something; it was not caught).
# Tools that take long by nature (an agent, a web search); only these make a
# route a background task at the soft budget (0.18.30).
SLOW_TOOL = re.compile(r"(?:^|__)(?:ask_openclaw|web_search\w*|recall_memory)$|openclaw", re.I)
NOT_CAUGHT_TEXT ="Sorry, I didn't catch that. Could you say it again?"
# The static acknowledgement clip is a fallback only: it plays when nothing has
# been heard this long after the end of the user's speech while the request is
# still being worked on, never on top of the voice, and every firing is logged.
FALLBACK_ACK_AFTER_S = 1.2
# Pre-result audio held while the router decides (about 40 s at 100 ms deltas).
GATE_HOLD_MAX_CHUNKS = 400
# A commentary append counts as pending work until acknowledged, at most this
# long; the session is never closed under an append still in flight.
COMMENTARY_ACK_TIMEOUT_S = 5.0
# Pre-opening a Live socket between wakes is OFF. It would save about 0.3 s
# per wake, but an unstarted socket held open around the clock is an OpenAI
# connection nobody is using, and whether the endpoint bills it is not
# established; cost is a design requirement, so nothing connects between wakes.
PREWARM_SOCKET = False
# A routed request has a hard total bound. "Never close under a pending
# request" must not be holdable by a request that never ends.
ROUTE_TIMEOUT_S = 180.0
# A pre-opened, not yet started Live socket is reused for the next wake while
# younger than this (measured: an unstarted socket accepted session.start
# after 200 s idle; reuse saves the ~0.3 s TCP/TLS/WebSocket connect).
WARM_SOCKET_MAX_AGE_S = 150.0
# A slow request (an agent question can take a minute) is narrated like a
# person would: after PROGRESS_FIRST_S the voice is asked to say it is still
# working on it, then every PROGRESS_EVERY_S, at most PROGRESS_MAX times. If
# nothing is heard soon after such a prompt, the fallback clip plays instead.
PROGRESS_FIRST_S = 8.0
PROGRESS_EVERY_S = 20.0
PROGRESS_MAX = 4
PROGRESS_SILENT_FALLBACK_S = 2.5
PROGRESS_TEXT = ("This request is taking a little longer. In a few words, tell the user you "
                 "are still working on it and ask them to wait. Do not guess the result.")
# Improvement 6: log what the voice said before the verdict (redacted, at
# most a dozen words) and whether it played, so the acknowledgement grammar
# can be widened from what the voice really says. Never the user's words.
LOG_HELD_SPEECH_SAMPLE = True
# Requests shorter than this are not routed (accidental wakes, "um").
ROUTE_MIN_CHARS = 4
# A lookup finished elsewhere (ask_openclaw accepted as a background task)
# that has not reported back by then is closed as failed, so the promised
# report is never silently forgotten. A deferred watch has its own, longer
# bound (TaskPolicy.deferred_max_s).
EXTERNAL_TASK_MAX_S = 1800.0
# An explanation (ask_openclaw explain mode) has a 10-minute budget at the
# bridge, which reports a failure itself; this is the backstop if it never does.
EXPLAIN_TASK_MAX_S = 900.0
# A result delivered into an open session counts as heard once at least this
# much audible output followed the commentary AND the voice has gone quiet past
# the speech hangover in a session that stayed open (rig 2026-10-14: a result
# sent 0.8 s before the device's follow-up cut-off closed the session was
# "delivered" and never heard). Short answers are short: a one-word "Two."
# is under a second of audio (rig 2026-10-15, where a 2 s floor re-queued it
# into the next session), so the floor only rules out a stray fragment.
# A session still open but silent TASK_HEARD_WAIT_S after the commentary is
# sent the result again.
TASK_HEARD_MIN_S = 0.6
TASK_HEARD_WAIT_S = 20.0
# A result being spoken holds the session at most this long from the commentary.
TASK_SPOKEN_HOLD_S = 90.0
# A background result follows the foreground one no sooner than this, so the
# two commentaries never race into the same response.
TASK_AFTER_RESULT_S = 1.5
# A deferred in-session delivery (the user is talking, a request is being
# routed, the voice is speaking) is retried this often, this many times, before
# waiting for the next trigger (a turn closing, a session opening).
TASK_FLUSH_RETRY_S = 1.0
TASK_FLUSH_RETRIES = 60
# A finished but undelivered task is joined by an identical request only this
# soon after it finished; later, the same words are a new request (a second
# "next track" a minute on is meant). A task still running is always joined.
TASK_JOIN_DONE_WINDOW_S = 60.0
# In a session the USER opened (a wake), a waiting result is delivered after
# their own request, or once this long passes with nothing said.
TASK_WAKE_DEFER_S = 3.0
# A turn the router did not route, or found no request in, closes silently
# (no follow-up window) when the voice has said nothing this long after it.
ROUTER_SILENT_CLOSE_S = 6.0


# The fast chat classifier (app/live_classifier.py) may open the gate before
# the backend's verdict only for an utterance none of these deterministic
# checks call a request. A short reply ("yes", "the other one", "do it") is
# only ever an answer to the assistant, never chat.
CHAT_FAST_PATH_MIN_WORDS = 2
# The classifier starts speculatively once the user's transcript has paused
# this long (the turn itself closes at USER_TURN_GAP_S), so its verdict is
# usually in when the turn closes. Used only if the closed text is exactly
# the text it classified; at most this many speculative calls per utterance.
CHAT_SPECULATIVE_PAUSE_S = 0.3
CHAT_SPECULATIVE_MAX = 2
# One-word utterances that are plainly social and never answer a question.
_ONE_WORD_SOCIAL = re.compile(r"^\s*(?:hello|hi|hey|hiya|thanks|cheers|goodbye|bye|morning|evening)\b[\s.!?,]*$", re.I)
_SHORT_REPLY = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|no|nope|nah|sure|ok(?:ay)?|do it|go ahead|please do|"
    r"the other one|that one|this one|both|all of them|none of them|never ?mind|cancel|stop)"
    r"\b[\s.!?,]*(?:please)?[\s.!?]*$", re.I)


# Broader than the silent-command rescue: inflected verbs and household
# nouns. A chat question that names one ("how does a smart lock work?") only
# loses the fast path; it is still answered, at v33 speed.
_CHAT_VETO_DEVICE = re.compile(
    r"\b(?:turn(?:s|ed|ing)?|switch(?:es|ed|ing)?|light(?:s|ing)?|lamps?|locks?|locked|doors?|garage|"
    r"thermostat|heating|timers?|alarms?|lists?|vacuum|blinds|curtains|volume|music|song|playlist|"
    r"tv|television|fan|plug|outlet|camera|car|vehicle|"
    # merge (v35, fastchat review): security, scenes and modes, bare on/off,
    # climate, and "the same"/"again" (a repeat of an earlier request).
    r"security|arm(?:s|ed|ing)?|disarm(?:s|ed|ing)?|gates?|scenes?|modes?|routines?|on|off|"
    r"ac|a/c|air ?con(?:ditioning|ditioner)?|heat(?:s|er|ing)?|same|again)\b", re.I)
# A CHAT verdict waits at most this long for the home's own intent engine,
# which runs first inside route(); still undecided by then = no fast path.
CHAT_INTENT_WAIT_S = 0.5


def chat_fast_path_text_veto(text: str, tool_names: List[str]) -> Optional[str]:
    """Why this utterance's wording alone rules the fast chat path out, or None."""
    normalized = " ".join((text or "").split())
    if _SHORT_REPLY.match(normalized) or (len(normalized.split()) < CHAT_FAST_PATH_MIN_WORDS
                                          and not _ONE_WORD_SOCIAL.match(normalized)):
        return "short reply"
    if (should_rescue_silent_tool_request(normalized) or _SILENT_TOOL_COMMAND.search(normalized)
            or _CHAT_VETO_DEVICE.search(normalized)):
        return "request-shaped"
    if clock_tool(normalized, tool_names) is not None or re.search(r"\b(?:time|date|tomorrow|"
                                                                  r"weather|forecast|rain|news|calendar|"
                                                                  r"message|remind|score|price)\b",
                                                                  normalized, re.I):
        return "current or personal information"
    return None


@dataclass
class RouterOp:
    """One routed utterance. Its id is the dedupe key for this request.

    An op is *foreground* while the pre-result gate waits for it. When its
    soft budget passes, or the user talks over it once a tool has started, it
    becomes a *background* task (app/live_tasks.py): it is never cancelled,
    and its result is delivered when it lands. ``abandoned`` is the only way
    an op ends without a result, and only before any tool ran.
    """

    op_id: str
    seq: int
    text: str
    started_mono: float
    task: Optional[asyncio.Task] = None
    delegations: List[str] = field(default_factory=list)
    tools_started: bool = False
    done: bool = False
    result: Optional[RouteResult] = None
    result_sent_mono: Optional[float] = None
    answered_delegations: set = field(default_factory=set)
    confirm_ids: set = field(default_factory=set)
    # The fast chat classifier's verdict for this utterance (logging and the
    # gate decision), and why the fast path was not taken.
    classifier: Optional[str] = None
    classifier_ms: Optional[int] = None
    fast_veto: Optional[str] = None
    classifier_task: Optional[asyncio.Task] = None
    # Background task state.
    key: str = ""                 # normalised request text: the join key for a repeat
    background: bool = False
    abandoned: bool = False
    promoted_mono: Optional[float] = None
    result_ready_mono: Optional[float] = None
    delivered: bool = False
    joins: int = 0
    progress_count: int = 0
    # A task that finishes elsewhere (ask_openclaw accepted it): the id its
    # result arrives with at /task-result, and the tool that handed it off.
    external_id: str = ""
    tool_name: str = ""
    # A tool that takes long by nature ran (SLOW_TOOL): promote at the soft
    # budget. Without one the route has until TaskPolicy.fast_promote_s.
    slow_tool: bool = False
    # A deferred request ("tell me when X leaves"): answered when the event
    # fires, possibly hours later. Never holds a session, never narrated.
    deferred: bool = False
    # An explanation (ask_openclaw explain mode): minutes of research. The
    # voice set the expectation up front, so it holds no session and is not
    # narrated; the result is a spoken summary (``result.text``) with a fuller
    # write-up (``detail``) for a text, which the user may ask for meanwhile.
    explain: bool = False
    text_requested: bool = False
    detail: str = ""
    # The explanation went into an open session at this time and is not yet
    # known to have been heard (see _task_heard_watch).
    session_sent_mono: Optional[float] = None
    # The route called ask_openclaw in explain mode (known at dispatch, before
    # the acceptance): a promotion speaks the explain acknowledgement instead
    # of the generic "still working" line, and only one acknowledgement is
    # ever spoken (explain_ack_sent). ``speaker`` is the identified first
    # name at the time, for the acknowledgement and the text's recipient.
    explain_route: bool = False
    explain_ack_sent: bool = False
    speaker: str = ""
    # The explanation was spoken to the end: it is never texted as well.
    spoken_to_end: bool = False
    watch: Optional[asyncio.Task] = None       # promotion, then progress narration
    reopen_requested_mono: Optional[float] = None
    delivery: DeliveryLog = field(default_factory=DeliveryLog)
    # A held action of this op was withdrawn (it left the foreground): it is
    # never asked later, never joinable, and its result says it was not done.
    confirm_withdrawn: bool = False


# Delegated calls whose arguments are kept for result classification. Matches
# FunctionCallLog's own bound; only calls awaiting a result are ever read.
IN_FLIGHT_LIMIT = 64

# Sent as session.thinking.append, NOT session.instructions.append. Session
# instructions are documented as trusted and cumulative ("Immutable after
# startup; add more with session.instructions.append"), so appending a
# per-turn stop there would permanently add "wait silently for the user" to
# the session's standing instructions — once per interruption, for the life of
# the session. thinking.append is the documented channel for quiet,
# situational context.
STOP_CONTEXT = (
    "The user just interrupted you. Stop speaking now, do not continue or "
    "repeat that answer, and listen to what they say next."
)


class _Turn:
    __slots__ = ("open", "text", "timer")

    def __init__(self) -> None:
        self.open = False
        self.text = ""
        self.timer: Optional[asyncio.Task] = None


@dataclass
class FunctionCallRecord:
    """One delegated function call, from observation to continuation.

    Argument *keys* and a byte count are kept; argument **values** are not.
    A household's entity names and spoken details live in those values, and
    this record exists to be logged on every turn.
    """

    call_id: str
    name: str
    delegation_id: Optional[str]
    arg_keys: List[str]
    arg_bytes: int
    observed_s: float
    dispatched: bool = False
    result_s: Optional[float] = None
    submitted: bool = False
    continued: bool = False
    outcome: str = "observed"
    # Argument keys the sanitizer removed as placeholders, and what the tool
    # actually answered. Keys and a classification only — never values. Without
    # these two, a turn whose lifecycle counters all read "fine" gives no clue
    # why the house did not change; that is exactly how the second canary's
    # Atrium Lamp failure looked in the log.
    dropped_keys: List[str] = field(default_factory=list)
    result: str = ""

    def describe(self) -> str:
        elapsed = "" if self.result_s is None else f" in {self.result_s - self.observed_s:.2f}s"
        verdict = f" -> {self.result}" if self.result else ""
        dropped = f" [dropped {','.join(self.dropped_keys)}]" if self.dropped_keys else ""
        return (
            f"{self.name}({','.join(self.arg_keys) or '-'})[{self.call_id}] "
            f"{self.outcome}{elapsed}{verdict}{dropped}"
            f"{'' if self.submitted else ' RESULT-NOT-SUBMITTED'}"
            f"{'' if self.continued or not self.submitted else ' NO-CONTINUATION'}"
        )


class FunctionCallLog:
    """Bounded lifecycle log for delegated function calls.

    The canary's tool failure could not be diagnosed because nothing recorded
    whether a call was ever observed, dispatched to a handler, answered, or
    continued. Each of those is a different bug with a different fix, so each
    gets its own counter and its own explicit failure reason.
    """

    def __init__(self, limit: int = 64) -> None:
        self.records: Deque[FunctionCallRecord] = deque(maxlen=limit)
        self._by_id: Dict[str, FunctionCallRecord] = {}
        self.observed = 0
        self.dispatched = 0
        self.unregistered = 0
        self.submitted = 0
        self.continued = 0
        self.abandoned = 0

    def observe(self, *, call_id: str, name: str, delegation_id: Optional[str],
                arguments: dict, now: float) -> FunctionCallRecord:
        record = FunctionCallRecord(
            call_id=call_id, name=name, delegation_id=delegation_id,
            arg_keys=sorted(str(k) for k in (arguments or {})),
            arg_bytes=len(json.dumps(arguments or {}, ensure_ascii=False)),
            observed_s=now,
        )
        self.records.append(record)
        self._by_id[call_id] = record
        self.observed += 1
        return record

    def mark_dispatched(self, record: FunctionCallRecord, registered: bool) -> None:
        record.dispatched = registered
        if registered:
            self.dispatched += 1
            record.outcome = "dispatched"
        else:
            self.unregistered += 1
            record.outcome = "no handler registered"

    def mark_result(self, call_id: str, *, now: float, submitted: bool,
                    continued: bool, outcome: str) -> Optional[FunctionCallRecord]:
        record = self._by_id.get(call_id)
        if record is None:
            return None
        record.result_s = now
        record.submitted = submitted
        record.continued = continued or record.continued
        record.outcome = outcome
        if submitted:
            self.submitted += 1
        if continued:
            self.continued += 1
        return record

    def reject_submission(self, call_id: str, *, now: float,
                          outcome: str) -> Optional[FunctionCallRecord]:
        """Correct a result the socket accepted but the server rejected."""
        record = self._by_id.get(call_id)
        if record is None:
            return None
        if record.submitted:
            self.submitted = max(0, self.submitted - 1)
        if record.continued:
            self.continued = max(0, self.continued - 1)
        record.result_s = now
        record.submitted = False
        record.continued = False
        record.outcome = outcome
        return record

    def note_continuation(self, delegation_id: Optional[str]) -> None:
        """A response.create went out for a delegation after its results."""
        for record in reversed(self.records):
            if record.submitted and not record.continued and (
                delegation_id is None or record.delegation_id == delegation_id
            ):
                record.continued = True
                self.continued += 1
                return

    def reject_continuation(self, delegation_id: Optional[str], outcome: str) -> None:
        for record in reversed(self.records):
            if record.continued and (
                delegation_id is None or record.delegation_id == delegation_id
            ):
                record.continued = False
                record.outcome = outcome
                self.continued = max(0, self.continued - 1)
                return

    def abandon(self, call_ids: Iterable[str], reason: str) -> List[FunctionCallRecord]:
        out = []
        for call_id in call_ids:
            record = self._by_id.get(call_id)
            if record is None or record.submitted:
                continue
            record.outcome = reason
            self.abandoned += 1
            out.append(record)
        return out

    def unanswered(self) -> List[FunctionCallRecord]:
        return [r for r in self.records if not r.submitted]

    def snapshot(self, recent: int = 8, repeats_refused: int = 0) -> Dict[str, object]:
        return {
            "observed": self.observed,
            "dispatched": self.dispatched,
            "unregistered": self.unregistered,
            "submitted": self.submitted,
            "continued": self.continued,
            "abandoned": self.abandoned,
            "repeats_refused": repeats_refused,
            "recent": [r.describe() for r in list(self.records)[-recent:]],
        }


class OpenAILiveLLMService(GuardedToolsMixin, LLMService):
    """OpenAI GPT-Live over a primary WebSocket, with Responses delegation."""

    def __init__(
        self,
        *,
        api_key: str,
        config: LiveConfig,
        instructions: str,
        backend_instructions: str,
        tools: List[dict],
        history: Optional[List[dict]] = None,
        base_url: str = LIVE_URL,
        fill_silence: bool = True,
        audio_capture_ms: int = 0,
        on_demand: bool = False,
        admit_complete_replies: bool = True,
        stream_replies: bool = False,
        router=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.base_url = base_url
        # On-demand lifecycle (app/live_session_gate.py). GPT-Live bills per
        # connected session second and idle time counts, so when this is set
        # the service NEVER opens a socket on its own: pipecat's StartFrame
        # does nothing, and only an explicit open_session() from the gate —
        # driven by a real wake or the first mic frame of a turn — connects.
        # A paid session can outlive the device microphone (thinking/playout).
        # Live still needs continuous input to finish its output timeline. The
        # silence clock runs ONLY after session.started and is cancelled on
        # disconnect; it never opens a socket or reports user activity to the
        # billing gate, so it cannot extend the session's idle deadline.
        self.on_demand = bool(on_demand)
        self.config = config
        try:
            self.set_model_name(config.model)
        except Exception:  # pragma: no cover - pipecat API drift
            pass
        self.instructions = instructions
        self.backend_instructions = backend_instructions
        self.tools = list(tools)
        self._seed_history = list(history or [])
        self._fill_silence = fill_silence
        # Called (sync, never awaited) as a session opens, before the socket
        # connects: starts work that should run in parallel with the open,
        # such as the household snapshot (app/live_context.py).
        self.on_session_opening: Optional[Callable[[], None]] = None

        self._websocket = None
        self._receive_task: Optional[asyncio.Task] = None
        self._silence_task: Optional[asyncio.Task] = None
        self._disconnecting = False
        self._closing = False
        self._resetting_conversation = False
        self._session_started = False
        self._session_id: Optional[str] = None
        self._session_closed = asyncio.Event()
        self._startup_failures = 0
        self._context: Optional[LLMContext] = None
        self._resampler = create_stream_resampler()
        self._last_input_mono = 0.0
        self._input_audio_ms_sent = 0.0
        self._prestart_audio = bytearray()
        self._input_audio_lock = asyncio.Lock()

        self.ledger = DelegationLedger()
        self.reply_admission = LiveReplyAdmission() if admit_complete_replies else None
        self.stream_replies = bool(stream_replies and admit_complete_replies)
        self._stream_pending = None
        # Once a verified final answer starts streaming, that user-turn is
        # sealed against any later delegation. Audio already heard cannot be
        # revoked; the only safe late-tool policy is therefore to refuse the
        # new work and suppress the rest of the response. A genuinely new wake
        # or user turn changes this context and opens a fresh delegation scope.
        self._stream_seal_context = None
        self._reply_playout_until = 0.0
        # Raw model-output PCM capture is OFF unless an operator asks for it:
        # the raw output IS the assistant's voice. The format metrics and the
        # bounded hash that a static report needs are always collected.
        self.audio_capture = (
            OutputAudioCapture(limit_bytes=LIVE_SAMPLE_RATE * 2 * audio_capture_ms // 1000)
            if audio_capture_ms > 0 else None
        )
        self.audio = LiveOutputAudio(capture=self.audio_capture)
        # Transcript timestamps restart at zero for every provider session.
        # Keep LiveOutputAudio's lifetime metrics for diagnostics, but never
        # use them as the untimed-audio alignment clock across reconnects.
        self._session_output_audio_bytes = 0
        self._session_output_audio_identity = None
        # Output audio is forwarded on its own task. The receive loop must stay
        # free to dispatch response.event envelopes: a device socket that
        # backpressures mid-reply would otherwise delay — or, across a
        # reconnect, lose — the function call behind a queue of audio frames.
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._audio_task: Optional[asyncio.Task] = None
        # False until session.started proves the device-safe PCM format. An
        # ErrorFrame can take a moment to tear down the pipeline; never let a
        # mismatched stream reach the speaker during that window.
        self._audio_format_ok = False
        self.calls = FunctionCallLog()
        # Delegated-call argument shaping and outcome handling (see
        # app/tool_arguments.py and app/tool_results.py). The schema index is
        # built once from the tools declared to the session, so the sanitizer
        # can leave required parameters alone.
        self._tool_schemas = tool_schema_index(self.tools)
        self.actions = ActionLedger()
        # call_id -> (tool name, sanitized arguments) for the calls in flight,
        # so a result can be classified and recorded against what was sent.
        self._in_flight: Dict[str, tuple] = {}
        self._user_turn = _Turn()
        self._assistant_turn = _Turn()
        self._silent_task: Optional[asyncio.Task] = None
        self._delegations: dict = {}
        self._response_started_callbacks: List[Callable[[], Awaitable[None]]] = []
        self._pending_context: List[dict] = []
        # Post-interrupt: drop output until the user speaks again.
        self._suppress_output = False
        self._stop_sent = False
        self._heard_audio_this_turn = False
        # A delegated action may arrive before the input transcript which
        # caused it. Confirmation holds created in that window need an extra
        # utterance fence so the late original transcript cannot count as the
        # user's subsequent "yes".
        self._backend_answer_ready = False
        self._evidence = TurnEvidence()
        self._requires_backend_for_reply = False
        self._user_transcript_seen_for_response = False
        # Once any tool has started, replaying the captured user audio after a
        # disconnect could execute it twice. This deliberately survives a
        # session reset and is cleared only once reply audio is accepted.
        self._unsafe_to_replay_input = False
        self._last_user_chars = 0
        self._last_user_text = ""
        self._last_user_raw_text = ""
        self._last_user_text_seq = 0
        self._last_user_end_ms: Optional[float] = None
        self._current_user_start_ms: Optional[float] = None
        self._last_user_start_ms: Optional[float] = None
        self._last_user_turn_closed_mono: Optional[float] = None
        self._silent_rescue_turn = None
        self._confirmation_prompts_pending: set = set()
        self._drain_pending = None
        self._drain_batches = {}
        self._drain_seen = set()
        # Distinct unrecognised firmware drain `reason` values already reported,
        # so a long soak logs each one once instead of on every turn.
        self._unknown_drain_reasons = set()
        self._drain_timeout_task = None
        self._alignment_task = None
        # Bounded supervision of a held, not-yet-admitted reply. See
        # REPLY_RESOLUTION_S.
        self._reply_resolution_task = None
        self._reply_progress_mono = 0.0
        self._pending_result_events: Dict[str, str] = {}
        self._pending_continuation_events: Dict[str, Optional[str]] = {}
        self._unresolved_continuation = False
        self._suppressed_deltas = 0
        self._audio_overflow = 0
        self.live_seconds = 0.0
        # session.started.expires_at (unix seconds). The gate closes ahead of
        # it so the server never has to expire the session out from under a turn.
        self.session_expires_at: Optional[float] = None
        self.backend_tokens = {"in_text": 0, "cached": 0, "out_text": 0, "reasoning": 0}

        # Router mode (app/live_router.py): the app routes every request and the
        # voice speaks only what a tool verified. ``router`` is a TurnRouter.
        self.router = router
        if self.router is not None:
            # Replies stream as they arrive; the pre-result gate below is the
            # only hold, and only for speech that is not an acknowledgement.
            self.reply_admission = None
            self.stream_replies = False
        self._op: Optional[RouterOp] = None
        self._op_count = 0
        self._router_futures: Dict[str, asyncio.Future] = {}
        # Identical router calls still running, keyed by (tool, exact
        # arguments). The repeat ledger only learns a call's outcome when it
        # returns; a second identical call made meanwhile (a re-routed
        # continuation, a parallel route) joins the first instead of reaching
        # the house again.
        self._router_running: Dict[str, asyncio.Future] = {}
        # idle: nobody has spoken in this session yet; the voice's audio is
        #       dropped (Workshop canary attempt 2: 0.9 s after session.start
        #       the endpoint emitted a quiet sound that, streamed, put the
        #       device into "replying", drained, and the follow-up cut-off
        #       closed the session before the request was heard).
        # pre:  an utterance is open or routed and no verified result exists.
        # post: the verified result went in; audio streams.
        # open: no result is coming (chat, nothing to route); audio streams.
        self._gate = "idle"
        self._gate_idle_dropped = 0
        self._need_segment_start = False
        self._gate_lag: Deque = deque()
        # Session-timeline ms up to which the voice's speech is a complete
        # acknowledgement (None: no timestamped acknowledgement yet).
        self._gate_ack_end_ms: Optional[float] = None
        self._gate_timeline_seen = False
        # Everything the voice said while the gate was "pre" this utterance,
        # including before the transcript closed (_gate_text restarts at the
        # close). The fast chat path refuses to open over a claimed outcome.
        self._gate_turn_text = ""
        self._gate_held: List = []
        self._gate_text = ""
        self._gate_blocked = False
        self._gate_drop_until_ack = False
        self._gate_release_task: Optional[asyncio.Task] = None
        self._gate_dropped = 0
        self._fallback_task: Optional[asyncio.Task] = None
        self._progress_task: Optional[asyncio.Task] = None
        self._filler_playing = False
        self._filler_done = asyncio.Event()
        self._filler_done.set()
        self._speech_end_mono: Optional[float] = None
        self._last_user_delta_mono: Optional[float] = None
        self._turn_heard_mono: Optional[float] = None
        self._last_audible_mono: Optional[float] = None
        self._pending_appends: Dict[str, float] = {}
        self._commentary_sent_mono: Optional[float] = None
        self._result_acked = False
        self._result_heard = False
        self._routing_suspended = False
        self._confirmation_awaiting = False
        self._commentary_acked_mono: Optional[float] = None
        self._warm_socket = None
        self._warm_socket_mono = 0.0
        self._warm_task: Optional[asyncio.Task] = None
        self._warm_keeper: Optional[asyncio.Task] = None
        self._warm_closed = False
        # Set by main.py in router mode.
        #   on_fallback_ack(device_id) -> awaitable bool: play one static clip
        #   router_allowed() -> bool: the spend cutoff still allows backend work
        self.on_fallback_ack: Optional[Callable[[str], Awaitable[bool]]] = None
        self.router_allowed: Optional[Callable[[], bool]] = None
        self.router_stats = {"routed": 0, "local": 0, "backend": 0, "fallback_acks": 0,
                             "live_acks": 0, "held_dropped": 0, "fast_chat": 0,
                             "joined": 0, "promoted": 0, "tasks_done": 0, "delivered": {}}
        # Background tasks (app/live_tasks.py), by op id: promoted or detached
        # routes still running or awaiting delivery, and hand-offs awaiting an
        # external result. COST RULE: a task never holds the billed session
        # open (see router_pending); its result is delivered into an open
        # session, or through the configured fallback chain.
        self._tasks: Dict[str, RouterOp] = {}
        self.task_policy = TaskPolicy.from_env()
        self._task_flush_lock = asyncio.Lock()
        self._task_flush_task: Optional[asyncio.Task] = None
        self._task_flush_again = False
        # Set by main.py / websocket_handler in router mode:
        #   on_task_announce(text, device_id) -> awaitable bool: the announcement lane
        #   on_task_message(text) -> awaitable bool: a text through the OpenClaw bridge
        #   on_task_reopen() -> awaitable bool: open a short session through the spend gate
        self.on_task_announce: Optional[Callable[[str, str], Awaitable[bool]]] = None
        self.on_task_message: Optional[Callable[[str], Awaitable[bool]]] = None
        #   on_task_text(text, recipient) -> awaitable bool: a text the user asked for
        #   (an explanation); the bridge resolves the identified speaker's first
        #   name to its own route, else its default owner route
        self.on_task_text: Optional[Callable[[str, str], Awaitable[bool]]] = None
        self.on_task_reopen: Optional[Callable[[], Awaitable[bool]]] = None
        #   on_background_conversation() -> awaitable: close the session silently
        #   (set by websocket_handler to the gate's close)
        self.on_background_conversation: Optional[Callable[[], Awaitable[Any]]] = None
        #   on_discussion() -> None: a discussion opened (the gate raises its bounds)
        self.on_discussion: Optional[Callable[[], Any]] = None
        self._discussion_active = False
        self._merged_fragments = 0
        self._background_closing = False
        # Times of recent utterances with no request; kept across sessions.
        self._not_caught: Deque[float] = deque()
        self._session_started_mono: Optional[float] = None
        self._task_opened_session = False
        if self.router is not None:
            live_tasks.register_sink(self._claim_external_result)
        # Set by main.py in router mode: an app.live_classifier.ChatClassifier.
        # None keeps the v33 behaviour exactly.
        self.chat_classifier = None
        self._last_route_kind: Optional[str] = None
        # (text, task) of the speculative classification for the open turn.
        self._spec_classify: Optional[tuple] = None
        self._spec_timer: Optional[asyncio.Task] = None
        self._spec_count = 0

        # Set by main.py / websocket_handler (shared with the Realtime service).
        self.speaker_probe = None
        self.male_only_tools: set = set()
        self.action_gate = None
        self.spoken_prompts = None
        self.turn_liveness = None
        self.turn_timeline = None
        self.device_id = ""
        self.on_silent_response: Optional[Callable[[], None]] = None
        self.on_response_audio: Optional[Callable[[], None]] = None
        # Lifecycle/cost observers, set by the session gate. Each is a cheap
        # synchronous callback that only moves a deadline; they are separated
        # by *kind* because what may extend a billed session is not the same as
        # what merely proves the model is alive:
        #   on_input_audio      - the user is speaking (or the mic is open)
        #   on_audible_output   - audible reply audio reached the device
        #                         (silence must NEVER count: GPT-Live streams
        #                         continuous digital silence between replies)
        #   on_backend_activity - a delegation/tool made progress
        #   on_reply_finished   - a reply ended; start the follow-up window
        self.on_input_audio: Optional[Callable[[], None]] = None
        self.on_user_utterance: Optional[Callable[[], None]] = None
        self.on_audible_output: Optional[Callable[[], None]] = None
        self.on_backend_activity: Optional[Callable[[], None]] = None
        self.on_reply_finished: Optional[Callable[[], None]] = None

        self._register_event_handler("on_session_started")
        self._register_event_handler("on_delegation_created")

    # ------------------------------------------------------------------
    # pipecat lifecycle
    # ------------------------------------------------------------------

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self.on_demand:
            # Startup must not buy a session. The device connects when it is
            # plugged in and stays connected for days; opening here is what
            # turned uptime into session-minutes. A session is opened only by
            # LiveSessionGate.open(), from a real wake or the first mic frame.
            logger.info(
                "🪙 GPT-Live is on demand: no session until a wake. "
                "Idle costs nothing."
            )
            if PREWARM_SOCKET and self.router is not None and self._warm_keeper is None:
                self._warm_keeper = self._spawn(self._keep_warm(), "live-warm-keeper")
            return
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self.disconnect()
        await self._drop_warm()
        await self._tasks_shutdown()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()
        await self._drop_warm()
        await self._tasks_shutdown()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()
        await self._drop_warm()
        await self._tasks_shutdown()

    async def _drop_warm(self) -> None:
        """Shutdown: no pre-opened socket outlives the service."""
        self._warm_closed = True
        keeper, self._warm_keeper = self._warm_keeper, None
        await self._cancel(keeper)
        await self._cancel(self._warm_task)
        socket, self._warm_socket = self._warm_socket, None
        if socket is not None:
            await self._close_quietly(socket)

    async def _keep_warm(self) -> None:
        """Between wakes keep one pre-opened socket younger than its bound."""
        try:
            while not self._warm_closed:
                if self._websocket is None and (
                        self._warm_socket is None
                        or time.monotonic() - self._warm_socket_mono > WARM_SOCKET_MAX_AGE_S - 30.0):
                    self.prewarm_socket(force=True)
                await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            raise

    async def disconnect(self) -> None:
        """Graceful close (session.close → session.closed), then drop the socket."""
        await self._close_session()
        await self._disconnect()

    # ------------------------------------------------------------------
    # on-demand session surface (app/live_session_gate.py)
    #
    # The gate drives these and nothing else does. They are deliberately the
    # whole contract: open, close, and four read-only facts about the session
    # that a cost supervisor needs.
    # ------------------------------------------------------------------

    @property
    def session_ready(self) -> bool:
        """True only between ``session.started`` and the close/drop."""
        return bool(self._session_started and self._websocket is not None)

    @property
    def session_open(self) -> bool:
        """True from the moment a socket exists, handshake included."""
        return self._websocket is not None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def session_seconds(self) -> float:
        """Server-reported cumulative voice seconds (``session.usage.updated``)."""
        return self.live_seconds

    async def open_session(self) -> None:
        """Connect and send ``session.start``. Raises if the socket fails.

        Returns as soon as ``session.start`` is on the wire — it does NOT wait
        for ``session.started``. Device audio arriving in that window is held
        in the bounded pre-roll buffer and replayed in order once the session
        exists (see ``_send_user_audio`` / ``_on_session_started``), so the
        first utterance survives the handshake without anything upstream
        blocking on it.
        """
        if self._websocket is not None:
            # Already open. Returning early matters: the counters reset below
            # are what a spend cutoff reads, and wiping them on a redundant
            # open would make a long session look brand new.
            return
        if self._startup_failures >= MAX_STARTUP_FAILURES:
            raise RuntimeError(
                f"live session failed to start {self._startup_failures} times in a row; "
                "not opening again until the configuration is fixed and the add-on "
                "restarted"
            )
        # A reopened session has no server-side memory of the last one, so seed
        # it with the recent text conversation the same way a reconnect does.
        if self._context is not None:
            try:
                self._seed_history = history_to_session_input(self._context.get_messages())
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"⚠️ live: could not snapshot history for the new session: {e!r}")
        # Developer test rig (experiment_mode): hot A/B overrides re-read at every session open
        # (timing constants of this module; never a cost bound).
        from app import experiment_overrides
        experiment_overrides.apply_session_safely()
        self.live_seconds = 0.0
        self._backend_answer_ready = False
        self._requires_backend_for_reply = False
        self._user_transcript_seen_for_response = False
        self._heard_audio_this_turn = False
        self.session_expires_at = None
        self._suppress_output = False
        self._stop_sent = False
        self._routing_suspended = False
        self._merged_fragments = 0
        self._background_closing = False
        self._discussion_active = False
        if self.router is not None:
            self._gate_reset("idle")
            self._gate_idle_dropped = 0
        if self.on_session_opening is not None:
            try:
                self.on_session_opening()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"live: session-opening hook failed: {e!r}")
        await self._connect()

    async def close_session(self, reason: str = "") -> None:
        """Close the session gracefully and release everything it owned.

        Every close path funnels through here — idle, stop, timeout, max
        lifetime, spend cutoff, disconnect and error — so there is exactly one
        place where a billed session can end, and it always both closes the
        session *and* drops the socket.
        """
        if self._websocket is None:
            return
        logger.info(f"🧾 closing the GPT-Live session ({reason or 'no reason given'})")
        snapshot = getattr(self, "household_snapshot", None)
        if snapshot is not None and hasattr(snapshot, "cancel"):
            snapshot.cancel()  # a context fetch never outlives its session
        acknowledged = False
        self._routing_suspended = True
        try:
            await self._close_open_turns()
            acknowledged = await self._close_session()
        finally:
            socket_closed = await self._disconnect()
        if not acknowledged and not socket_closed:
            raise RuntimeError("Live session closure uncertain: no session.closed and socket close failed")
        if self.router is not None:
            self.prewarm_socket(force=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self._send_user_audio(frame)
        elif isinstance(frame, LLMContextFrame):
            self._context = frame.context
        elif isinstance(frame, OpenAILLMContextFrame):
            try:
                self._context = LLMContext.from_openai_context(frame.context)
            except Exception:  # pragma: no cover - defensive
                pass
        await self.push_frame(frame, direction)

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, FunctionCallResultFrame):
            await self._handle_function_call_result(frame)
        await super().push_frame(frame, direction)

    # ------------------------------------------------------------------
    # SessionControls (app/session_controls.py)
    # ------------------------------------------------------------------

    @property
    def backend_work_active(self) -> bool:
        """A tool/delegation continuation still owes the user an answer."""
        return (self.ledger.busy or bool(self._delegations) or self._unresolved_continuation
                or self.router_pending)

    @property
    def router_pending(self) -> bool:
        """A foreground request has no delivered result yet, or a commentary
        is still unacknowledged. Nothing may close or idle the session then.

        COST RULE: a background task (promoted after its soft budget, or
        detached when the user talked over it) is deliberately NOT pending
        here. The session closes on its ordinary bounds while the task runs;
        the result is delivered into an open session or through the fallback
        chain (app/live_tasks.py). A slow tool must never bill idle time.
        """
        if self.router is None:
            return False
        op = self._op
        if op is not None and not op.done and not op.background:
            return True
        if self._task_holds_session():
            return True
        now = time.monotonic()
        return any(now - sent < COMMENTARY_ACK_TIMEOUT_S for sent in self._pending_appends.values())

    @property
    def tasks_pending(self) -> int:
        """Background tasks running or awaiting delivery (telemetry)."""
        return len(self._tasks)

    def _task_holds_session(self) -> bool:
        """COST RULE, refined (owner, 2026-10-12): a slow lookup inside an
        active conversation keeps the open session, but only up to the hard
        bound ``hold_session_s`` (default 90 s) from its start; a deferred
        watch never does. Past the bound the ordinary idle rules close the
        session and the result arrives by a short new session or an
        announcement. This never opens a session, only declines to close one."""
        if not self._tasks:
            return False
        now = time.monotonic()
        # A result that went into this session and is being spoken holds it
        # until it has been heard (rig 2026-10-14: "follow-up quiet" closed
        # the session one sentence into a 40 s explanation). Bounded.
        if any(t.session_sent_mono is not None and now - t.session_sent_mono < TASK_SPOKEN_HOLD_S
               for t in self._tasks.values()):
            return True
        bound = self.task_policy.hold_session_s
        if bound <= 0:
            return False
        return any(not t.deferred and not t.explain and not t.delivered and now - t.started_mono < bound
                   for t in self._tasks.values())

    @property
    def backend_work_in_flight(self) -> bool:
        """Is backend work still *running*, as opposed to stuck?

        ``backend_work_active`` deliberately stays true for an unresolved
        continuation, because the turn must not speak and must not replay the
        tool. But that state never resolves on its own — only a new utterance
        or a disconnect clears it — so anything *waiting* for backend work must
        ask this instead. A held reply that waited on the latch deferred
        forever and the turn died mutely on the phase emitter's generic 15 s
        thinking watchdog as ``outcome: "error"``, which is the exact symptom
        the reply-resolution bound exists to remove.
        """
        return self.ledger.busy or bool(self._delegations) or self.router_pending

    @property
    def response_active(self) -> bool:
        return (time.monotonic() < self._reply_playout_until or self.audio.speaking
                or self.backend_work_active)

    @property
    def unsafe_to_replay_input(self) -> bool:
        return self._unsafe_to_replay_input

    def gate_request_context(self):
        device_id, user_seq, wake_seq = self.gate_context()
        if not self._user_transcript_seen_for_response:
            user_seq += 1
        return device_id, user_seq, wake_seq

    def confirmation_requires_spoken_prompt_boundary(self) -> bool:
        return True

    def confirmation_reply_is_affirmative(self) -> bool:
        """Fail closed unless the newest utterance is an explicit spoken yes."""
        timeline = self.turn_timeline
        if timeline is None or self._user_turn.open:
            return False
        text = self._last_user_text
        seq = self._last_user_text_seq
        if seq != timeline.user_turn_seq:
            return False
        folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
        normalized = " ".join(re.sub(r"[^a-z0-9' ]+", " ", folded).split())
        return bool(re.fullmatch(
            r"(?:yes|yeah|yep|yup|sure|ok|okay|confirm(?:ed)?|correct|"
            r"go ahead|do it|please do|that's right|"
            r"ja|jawel|zeker|graag|doe maar|ga door|bevestig(?:d)?|klopt|"
            r"si|claro|adelante|hazlo|confirmar|"
            r"oui|d'accord|allez y|faites le|"
            r"sicher|in ordnung|mach es|"
            r"certo|procedi|fallo)(?: please| graag| por favor| s'il vous plait| bitte)?",
            normalized,
        ))

    def confirmation_reply_source_start_ms(self) -> Optional[float]:
        return None if self._user_turn.open else self._last_user_start_ms

    def confirmation_reply_is_settled(self) -> bool:
        """A transcript prefix can never authorize a consequential action."""
        return not self._user_turn.open

    @property
    def _current_assistant_response(self):
        # Realtime-compatible attribute for code that still peeks at it.
        return True if self.response_active else None

    async def discard_pending_input(self, reason: str = "") -> bool:
        """GPT-Live has no input buffer to clear; the device mic gate is authoritative."""
        logger.debug(f"live: no pending-input buffer to discard ({reason})")
        return False

    async def cancel_active_response(self, reason: str = "", force: bool = False) -> bool:
        """Stop the model talking: drop output locally and tell it to yield.

        Live has no ``response.cancel``. The nudge goes out as
        ``session.thinking.append`` — quiet, situational context — and
        deliberately NOT as ``session.instructions.append``: session
        instructions are trusted and cumulative for the life of the session,
        so one append per interruption would permanently teach the session to
        stay silent. Audio already generated is dropped here until the user
        speaks again, so nothing more reaches the device. Backend work in
        flight finishes; its result is still appended to the conversation.
        """
        if not force and not self.response_active:
            return False
        self._suppress_output = True
        self._drain_output_audio()
        self.audio.reset()
        if self.reply_admission is not None:
            self.reply_admission.reset()
        self._reply_playout_until = 0.0
        # Release drain supervision with the admission that owned it. Resetting
        # the admission alone left `_drain_pending` and its watchdog armed over
        # a batch that had just become unconfirmable by construction, so the
        # watchdog was GUARANTEED to report "drain not confirmed" and close the
        # turn as admission silence — when the truthful account is that the
        # user interrupted it. An interrupted prompt must also never go on to
        # arm a consequential action.
        await self._cancel(self._drain_timeout_task)
        self._drain_timeout_task = None
        self._drain_pending = None
        self._drain_batches.clear()
        self._confirmation_prompts_pending.clear()
        if not self._stop_sent:
            self._stop_sent = True
            await self._send(context_append("thinking", STOP_CONTEXT, None))
            logger.info(f"🛑 live: stop context sent ({reason})")
        return True

    async def inject_context(self, text: str) -> None:
        for chunk in chunk_text(text):
            await self._send_or_queue(context_append("thinking", chunk, None))

    def on_assistant_response_started(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._response_started_callbacks.append(callback)

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        if self._websocket is not None:
            return
        try:
            warm = self._take_warm_socket()
            self._websocket = warm if warm is not None else await websocket_connect(
                uri=self.base_url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                max_size=16 * 1024 * 1024,
            )
            if warm is not None:
                logger.info("🔌 live: reusing a pre-opened socket for this wake")
        except Exception as e:
            self._websocket = None
            await self.push_error(error_msg=f"live connect failed: {e!r}", exception=e)
            raise
        self._session_closed.clear()
        if self._audio_task is None:
            self._audio_task = self._spawn(self._audio_task_handler(), "live-audio")
        self._receive_task = self._spawn(self._receive_task_handler(), "live-receive")
        await self._send(self._session_start_event())
        logger.info(
            f"🟢 live session.start sent (model={self.config.model}, backend={self.config.backend_model}, "
            f"tools={len(self.tools)}, history={len(self._seed_history)} msgs, "
            f"voice={self.config.voice or 'server default'})"
        )

    def _session_instructions(self) -> str:
        """Instructions are built per session in router mode, so the current
        date and time and the household notes are fresh at every wake."""
        if callable(self.instructions):
            return self.instructions()
        return self.instructions

    def _session_start_event(self) -> dict:
        return build_session_start(
            model=self.config.model,
            instructions=self._session_instructions(),
            backend_model=self.config.backend_model,
            backend_instructions=self.backend_instructions,
            tools=self.tools,
            voice=self.config.voice,
            reasoning_effort=self.config.reasoning_effort,
            service_tier=self.config.service_tier,
            max_output_tokens=self.config.max_output_tokens,
            history=self._seed_history,
            sample_rate=LIVE_SAMPLE_RATE,
            client_delegation=self.router is not None,
        )

    # -- warm socket (router mode keepalive between wakes) ----------------

    def _take_warm_socket(self):
        socket, self._warm_socket = self._warm_socket, None
        if socket is None:
            return None
        if time.monotonic() - self._warm_socket_mono > WARM_SOCKET_MAX_AGE_S:
            self._spawn(self._close_quietly(socket), "live-warm-expire")
            return None
        return socket

    async def _close_quietly(self, socket) -> None:
        try:
            await socket.close()
        except Exception:
            pass

    def prewarm_socket(self, force: bool = False) -> None:
        """Open (not start) the next session's socket ahead of the wake.

        An unstarted socket is not a session: nothing is billed until
        ``session.start``. It is replaced when it gets old and dropped on
        shutdown."""
        if (not PREWARM_SOCKET or self.router is None or not self.on_demand
                or self._warm_task is not None or self._warm_closed):
            return
        if (not force and self._warm_socket is not None
                and time.monotonic() - self._warm_socket_mono < WARM_SOCKET_MAX_AGE_S):
            return

        async def _open():
            try:
                old, self._warm_socket = self._warm_socket, None
                if old is not None:
                    await self._close_quietly(old)
                socket = await websocket_connect(
                    uri=self.base_url,
                    additional_headers={"Authorization": f"Bearer {self.api_key}"},
                    max_size=16 * 1024 * 1024,
                )
                self._warm_socket, self._warm_socket_mono = socket, time.monotonic()
            except Exception as e:
                logger.debug(f"live warm socket not opened: {e!r}")
            finally:
                self._warm_task = None

        self._warm_task = self._spawn(_open(), "live-warm")

    async def _disconnect(self) -> bool:
        socket_closed = True
        try:
            self._disconnecting = True
            self._session_started = False
            self.session_expires_at = None
            self._prestart_audio.clear()
            self._last_user_end_ms = None
            self._current_user_start_ms = None
            self._last_user_start_ms = None
            self._input_audio_ms_sent = 0.0
            self._last_user_text = ""
            self._last_user_text_seq = 0
            self._backend_answer_ready = False
            self._requires_backend_for_reply = False
            self._user_transcript_seen_for_response = False
            self._heard_audio_this_turn = False
            self._unresolved_continuation = False
            await self._cancel(self._silence_task)
            self._silence_task = None
            socket, self._websocket = self._websocket, None
            if socket is not None:
                try:
                    await socket.close()
                except Exception as e:  # pragma: no cover - defensive
                    socket_closed = False
                    logger.debug(f"live socket close: {e!r}")
            receive, self._receive_task = self._receive_task, None
            await self._cancel(receive)
            audio, self._audio_task = self._audio_task, None
            await self._cancel(audio)
            dropped = self._drain_output_audio()
            for turn in (self._user_turn, self._assistant_turn):
                await self._cancel(turn.timer)
                turn.timer = None
            await self._release_delegations("disconnect")
            # A call observed but never answered is a diagnosable failure, not
            # a silent one: the model is left waiting and the user hears a
            # claim nothing backed up.
            for record in self.calls.abandon(
                # A router call a background task still awaits is not
                # abandoned: the session closed, the tool did not.
                [r.call_id for r in self.calls.unanswered() if r.call_id not in self._router_futures],
                "abandoned at disconnect",
            ):
                logger.warning(f"⚠️ live function call unanswered: {record.describe()}")
            self._log_audio_summary(f"disconnect (dropped {dropped} unsent audio chunks)")
            self.ledger.reset()
            self._evidence = TurnEvidence()
            self.audio.reset()
            if self.reply_admission is not None:
                self.reply_admission.reset()
            self._reply_playout_until = 0.0
            self._drain_pending = None
            self._stream_pending = None
            self._stream_seal_context = None
            self._drain_batches.clear()
            self._confirmation_prompts_pending.clear()
            await self._cancel(self._drain_timeout_task)
            self._drain_timeout_task = None
            await self._cancel(self._alignment_task)
            self._alignment_task = None
            await self._cancel(self._reply_resolution_task)
            self._reply_resolution_task = None
            self.actions.reset()
            self._in_flight.clear()
            await self._cancel(self._silent_task)
            self._silent_task = None
            if self.router is not None:
                await self._router_supersede("disconnect")
                self._pending_appends.clear()
                for shared in self._router_running.values():
                    if not shared.done():
                        shared.set_exception(RuntimeError("session closed"))
                        shared.exception()
                self._router_running.clear()
        finally:
            self._disconnecting = False
        if self.router is not None:
            # A finished task that was waiting for a free session now goes
            # through the fallback chain (a short new session, an
            # announcement, a text).
            self._task_flush_soon("session closed")
        return socket_closed

    def _log_audio_summary(self, context: str) -> None:
        """One privacy-safe line with everything a static report needs.

        Counters are for the window since the previous summary — one reply —
        because "was that reply broken?" cannot be answered from session
        totals. No transcript and no audio: levels, sizes, counts and a hash.
        """
        report = self.audio.take_turn_report()
        if not report["deltas"]:
            return
        logger.info(
            f"🔊 live output audio [{context}]: {report['forwarded_deltas']}/{report['deltas']} "
            f"deltas {'assembled before reply admission' if self.reply_admission else 'forwarded'}, "
            f"{report['bytes_out']}/{report['bytes_in']} bytes, "
            f"{report['segments']} segment(s), peak {report['peak']}, "
            f"silence {report['silent_deltas']} ({report['silence_suppressed_deltas']} "
            f"suppressed, {report['silence_suppressed_ms']:.0f} ms; "
            f"{report['silence_released_deltas']} in-reply pauses kept, "
            f"{report['idle_head_discarded_deltas']} idle-head discarded, "
            f"{report['silence_held_deltas']} held), "
            f"odd-length {report['odd_length']}, undecodable {report['undecodable']}, "
            f"untimed {report['untimed_deltas']}, timeline gaps {report['timeline_gaps']}"
            f"/overlaps {report['timeline_overlaps']}, "
            f"pace {report['realtime_ratio']}x real time, "
            f"dropped-after-stop {self._suppressed_deltas}, "
            f"backlog-dropped {self._audio_overflow}, "
            f"sha256 {self.audio.digest[:16]}"
        )
        reasons = self.audio.failure_reasons()
        if reasons:
            logger.error(f"❌ live output audio is malformed: {'; '.join(reasons)}")
        if self.calls.observed:
            logger.info(
                "🔧 live function calls: "
                f"{self.calls.snapshot(repeats_refused=self.actions.suppressed)}"
            )
        self._suppressed_deltas = 0

    async def _close_session(self) -> bool:
        if self._websocket is None or not self._session_started:
            return self._session_closed.is_set()
        self._closing = True
        try:
            await self._send(session_close())
            await asyncio.wait_for(self._session_closed.wait(), SESSION_CLOSE_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            logger.info("live: session.closed did not arrive before the close timeout")
        except Exception as e:
            logger.debug(f"live close: {e!r}")
        return False

    async def reset_conversation(self) -> None:
        """Replace the session in place (ConnectionRecovery's reconnect).

        The Live API only takes history at session start, so the replacement
        is seeded with the recent text conversation from the shared context.
        The old session is abandoned, not closed gracefully: a graceful close
        waits for in-flight delegations whose results belong to the dead
        session. Never called from the receive task.
        """
        if self.on_demand:
            raise RuntimeError("on-demand recovery must use LiveSessionGate.recover")
        if self._startup_failures >= MAX_STARTUP_FAILURES:
            raise RuntimeError(
                f"live session failed to start {self._startup_failures} times in a row; "
                "not reconnecting again until the configuration is fixed and the add-on restarted"
            )
        self._resetting_conversation = True
        try:
            await self._close_open_turns()
            await self._disconnect()
            if self._context is not None:
                try:
                    self._seed_history = history_to_session_input(self._context.get_messages())
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning(f"⚠️ live: could not snapshot history for the new session: {e!r}")
            self._suppress_output = False
            self._stop_sent = False
            await self._connect()
        finally:
            self._resetting_conversation = False

    async def _send(self, payload: dict) -> bool:
        socket = self._websocket
        if self._disconnecting or socket is None:
            return False
        try:
            await socket.send(json.dumps(payload))
            return True
        except Exception as e:
            if self._disconnecting or self._websocket is None:
                return False
            # Same wording as pipecat's Realtime service: ConnectionRecovery
            # recognises "Error sending client event" + a close-code marker.
            await self.push_error(error_msg=f"Error sending client event: {e}", exception=e)
            return False

    async def _send_or_queue(self, payload: dict) -> None:
        if self._session_started:
            await self._send(payload)
        else:
            self._pending_context.append(payload)

    # ------------------------------------------------------------------
    # receive loop
    # ------------------------------------------------------------------

    async def _receive_task_handler(self) -> None:
        socket = self._websocket
        if socket is None:
            return
        try:
            async for message in socket:
                if socket is not self._websocket:
                    return
                evt = parse_server_event(message)
                if evt is None:
                    logger.debug("live: ignoring unparseable server event")
                    continue
                try:
                    await self._handle_server_event(evt)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"⚠️ live event handler failed for {evt.get('type')}: {e!r}")
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as e:
            if self._disconnecting or self._resetting_conversation:
                return
            await self.push_error(error_msg=f"live receive loop died: {e!r}")
            return
        except Exception as e:
            if self._disconnecting or self._resetting_conversation:
                return
            await self.push_error(error_msg=f"live receive loop died: {e!r}")
            return
        if self._disconnecting or self._resetting_conversation or self._closing:
            return
        await self.push_error(error_msg="live receive loop ended — connection closed")

    async def _handle_server_event(self, evt: dict) -> None:
        kind = evt["type"]
        if kind == "session.started":
            await self._on_session_started(evt)
        elif kind == "session.output_audio.delta":
            self._on_audio_delta(evt)
        elif kind == "session.output_transcript.delta":
            await self._on_transcript_delta("assistant", evt)
        elif kind == "session.input_transcript.delta":
            await self._on_transcript_delta("user", evt)
        elif kind == "session.delegation.created":
            await self._on_delegation_created(evt)
        elif kind == "response.event":
            await self._on_response_event(evt)
        elif kind == "session.usage.updated":
            seconds = (evt.get("usage") or {}).get("seconds")
            if isinstance(seconds, (int, float)):
                self.live_seconds = float(seconds)
        elif kind == "session.closed":
            await self._on_session_closed(evt)
        elif kind == "error":
            await self._on_error(evt)
        elif kind == "session.updated":
            logger.debug("live: session updated")
        elif kind.endswith(".appended"):
            self._on_appended(evt)
        elif kind.endswith(".muted") or kind.endswith(".unmuted"):
            logger.debug(f"live: ack {kind}")
        else:
            logger.debug(f"live: unhandled event {kind}")

    async def _on_session_started(self, evt: dict) -> None:
        session = evt.get("session") or {}
        session_id = session.get("id")
        identity = (id(self._websocket), session_id)
        if identity != self._session_output_audio_identity:
            self._session_output_audio_bytes = 0
            self._session_output_audio_identity = identity
        self._session_id = session_id
        self._session_started = True
        self._session_started_mono = time.monotonic()
        self._startup_failures = 0
        self._closing = False
        # The server states its own deadline up front; the gate closes ahead of
        # it so `expired` never lands mid-turn (and so a stuck session has a
        # second, independent bound on what it can bill).
        expires_at = session.get("expires_at")
        self.session_expires_at = (
            float(expires_at) if isinstance(expires_at, (int, float)) and expires_at > 0 else None
        )
        logger.info(
            f"✅ live session started ({self._session_id})"
            + (f", server expiry in {self.session_expires_at - time.time():.0f}s"
               if self.session_expires_at else "")
        )
        self._audio_format_ok = await self._verify_negotiated_audio(session)
        if not self._audio_format_ok:
            # Refuse the entire session, not merely its output bytes. Feeding
            # input or reporting readiness here would leave a deaf/noisy
            # session presented to the device as healthy.
            self._closing = True
            await self._send(session_close())
            self._session_started = False
            self._prestart_audio.clear()
            return
        pending, self._pending_context = self._pending_context, []
        for payload in pending:
            await self._send(payload)
        async with self._input_audio_lock:
            if self._prestart_audio:
                audio, self._prestart_audio = bytes(self._prestart_audio), bytearray()
                chunk_bytes = LIVE_SAMPLE_RATE * 2 * SILENCE_CHUNK_MS // 1000
                for offset in range(0, len(audio), chunk_bytes):
                    chunk = audio[offset:offset + chunk_bytes]
                    await self._send_input_audio(
                        input_audio_append(base64.b64encode(chunk).decode("ascii")),
                        len(chunk),
                    )
        if self._fill_silence and self._silence_task is None:
            self._silence_task = self._spawn(self._silence_filler(), "live-silence")
        await self._call_event_handler("on_session_started", evt.get("session") or {})
        if self.router is not None:
            self._task_flush_soon("session started")

    async def _verify_negotiated_audio(self, session: dict) -> bool:
        """Check the echoed audio configuration instead of assuming it.

        Every byte downstream — the assembler, the transport's
        ``audio_out_sample_rate``, the device's I2S chain — assumes mono PCM16
        at ``LIVE_SAMPLE_RATE``. If the server ever negotiates something else,
        the device plays the stream at the wrong rate or the wrong width, which
        is indistinguishable from static at the speaker. The canary had no way
        to tell those apart, so it is checked and named here.
        """
        audio = session.get("audio")
        voice = ((audio or {}).get("output") or {}).get("voice") if isinstance(audio, dict) else None
        fmt = (audio or {}).get("format") if isinstance(audio, dict) else None
        if not isinstance(fmt, dict):
            # The reference states the WebSocket format is fixed at 24 kHz
            # PCM16 mono; an endpoint that does not echo it is taken at its
            # documented word, and the delta metrics will show any drift.
            logger.info(
                f"🔊 live audio: server echoed no format, assuming the documented "
                f"mono PCM16 @ {LIVE_SAMPLE_RATE} Hz (voice={voice or 'server default'})"
            )
            return True
        kind, rate = fmt.get("type"), fmt.get("rate")
        if kind == "audio/pcm" and rate == LIVE_SAMPLE_RATE:
            logger.info(f"🔊 live audio: mono PCM16 @ {rate} Hz (voice={voice or 'server default'})")
            return True
        await self.push_error(error_msg=(
            f"live audio format mismatch: the session negotiated {kind!r} at {rate!r} Hz, "
            f"but this runtime and the device transport only handle mono PCM16 at "
            f"{LIVE_SAMPLE_RATE} Hz. Refusing to feed the device a stream it will play "
            f"as noise."
        ))
        return False

    async def _on_session_closed(self, evt: dict) -> None:
        reason = evt.get("reason") or "unknown"
        usage = evt.get("usage") or {}
        if isinstance(usage.get("seconds"), (int, float)):
            self.live_seconds = float(usage["seconds"])
        logger.info(f"🔚 live session closed ({reason}); voice usage {self.live_seconds:.0f}s")
        self._session_started = False
        self._session_closed.set()
        if not (self._closing or self._disconnecting or self._resetting_conversation):
            # Unrequested close (expired, content, connection_lost): recovery
            # treats "maximum duration"/"session_expired" as a reconnect.
            await self.push_error(error_msg=f"live session_expired: closed by server ({reason})")

    async def _on_error(self, evt: dict) -> None:
        error = evt.get("error") or {}
        details = (
            f"{error.get('type') or 'error'}/{error.get('code') or 'unknown'}: "
            f"{error.get('message') or ''}"
        )
        if error.get("param"):
            details += f" (param: {error['param']})"
        if not self._session_started:
            self._startup_failures += 1
            logger.error(f"❌ live session startup failed ({self._startup_failures}): {details}")
            await self.push_error(error_msg=f"live session startup failed: {details}")
            return
        if error.get("client_event_id"):
            event_id = str(error.get("client_event_id"))
            call_id = self._pending_result_events.pop(event_id, None)
            if call_id is not None:
                self.calls.reject_submission(
                    call_id, now=time.monotonic(),
                    outcome="tool result rejected by server; outcome may be unknown",
                )
                self._unsafe_to_replay_input = True
                await self.push_error(error_msg=(
                    f"live rejected a tool result for {call_id}; the action outcome may be "
                    f"unknown and the request must not be retried automatically: {details}"
                ))
                return
            if event_id in self._pending_continuation_events:
                delegation_id = self._pending_continuation_events.pop(event_id)
                self.calls.reject_continuation(
                    delegation_id, "backend continuation rejected by server"
                )
                self._unresolved_continuation = True
                await self.push_error(error_msg=(
                    "live rejected the backend continuation; the tool result may have been "
                    f"accepted but the answer is unresolved and must not be replayed: {details}"
                ))
                return
            # A rejected non-result append does not by itself end the turn.
            logger.warning(f"⚠️ live rejected a command: {details}")
            return
        logger.warning(f"⚠️ live error: {details}")
        await self.push_error(error_msg=f"live error: {details}")

    # ------------------------------------------------------------------
    # audio
    # ------------------------------------------------------------------

    async def _send_user_audio(self, frame: InputAudioRawFrame) -> None:
        audio = frame.audio
        if frame.sample_rate != LIVE_SAMPLE_RATE:
            audio = await self._resampler.resample(audio, frame.sample_rate, LIVE_SAMPLE_RATE)
        if len(audio) % 2:
            audio = audio[:-1]
        if not audio:
            return
        self._last_input_mono = time.monotonic()
        self._note(self.on_input_audio)
        async with self._input_audio_lock:
            if not self._session_started:
                self._prestart_audio.extend(audio)
                limit = LIVE_SAMPLE_RATE * 2 * PRESTART_AUDIO_MAX_S
                if len(self._prestart_audio) > limit:
                    del self._prestart_audio[: len(self._prestart_audio) - limit]
                return
            await self._send_input_audio(
                input_audio_append(base64.b64encode(audio).decode("ascii")), len(audio)
            )

    async def _silence_filler(self) -> None:
        """Keep the session timeline continuous while the device mic is closed."""
        chunk = b"\x00\x00" * int(LIVE_SAMPLE_RATE * SILENCE_CHUNK_MS / 1000)
        payload = input_audio_append(base64.b64encode(chunk).decode("ascii"))
        period = SILENCE_CHUNK_MS / 1000.0
        try:
            while self._session_started and self._websocket is not None:
                await asyncio.sleep(period)
                async with self._input_audio_lock:
                    if (self._session_started and self._websocket is not None
                            and time.monotonic() - self._last_input_mono >= SILENCE_AFTER_MS / 1000.0):
                        await self._send_input_audio(payload, len(chunk))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"live silence filler stopped: {e!r}")

    def _on_audio_delta(self, evt: dict) -> None:
        """Assemble one delta and hand it to the forwarding task.

        Synchronous on purpose: the receive loop must never block on the
        device-bound socket, because the same loop carries the delegated
        function calls.
        """
        # A device interruption suppresses the cancelled turn until the next
        # user utterance. Suppressed audio is not fed to the assembler at all:
        # doing so would manufacture a fresh response-start callback and mark
        # unheard audio as delivered even though no bytes reach the device.
        if self._suppress_output or not self._audio_format_ok:
            self._suppressed_deltas += 1
            return
        lifetime_bytes_before = self.audio.metrics.bytes_in
        chunk = self.audio.feed(evt.get("delta"), evt.get("start_ms"), evt.get("end_ms"))
        self._session_output_audio_bytes += (
            self.audio.metrics.bytes_in - lifetime_bytes_before
        )
        # The production GPT-Live endpoint omits start_ms/end_ms from every
        # output-audio delta while output-transcript deltas carry timestamps on
        # the same session timeline. The output stream itself is continuous,
        # including digital silence, so decoded PCM duration is the exact
        # fallback timeline position. Without this, any timestamped transcript
        # can only wait forever for an audio timestamp the endpoint never sends.
        audio_end_ms = evt.get("end_ms")
        if not isinstance(audio_end_ms, (int, float)):
            audio_end_ms = (
                1000.0 * self._session_output_audio_bytes
                / (LIVE_SAMPLE_RATE * 2)
            )
        if chunk.reason and chunk.reason not in WITHHELD_REASONS:
            logger.warning(f"⚠️ live output audio rejected a delta: {chunk.reason}")
        if self.reply_admission is not None:
            hold = self._hold_pending_continuation()
            # Judged BEFORE the boundary is fed: the boundary itself discards a
            # rejected segment, and a success claim with nothing behind it
            # must be reported and rescued, not silently dropped.
            claim = (chunk.reason == "idle run exceeded max_silence_ms"
                     and self.reply_admission.has_audio
                     and self._claim_without_evidence())
            reply = self.reply_admission.feed(
                chunk, self._reply_busy() and not hold,
                end_ms=audio_end_ms, pending=hold,
                streamable=self._reply_streamable())
            if reply is not None:
                self._audio_queue.put_nowait(reply)
            elif claim:
                self._spawn(self._withhold_false_success(), "live-false-success")
            admission = self.reply_admission
            if admission.alignment_deadline is not None and self._alignment_task is None:
                self._alignment_task = self._spawn(self._alignment_watch(), "live-alignment")
            self._reconcile_drain_supervision()
            self._note_output_progress()
            return
        if not chunk.pcm:
            return
        if self.router is not None:
            self._gate_push(chunk, audio_end_ms)
            return
        self._enqueue_audio(chunk)

    def _enqueue_audio(self, chunk) -> None:
        if getattr(self, "_need_segment_start", False) and chunk.pcm:
            # Audio before this chunk was dropped, possibly the start of its
            # segment: this chunk opens the response the device will hear.
            self._need_segment_start = False
            if not chunk.started_segment:
                chunk = OutputChunk(pcm=chunk.pcm, started_segment=True, audible=chunk.audible,
                                    gap_ms=chunk.gap_ms, reason=chunk.reason)
        if self._audio_queue.qsize() >= MAX_PENDING_AUDIO_CHUNKS:
            # The device socket has been unable to keep up for ~30 s of audio.
            # Dropping the oldest, and saying so, beats growing without bound;
            # the connection is about to be recovered anyway.
            self._audio_queue.get_nowait()
            self._audio_overflow += 1
            if self._audio_overflow == 1:
                logger.error(
                    "❌ live output audio backlog exceeded "
                    f"{MAX_PENDING_AUDIO_CHUNKS} chunks; the device link cannot "
                    "keep up and reply audio is being dropped"
                )
        self._audio_queue.put_nowait(chunk)

    async def _alignment_watch(self):
        try:
            admission = self.reply_admission
            while admission is self.reply_admission and admission.alignment_deadline is not None:
                await asyncio.sleep(max(0., admission.alignment_deadline - admission.clock()))
                admission.check_alignment()
                if admission.terminal:
                    self._note(self.on_silent_response)
                    break
        finally:
            self._alignment_task = None

    def _reply_candidate_held(self) -> bool:
        """Is there assembled reply material that has not been admitted yet?"""
        admission = self.reply_admission
        if admission is None or admission.terminal:
            return False
        return bool(admission.has_audio or admission.boundary_pending
                    or admission.streaming_segment)

    def _note_output_progress(self) -> None:
        """The provider produced output. Refresh and arm the reply watchdog."""
        self._reply_progress_mono = time.monotonic()
        if (self._reply_resolution_task is None
                and self.reply_admission is not None
                and self._reply_candidate_held()):
            self._reply_resolution_task = self._spawn(
                self._reply_resolution_watch(), "live-reply-resolution"
            )

    async def _reply_resolution_watch(self) -> None:
        """Resolve a held reply locally once the output stream stops.

        Deliberately takes the SAME path as a provider-produced quiet boundary
        (``feed`` with the idle-run reason), so every admission precondition —
        a settled transcript, timestamp coverage, capacity, the segment limit,
        and completed continuation evidence — is enforced identically. Nothing
        here admits speech that the ordinary boundary would have refused; it
        only stops an output stream that went quiet without a final delta from
        hanging the turn, and makes the fail-closed case say why.
        """
        admission = self.reply_admission
        try:
            while admission is self.reply_admission and not admission.terminal:
                if not self._reply_candidate_held():
                    return
                # Backend work in flight owns the wait: a tool/delegation holds
                # turn liveness open and is bounded by DELEGATION_TIMEOUT_S.
                # An *unresolved* continuation is not in flight — it is a dead
                # end for this turn — so it must not defer the bound.
                if self.backend_work_in_flight:
                    self._reply_progress_mono = time.monotonic()
                remaining = (self._reply_progress_mono + REPLY_RESOLUTION_S
                             - time.monotonic())
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                if admission.alignment_deadline is not None:
                    # A timestamped transcript is still owed its PCM; that wait
                    # has its own bounded watchdog with its own failure text.
                    return
                await self._resolve_stalled_reply()
                return
        finally:
            self._reply_resolution_task = None

    async def _resolve_stalled_reply(self) -> None:
        admission = self.reply_admission
        if admission is None or admission.terminal:
            return
        # Judged on the state BEFORE the attempt: a rejected candidate is
        # cleared by the boundary itself, and that must still be reported
        # rather than silently discarded — an unreported discard is how the
        # turn ended up dying on the generic thinking watchdog as `error`.
        held = self._reply_candidate_held()
        hold = self._hold_pending_continuation()
        reply = admission.feed(
            OutputChunk(reason="idle run exceeded max_silence_ms"),
            self._reply_busy() and not hold, pending=hold,
            streamable=self._reply_streamable(),
        )
        if reply is not None:
            logger.info(
                "🔊 live admitted a held reply on a stalled output stream "
                "after %.1fs with no provider output", REPLY_RESOLUTION_S,
            )
            self._audio_queue.put_nowait(reply)
            return
        if admission.alignment_deadline is not None or not held:
            return
        evidence = self._evidence
        admission.fail(
            f"held reply unresolved {REPLY_RESOLUTION_S:.0f}s after the output "
            f"stream stopped (continuations awaiting a response: "
            f"{evidence.pending_continuations}, requirement "
            f"{evidence.requirement or 'none'}, results {sorted(evidence.result_kinds) or 'none'}"
            + (f", unclassified {sorted(evidence.unclassified_results)}"
               if evidence.unclassified_results else "")
            + f", tool results {len(evidence.tool_results)})"
        )
        self._drain_output_audio()
        self._log_audio_summary("held reply failed closed on a stalled output stream")
        self._note(self.on_silent_response)

    def note_output_drained(self, batch_id=None):
        """Only an ordered, byte-covered batch marker identifies output.

        Untagged BotStoppedSpeaking is deliberately not completion evidence.
        """
        pending = self._drain_batches.get(batch_id)
        if pending is not None and pending['enqueued']:
            pending['output_drained'] = True

    def note_device_drain(self, turn_id, metrics):
        """Accept only a fresh matching firmware drain report after output drain.

        Missing, interrupted, stale or timed-out reports never arm confirmation.
        The physical accuracy of firmware drain telemetry remains an HIL gate.
        """
        batch_id = metrics.get('output_batch_id')
        pending = self._drain_batches.get(batch_id)
        if pending is None or not pending["output_drained"] or self._suppress_output:
            return False
        if self.reply_admission is None or pending["generation"] != self.reply_admission.generation:
            return False
        if pending['segment'] not in self.reply_admission.enqueued:
            return False
        seq = metrics.get("seq")
        drained = metrics.get("audible_to_drained_ms")
        reported_turn = turn_id if seq == 0 else f"{turn_id}.f{seq}"
        # Firmware 1.3.1 states the turn's disposition in `reason`, not in
        # `outcome`/`drain_timeout`: across 12 real turn_metrics reports in the
        # retained captures those two keys appear 0 times, while `reason` is
        # always present and was observed as both "reply" and "superseded".
        # Reading only the absent keys (and defaulting them to "pass") meant a
        # turn the DEVICE had abandoned could still arm confirmation. The
        # legacy keys are still honoured for any firmware that sends them.
        if (reported_turn != pending["turn_id"] or type(seq) is not int or seq < 0
                or type(drained) is not int or drained < 0):
            return False
        reason = metrics.get("reason", "reply")
        if reason != "reply":
            # Fail closed on any disposition that is not a plain reply, but say
            # so when the value is one this house has never emitted. `reason`
            # became a rejection signal from a 12-report sample, so a benign
            # firmware value outside that sample would withhold every
            # otherwise-good drain — and an unexplained `return False` here is
            # indistinguishable from a report that never arrived. Logged once
            # per distinct value so a multi-day soak surfaces it immediately
            # instead of hiding it in a drain-timeout warning.
            if reason not in KNOWN_DRAIN_REASONS:
                if reason not in self._unknown_drain_reasons:
                    self._unknown_drain_reasons.add(reason)
                    logger.error(
                        "❌ live drain report rejected on an UNRECOGNISED "
                        f"firmware reason {reason!r} (known: "
                        f"{sorted(KNOWN_DRAIN_REASONS)}); the reply may have "
                        "played correctly. Confirmation stays unarmed."
                    )
            return False
        if (metrics.get("outcome", "replied") != "replied"
                or metrics.get("drain_timeout", False)):
            return False
        key = (turn_id, seq, batch_id)
        if key in self._drain_seen:
            return False
        self.reply_admission.mark_delivered(pending["generation"], pending['segment'])
        self._drain_batches.pop(batch_id, None)
        self._drain_seen.add(key)
        if len(self._drain_seen) > 256:
            self._drain_seen = {key}
        # Earlier segments can finish after later segments start enqueueing.
        # Their completion acknowledges ONLY their own PCM, never the later
        # question or its follow-up window.
        if pending is not self._drain_pending or self.reply_admission.pending:
            return True
        self._drain_pending = None
        self._reply_playout_until = 0.
        if pending.get("prompt_complete", False):
            self._arm_confirmation_prompts()
        self._note(self.on_reply_finished)
        return True

    async def _drain_watch(self, pending, timeout):
        await asyncio.sleep(timeout)
        if self._drain_pending is pending:
            logger.warning("Live reply drain not confirmed; confirmation remains unarmed")
            self._drain_pending = None
            self._drain_batches.clear()
            self._confirmation_prompts_pending.clear()
            self._reply_playout_until = 0.
            self._note(self.on_silent_response)

    async def _audio_task_handler(self) -> None:
        """Push assembled output audio downstream, strictly in order."""
        while True:
            chunk = await self._audio_queue.get()
            try:
                if isinstance(chunk, Reply):
                    await self._deliver_reply(chunk)
                    continue
                if isinstance(chunk, ReplyStreamStart):
                    await self._start_streamed_reply(chunk)
                    continue
                if isinstance(chunk, ReplyStreamChunk):
                    await self._push_streamed_reply_chunk(chunk)
                    continue
                if isinstance(chunk, ReplyStreamEnd):
                    await self._finish_streamed_reply(chunk)
                    continue
                if self._filler_playing:
                    # Never on top of the fallback clip: the voice waits for it.
                    try:
                        await asyncio.wait_for(self._filler_done.wait(), 4.0)
                    except asyncio.TimeoutError:
                        pass
                if chunk.started_segment:
                    await self._speech_segment_started()
                    # A response-start observer can synchronously cancel the
                    # just-started reply (for example, a stop/barge-in racing
                    # the first audio). The chunk already in this task is not
                    # in the queue that cancel_active_response() drains.
                    if self._suppress_output:
                        continue
                if self._suppress_output:
                    continue
                if chunk.audible:
                    if self.router is None or (self._gate == "post" and self._result_acked):
                        # Router mode: only speech the voice produced after
                        # the result went in (the question itself) may arm a
                        # held action; a trailing acknowledgement never does.
                        self._arm_confirmation_prompts()
                    if self.router is not None:
                        self._note_heard()
                        if self._gate == "post" and self._result_acked and not self._result_heard:
                            self._note_result_heard()
                    # ONLY audible output keeps a billed session alive. GPT-Live
                    # streams continuous digital silence between replies with no
                    # output-audio-done event, so counting every forwarded frame
                    # would let an idle model hold the session open indefinitely.
                    self._note(self.on_audible_output)
                await self.push_frame(TTSAudioRawFrame(
                    audio=chunk.pcm, sample_rate=LIVE_SAMPLE_RATE, num_channels=1
                ))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"⚠️ live output audio push failed: {e!r}")

    def _hold_pending_continuation(self):
        # Relevant submitted work is enough to retain a candidate, never to
        # deliver it. Only completed continuation evidence admits that PCM.
        e = self._evidence
        return bool(e.tool_results) and e.requirement_met() and not self._backend_answer_ready

    def _reply_busy(self):
        return (not self._user_transcript_seen_for_response or self.ledger.busy
                or bool(self._delegations) or self._unresolved_continuation
                or (self._requires_backend_for_reply and not self._backend_answer_ready))

    def _reply_streamable(self):
        """Only stream a proven post-backend final answer.

        Tool-free speech still uses complete-reply admission: without a
        delegation boundary, a late function call could otherwise turn the
        already-audible words into an unrevocable pre-tool acknowledgement.
        Confirmation prompts also stay complete so their transcript fence is
        known before any consequential action can be armed.
        """
        return bool(
            self.stream_replies
            and self._requires_backend_for_reply
            and self._backend_answer_ready
            and not self._confirmation_prompts_pending
            and not self._reply_busy()
        )

    async def _start_streamed_reply(self, event: ReplyStreamStart) -> None:
        admission = self.reply_admission
        if (self._suppress_output or admission is None
                or not admission.can_deliver(event) or not self._reply_streamable()):
            return
        await self._speech_segment_started()
        if (self._suppress_output or not admission.can_deliver(event)
                or not self._reply_streamable()):
            return
        timeline = self.turn_timeline
        turn = timeline.current if timeline is not None else None
        batch_id = new_event_id('output_batch')
        pending = {
            "generation": event.generation,
            "segment": event.segment,
            "enqueued": False,
            "turn_id": turn.turn_id if turn else "",
            "output_drained": False,
            "prompt_complete": False,
            "batch_id": batch_id,
            "pcm_bytes": 0,
        }
        self._stream_pending = pending
        self._stream_seal_context = self.gate_context()
        self._drain_pending = pending
        self._drain_batches[batch_id] = pending
        self._reply_playout_until = time.monotonic() + 40.0
        logger.info("🔊 live reply streaming started after backend completion")
        await self.push_frame(LLMFullResponseStartFrame())
        for chunk in event.chunks:
            if (self._suppress_output or self._stream_pending is not pending
                    or not admission.can_deliver(event)):
                return
            pending["pcm_bytes"] += len(chunk.pcm)
            if chunk.audible:
                self._note(self.on_audible_output)
            await self.push_frame(TTSAudioRawFrame(
                audio=chunk.pcm, sample_rate=LIVE_SAMPLE_RATE, num_channels=1
            ))

    async def _push_streamed_reply_chunk(self, event: ReplyStreamChunk) -> None:
        admission = self.reply_admission
        pending = self._stream_pending
        if (pending is None or admission is None or self._suppress_output
                or pending["generation"] != event.generation
                or pending["segment"] != event.segment
                or not admission.can_deliver(event)):
            return
        pending["pcm_bytes"] += len(event.chunk.pcm)
        if event.chunk.audible:
            self._note(self.on_audible_output)
        await self.push_frame(TTSAudioRawFrame(
            audio=event.chunk.pcm, sample_rate=LIVE_SAMPLE_RATE, num_channels=1
        ))

    async def _finish_streamed_reply(self, event: ReplyStreamEnd) -> None:
        admission = self.reply_admission
        pending = self._stream_pending
        if (pending is None or admission is None or self._suppress_output
                or pending["generation"] != event.generation
                or pending["segment"] != event.segment
                or not admission.can_deliver(event)):
            return
        pending["prompt_complete"] = event.prompt_complete
        if event.text.strip():
            frame = TTSTextFrame(event.text, aggregated_by=AggregationType.SENTENCE)
            frame.includes_inter_frame_spaces = True
            await self.push_frame(frame)
        await self.push_frame(LLMFullResponseEndFrame())
        admission.mark_enqueued(event)
        pending["enqueued"] = True
        self._stream_pending = None
        duration = pending["pcm_bytes"] / (LIVE_SAMPLE_RATE * 2)
        timeout = duration + 10.0
        self._reply_playout_until = time.monotonic() + timeout
        await self._cancel(self._drain_timeout_task)
        self._drain_timeout_task = self._spawn(
            self._drain_watch(pending, timeout), "live-device-drain"
        )
        await self.push_frame(OutputTransportMessageFrame(message={
            'type': 'output_batch_end',
            'output_batch_id': pending['batch_id'],
            'pcm_bytes': pending['pcm_bytes'],
            'sample_rate': LIVE_SAMPLE_RATE,
        }))
        logger.info(
            "🔊 live streamed reply complete: %d bytes, %.2fs queued for device playout",
            pending["pcm_bytes"], duration,
        )
        self._log_audio_summary(
            f"streamed segment enqueued, {len(event.text.strip())} transcript chars"
        )

    async def _deliver_reply(self, reply: Reply) -> None:
        admission = self.reply_admission
        if (self._suppress_output or admission is None
                or not admission.can_deliver(reply) or self._reply_busy()):
            return
        await self._speech_segment_started()
        if self._suppress_output or not admission.can_deliver(reply) or self._reply_busy():
            return
        # The downstream transport paces queued PCM. Keep the paid session
        # alive through that bounded playout, not merely through enqueue time.
        duration = sum(len(c.pcm) for c in reply.chunks) / (LIVE_SAMPLE_RATE * 2)
        # Keep bounded supervision alive until the actual device drain report.
        timeout = duration + 10.
        self._reply_playout_until = time.monotonic() + timeout
        timeline = self.turn_timeline
        turn = timeline.current if timeline is not None else None
        batch_id = new_event_id('output_batch')
        self._drain_pending = {"generation": reply.generation,
                               "segment": reply.segment, "enqueued": False,
                               "turn_id": turn.turn_id if turn else "",
                               "output_drained": False,
                               "prompt_complete": reply.prompt_complete}
        self._drain_batches[batch_id] = self._drain_pending
        await self._cancel(self._drain_timeout_task)
        if self._suppress_output or not admission.can_deliver(reply) or self._reply_busy():
            return
        self._drain_timeout_task = self._spawn(
            self._drain_watch(self._drain_pending, timeout), "live-device-drain")
        logger.info("🔊 live reply admitted: %d bytes, %.2fs queued for device playout",
                    sum(len(c.pcm) for c in reply.chunks), duration)
        await self.push_frame(LLMFullResponseStartFrame())
        if reply.text.strip():
            frame = TTSTextFrame(reply.text, aggregated_by=AggregationType.SENTENCE)
            frame.includes_inter_frame_spaces = True
            await self.push_frame(frame)
        for chunk in reply.chunks:
            if self._suppress_output or not admission.can_deliver(reply) or self._reply_busy():
                return
            if chunk.audible:
                self._note(self.on_audible_output)
            await self.push_frame(TTSAudioRawFrame(
                audio=chunk.pcm, sample_rate=LIVE_SAMPLE_RATE, num_channels=1
            ))
        if self._suppress_output or not admission.can_deliver(reply) or self._reply_busy():
            return
        await self.push_frame(LLMFullResponseEndFrame())
        admission.mark_enqueued(reply)
        pending = self._drain_batches.get(batch_id)
        if pending is None:
            return
        pending['enqueued'] = True
        await self.push_frame(OutputTransportMessageFrame(message={
            'type': 'output_batch_end', 'output_batch_id': batch_id,
            'pcm_bytes': sum(len(c.pcm) for c in reply.chunks),
            'sample_rate': LIVE_SAMPLE_RATE,
        }))
        self._log_audio_summary(f"segment enqueued, {len(reply.text.strip())} transcript chars")

    def _drain_output_audio(self) -> int:
        """Drop audio not yet pushed (interruption, teardown). Never heard."""
        dropped = 0
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        return dropped

    async def _speech_segment_started(self) -> None:
        await self._cancel(self._silent_task)
        self._silent_task = None
        for callback in list(self._response_started_callbacks):
            try:
                await callback()
            except Exception as e:
                logger.debug(f"response-started observer failed: {e!r}")
        # A response-start callback may synchronously cancel this first
        # segment. Nothing below should describe cancelled, unheard bytes as a
        # delivered reply.
        if self._suppress_output:
            return
        if not self._heard_audio_this_turn:
            self._heard_audio_this_turn = True
            self._unsafe_to_replay_input = False
            timeline = self.turn_timeline
            if timeline is not None:
                timeline.mark("response_created")
                timeline.mark("first_model_audio")
            if self.on_response_audio is not None:
                try:
                    self.on_response_audio()
                except Exception as e:
                    logger.debug(f"response-audio observer failed: {e!r}")

    def _arm_confirmation_prompts(self) -> None:
        """Arm held actions only at an accepted prompt boundary.

        Complete-reply admission calls this only after the timestamp-covered
        question's exact batch drains. Legacy unbuffered tests use audible
        chunks; that path is not the production Live admission contract.
        """
        if not self._confirmation_prompts_pending:
            return
        armable = self._confirmation_prompts_pending
        if self.router is not None:
            # Router mode: only the current operation's question can be what
            # was just heard. An earlier operation's question was dropped
            # unheard when it was overtaken (twelfth review B11).
            op = self._op
            armable = armable & (op.confirm_ids if op is not None else set())
        gate = getattr(self, "action_gate", None)
        timeline = self.turn_timeline
        if gate is not None and timeline is not None:
            for confirm_id in armable:
                gate.fence_after_prompt(
                    confirm_id, timeline.user_turn_seq, self._input_audio_ms_sent
                )
        self._confirmation_prompts_pending.clear()

    # ------------------------------------------------------------------
    # transcript turns
    # ------------------------------------------------------------------

    async def _on_transcript_delta(self, role: str, evt: dict) -> None:
        delta = evt.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        if role == "assistant" and self.reply_admission is not None:
            hold = self._hold_pending_continuation()
            self.reply_admission.transcript(delta, busy=self._reply_busy() and not hold,
                                            end_ms=evt.get("end_ms"))
            # A late transcript may be the last missing part of an already
            # bounded acoustic segment; it does not require another PCM delta.
            admission = self.reply_admission
            if admission.alignment_deadline is not None:
                reply = admission.feed(OutputChunk(reason="idle run exceeded max_silence_ms"),
                                       self._reply_busy() and not hold, pending=hold,
                                       streamable=self._reply_streamable())
                if reply is not None:
                    self._audio_queue.put_nowait(reply)
            self._reconcile_drain_supervision()
            self._note_output_progress()
            return
        if role == "assistant" and self.router is not None:
            self._gate_note_transcript(delta, evt.get("end_ms"))
        if role == "user":
            self._last_user_delta_mono = time.monotonic()
        turn = self._user_turn if role == "user" else self._assistant_turn
        continuation = False
        if role == "user" and not turn.open:
            start_ms = evt.get("start_ms")
            if isinstance(start_ms, (int, float)) and self._last_user_end_ms is not None:
                continuation = (
                    self._last_user_end_ms <= float(start_ms)
                    <= self._last_user_end_ms + USER_TRANSCRIPT_CONTINUATION_MS
                )
            if not continuation and self._fragment_of_unanswered_request():
                continuation = True
                self._merged_fragments += 1
                logger.info(
                    "🧩 user transcript fragment %.1fs after the previous one with "
                    "nothing answered yet: merged into one request [%d + %d chars]",
                    time.monotonic() - (self._last_user_turn_closed_mono or 0.0),
                    len(self._last_user_raw_text.strip()), len(delta.strip()),
                )
                if self._background_conversation(
                        len(self._last_user_raw_text.strip()) + len(delta.strip())):
                    return
            elif not continuation:
                self._merged_fragments = 0
        if not turn.open:
            turn.open = True
            turn.text = self._last_user_raw_text if continuation else ""
            if role == "user":
                if continuation:
                    self._current_user_start_ms = self._last_user_start_ms
                else:
                    start_ms = evt.get("start_ms")
                    self._current_user_start_ms = (
                        float(start_ms) if isinstance(start_ms, (int, float)) else None
                    )
            await self._open_turn(role, continuation=continuation)
        turn.text += delta
        if role == "user":
            # Enforce tool routing below the voice model: a plausible spoken
            # claim is not evidence that a clock lookup or home action ran.
            # The backend may still return a clarification or a guarded refusal.
            self._requires_backend_for_reply = (self._requires_backend_for_reply
                                                or should_rescue_silent_tool_request(turn.text))
            if self._requires_backend_for_reply:
                if _SILENT_TOOL_COMMAND.search(turn.text):
                    self._evidence.requirement = "action"
                elif self._evidence.requirement != "action" and re.search(r"\b(?:time|date)\b", turn.text, re.I):
                    self._evidence.requirement = "clock"
                elif self._evidence.requirement is None:
                    self._evidence.requirement = "query"
                evidence = self._evidence
                relevant = (evidence.kind in ("clarification", "refusal")
                            or evidence.requirement_met())
                self._backend_answer_ready = evidence.ready and relevant and not self.ledger.busy
        if role == "user" and isinstance(evt.get("end_ms"), (int, float)):
            self._last_user_end_ms = float(evt["end_ms"])
        if role == "user":
            await self.push_frame(
                InterimTranscriptionFrame(turn.text, "", time_now_iso8601()), FrameDirection.UPSTREAM
            )
        else:
            frame = TTSTextFrame(delta, aggregated_by=AggregationType.SENTENCE)
            frame.includes_inter_frame_spaces = True
            await self.push_frame(frame)
        await self._cancel(turn.timer)
        gap = USER_TURN_GAP_S if role == "user" else ASSISTANT_TURN_GAP_S
        turn.timer = self._spawn(self._close_turn_after(role, gap), f"live-turn-{role}")
        if role == "user" and self.chat_classifier is not None and self.router is not None:
            await self._cancel(self._spec_timer)
            self._spec_timer = self._spawn(self._speculative_classify(), "live-classify-spec")

    async def _close_turn_after(self, role: str, gap: float) -> None:
        await asyncio.sleep(gap)
        turn = self._user_turn if role == "user" else self._assistant_turn
        turn.timer = None
        await self._end_turn(role)

    async def _close_open_turns(self) -> None:
        for role in ("user", "assistant"):
            turn = self._user_turn if role == "user" else self._assistant_turn
            await self._cancel(turn.timer)
            turn.timer = None
            await self._end_turn(role)

    async def _open_turn(self, role: str, continuation: bool = False) -> None:
        if role == "user":
            if not continuation:
                self._note(self.on_user_utterance)
            # A new utterance: lift any post-stop output suppression and start
            # the per-turn bookkeeping the Realtime path gets from server VAD.
            self._suppress_output = False
            self._stop_sent = False
            if self.router is not None:
                if not continuation:
                    self._spec_count = 0
                    self._spec_classify = None
                await self._router_on_user_speech(continuation)
            if not continuation:
                await self._cancel(self._alignment_task)
                self._alignment_task = None
                await self._cancel(self._reply_resolution_task)
                self._reply_resolution_task = None
                await self._cancel(self._drain_timeout_task)
                self._drain_timeout_task = None
                self._drain_pending = None
                self._drain_batches.clear()
                # Delegation can precede the FIRST input transcript. That
                # work belongs to this session's provisional first utterance,
                # not to a superseded turn. Later utterances get fresh scopes.
                if self._user_transcript_seen_for_response:
                    self._evidence.next_turn()
                    self.ledger.abandon_active()
                    await self._release_delegations("superseded user turn")
                    self._backend_answer_ready = False
                    # An unresolved continuation belonged to the turn this new
                    # utterance supersedes. Leaving it set made _reply_busy()
                    # true for the rest of the session, so every later reply
                    # was invalidated on arrival. The replay-safety flag
                    # (_unsafe_to_replay_input) is deliberately NOT touched.
                    self._unresolved_continuation = False
                    self.actions.reset()
                self._stream_seal_context = None
                self._requires_backend_for_reply = False
                if self.reply_admission is not None:
                    self.reply_admission.reset()
                self._heard_audio_this_turn = False
                self._user_transcript_seen_for_response = True
                # The provisional first utterance retains its repeat fence.
            await self._cancel(self._silent_task)
            self._silent_task = None
            timeline = self.turn_timeline
            if timeline is not None:
                turn = timeline.ensure_active()
                # Confirmation safety needs the utterance sequence at speech
                # start. GPT-Live can delegate before the transcript's close
                # timer fires; incrementing at speech end would let that same
                # utterance satisfy its own confirmation challenge.
                if not continuation:
                    timeline.user_turn_seq += 1
                timeline.mark("speech_started")
                turn.meta.setdefault("model_name", self.model_name)
            await self._broadcast(UserStartedSpeakingFrame)
        else:
            await self.push_frame(LLMFullResponseStartFrame())

    async def _end_turn(self, role: str) -> None:
        turn = self._user_turn if role == "user" else self._assistant_turn
        if not turn.open:
            return
        turn.open = False
        text, turn.text = turn.text, ""
        if role == "user":
            # Length only, never the words. The second canary's unanswered
            # "all of them" follow-up could not be diagnosed because nothing
            # recorded whether GPT-Live had transcribed the utterance at all:
            # "the model never heard it" and "the model heard it and chose to
            # say nothing" need completely different fixes.
            self._last_user_chars = len(text.strip())
            self._last_user_raw_text = text
            self._last_user_text = text.strip()
            self._last_user_start_ms = self._current_user_start_ms
            self._last_user_turn_closed_mono = time.monotonic()
            timeline = self.turn_timeline
            self._last_user_text_seq = timeline.user_turn_seq if timeline is not None else 0
            if text.strip():
                await self.push_frame(TranscriptionFrame(text.strip(), "", time_now_iso8601()),
                                      FrameDirection.UPSTREAM)
            timeline = self.turn_timeline
            if timeline is not None:
                # Live increments user_turn_seq in _open_turn(), before tools
                # can dispatch. Do not increment it a second time here.
                timeline.mark("speech_stopped")
            await self._broadcast(UserStoppedSpeakingFrame)
            await self._cancel(self._silent_task)
            if self.router is not None:
                # Router mode: the app routes the request now. There is no
                # silent wait and no rescue; tools never depend on the voice
                # deciding to delegate.
                self._silent_task = None
                await self._router_start(text.strip())
                self._task_flush_soon("user turn closed")
            else:
                self._silent_task = self._spawn(self._silent_response_watch(), "live-silent")
        else:
            if self.router is not None and self._gate == "open" and text.strip():
                self.router.note_assistant(text)
            await self.push_frame(LLMFullResponseEndFrame())
            # The reply is over: start the bounded follow-up window. Once it
            # expires with nothing further, the session closes and stops billing.
            self._note(self.on_reply_finished)
            if self.router is not None:
                self._task_flush_soon("reply finished")
            # The reply is over: write the turn's audio and function-call
            # evidence now, while it is still correlatable to what the
            # household just heard. The canary's logs were lost to a container
            # restart; a per-reply line survives that.
            self._log_audio_summary(f"reply end, {len(text.strip())} transcript chars")

    async def _broadcast(self, frame_cls) -> None:
        await self.push_frame(frame_cls(), FrameDirection.UPSTREAM)
        await self.push_frame(frame_cls(), FrameDirection.DOWNSTREAM)

    def _background_conversation(self, chars: int) -> bool:
        """Close the session if the unanswered request is a conversation in
        the room (see BACKGROUND_MIN_MERGES). True when the transcript is to
        be ignored because the session is closing."""
        if self._background_closing:
            return True
        if self._merged_fragments < BACKGROUND_MIN_MERGES or chars < BACKGROUND_MIN_CHARS:
            return False
        return self._close_for_background(
            f"{self._merged_fragments + 1} fragments, {chars} characters with nothing answered")

    def on_discussion_started(self) -> None:
        """A discussion tool loaded an item (discussions by voice): the owner is
        talking to the device at length on purpose, so the room-talk closes
        stand down for this session and the gate gets discussion bounds."""
        self._discussion_active = True
        self._not_caught.clear()
        if self.on_discussion is not None:
            try:
                self.on_discussion()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"discussion observer failed: {e!r}")

    def _close_for_background(self, why: str) -> bool:
        """Close the session silently: what it hears is a conversation in the
        room, not a request. Lengths and counts only, never the words."""
        if self._discussion_active:
            return False
        if self._background_closing:
            return True
        if self.on_background_conversation is None:
            return False
        self._background_closing = True
        self._routing_suspended = True  # a half conversation is not a request
        self._suppress_output = True  # nothing more is said to the room
        op = self._op
        if op is not None and not op.done and op.task is not None and not op.tools_started:
            op.abandoned = True
            op.task.cancel()
        logger.warning(f"🙉 background conversation: {why}; closing the session without replying")
        self._spawn(self.on_background_conversation(), "live-background-close")
        return True

    def _fragment_of_unanswered_request(self) -> bool:
        """Is a user fragment opening now a continuation of the last request?

        True while the previous fragment closed less than
        ``USER_FRAGMENT_MERGE_S`` ago and nothing has answered it: no reply
        audio has been heard and no tool has returned. A delegation merely in
        flight does not count as an answer; the model, too, is still deciding.
        """
        closed = self._last_user_turn_closed_mono
        if closed is None or not self._last_user_raw_text.strip():
            return False
        if time.monotonic() - closed > USER_FRAGMENT_MERGE_S:
            return False
        return (self._user_transcript_seen_for_response
                and not self._heard_audio_this_turn
                and not self._evidence.tool_results)

    def _claim_without_evidence(self) -> bool:
        """A complete model segment for a tool-shaped request with nothing behind it.

        The user's words raised an action, clock or query requirement; no tool
        has returned, no backend work is in flight or awaited, and nothing has
        been played. Whatever the model just said is therefore a claim that
        cannot be true yet. Retained Workshop turn c0ffee02-38 said "Done."
        here and the light stayed on.
        """
        return (self._user_transcript_seen_for_response
                and self._requires_backend_for_reply
                and not self._backend_answer_ready
                and not self.ledger.busy
                and not self._delegations
                and not self._unresolved_continuation
                and not self._evidence.tool_results
                and not self._evidence.has_outstanding_request()
                and not self._heard_audio_this_turn)

    async def _withhold_false_success(self) -> None:
        requirement = self._evidence.requirement or "tool"
        if self._user_turn.open:
            # The model answered before the user's transcript closed (Workshop
            # canary turn c0ffee02-54, candidate-v30). The rescue judges and
            # sends the last closed request, so close this one now: otherwise
            # it judged the previous turn's text, found nothing to rescue, and
            # refused a request that had never been tried. A later fragment
            # still merges under the fragment rule.
            await self._cancel(self._user_turn.timer)
            self._user_turn.timer = None
            await self._end_turn("user")
        if self._can_rescue_through_backend():
            logger.warning(
                "⛔ live reply withheld: %s-shaped request answered with no tool "
                "this turn [%d transcript chars]; sending it directly to the "
                "guarded backend", requirement, self._last_user_chars,
            )
            await self._rescue_through_backend()
            return
        logger.warning(
            "⛔ live reply withheld again: %s-shaped request answered with no "
            "tool this turn after the rescue; ending the turn truthfully",
            requirement,
        )
        prompts = self.spoken_prompts
        if prompts is not None and getattr(prompts, "enabled", False):
            try:
                await prompts.say("error_not_done", self.device_id)
            except Exception as e:
                logger.debug(f"could not speak the refusal: {e!r}")
        await self._cancel(self._silent_task)
        self._silent_task = None
        self._note(self.on_silent_response)

    def _can_rescue_through_backend(self) -> bool:
        """May the user's request be sent to the guarded backend right now?

        Shared by the silent-turn watch and the false-success withhold so both
        obey the same single-flight rule: never duplicate a potentially
        side-effecting rescue, and never rescue twice for one request.
        """
        rescue_key = (self._last_user_text_seq, self._last_user_text)
        return (not self._backend_answer_ready and not self._evidence.tool_results
                and not self._evidence.has_outstanding_request()
                and rescue_key != self._silent_rescue_turn
                and should_rescue_silent_tool_request(self._last_user_text))

    async def _rescue_through_backend(self) -> None:
        """Send the user's full request to the guarded backend, once per request."""
        self._silent_rescue_turn = (self._last_user_text_seq, self._last_user_text)
        self._note(self.on_backend_activity)
        request = response_create(new_event_id("silent_rescue_response"))
        self._evidence.request_client_response(request['event_id'])
        await self._send_or_queue({
            "type": "response.item.create",
            "event_id": new_event_id("silent_rescue"),
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": self._last_user_text}],
            },
        })
        await self._send_or_queue(request)
        # One rescue only. If the backend also produces nothing, the next
        # watchdog pass takes the ordinary silent-turn close path.
        await self._cancel(self._silent_task)
        self._silent_task = self._spawn(self._silent_response_watch(), "live-silent-rescue")

    async def _silent_response_watch(self) -> None:
        await asyncio.sleep(SILENT_RESPONSE_S)
        if (self._heard_audio_this_turn or self._delegations or self.ledger.busy or self._user_turn.open
                or (self.reply_admission is not None and self.reply_admission.has_audio
                    and not self.reply_admission.rejected)):
            return
        if self._can_rescue_through_backend():
            logger.warning(
                "🛟 GPT-Live stayed silent on a clear household-tool request "
                "[%d transcript chars]; sending it directly to the guarded backend",
                self._last_user_chars,
            )
            await self._rescue_through_backend()
            return
        logger.info(
            f"🤐 live said nothing and called no tool {SILENT_RESPONSE_S:.0f}s after "
            f"the user's turn ended [{self._last_user_chars} transcript chars heard]"
            + ("" if self._last_user_chars else
               " — GPT-Live produced no input transcript, so the utterance did not "
               "reach the model")
        )
        callback = self.on_silent_response
        if callback is not None:
            try:
                callback()
            except Exception as e:
                logger.debug(f"silent-response observer failed: {e!r}")

    def _reconcile_drain_supervision(self) -> None:
        """Keep drain bookkeeping and the admission's enqueued set in step.

        ``LiveReplyAdmission.invalidate()`` drops the enqueued set, and it is
        reached from inside ``feed()`` whenever the backend is busy — including
        on the first provider delta after ``_unresolved_continuation`` is set by
        a server-rejected continuation. The explicit revocation paths clear
        ``_drain_batches`` with it, but that one does not, which left the
        service supervising a batch the admission had already revoked: no
        firmware report could ever match it, so the drain watchdog was
        guaranteed to report an admitted, physically audible reply as
        "drain not confirmed" and close the turn as admission silence — the
        retained Kitchen c0ffee01-20 signature.

        A revoked batch still must never arm confirmation or start the
        follow-up window, so this releases supervision rather than confirming
        anything; it only makes the failure honest and bounded.
        """
        admission = self.reply_admission
        if admission is None or not self._drain_batches:
            return
        revoked = [
            batch_id for batch_id, batch in self._drain_batches.items()
            if batch.get("enqueued")
            and (batch["generation"] != admission.generation
                 or batch["segment"] not in admission.enqueued)
        ]
        if not revoked:
            return
        pending_revoked = False
        for batch_id in revoked:
            batch = self._drain_batches.pop(batch_id)
            if batch is self._drain_pending:
                pending_revoked = True
        if not pending_revoked:
            return
        logger.warning(
            "⚠️ live reply batch revoked before the device confirmed its drain; "
            "confirmation stays unarmed and no follow-up window opens"
        )
        self._drain_pending = None
        self._reply_playout_until = 0.
        self._confirmation_prompts_pending.clear()
        task, self._drain_timeout_task = self._drain_timeout_task, None
        if task is not None and not task.done():
            # Synchronous on purpose: this runs on the receive loop's audio
            # path, which must never block on anything.
            task.cancel()
        self._note(self.on_silent_response)

    # ------------------------------------------------------------------
    # delegation
    # ------------------------------------------------------------------

    async def _invalidate_queued_reply(self):
        if self.reply_admission is not None:
            had_output = self._drain_pending is not None
            self.reply_admission.invalidate(
                reject_segment=self.reply_admission.has_audio)
            self._drain_output_audio()
            self._drain_pending = None
            self._stream_pending = None
            self._drain_batches.clear()
            await self._cancel(self._alignment_task)
            self._alignment_task = None
            if had_output:
                await self.push_frame(StartInterruptionFrame())

    async def _on_delegation_created(self, evt: dict) -> None:
        delegation = evt.get("delegation") or {}
        did = str(delegation.get("id") or "")
        target = delegation.get("target")
        if self.router is not None:
            await self._router_on_delegation(did, target)
            return
        if self._stream_seal_context is not None:
            if self.gate_context() == self._stream_seal_context:
                # This work arrived after irreversible audio for the same
                # utterance. Never execute it or admit any later model output;
                # cut the remainder locally and leave the fresh-turn path to
                # lift suppression. This converts an impossible rollback into
                # a deterministic no-side-effect failure.
                logger.warning(
                    "⚠️ refusing late delegation %s after streamed reply admission",
                    did or "?",
                )
                self._backend_answer_ready = False
                await self._invalidate_queued_reply()
                await self.cancel_active_response(
                    "late delegation after streamed reply", force=True
                )
                return
            # A fresh wake/utterance owns a different timeline scope. Its
            # pre-transcript delegation is legitimate and must not inherit the
            # previous turn's seal.
            self._stream_seal_context = None
        if not self._evidence.delegate(did):
            return
        self._backend_answer_ready = False
        logger.info(f"🧠 live delegation {did or '?'} → {target}")
        await self._invalidate_queued_reply()
        self._note(self.on_backend_activity)
        timeline = self.turn_timeline
        if timeline is not None:
            timeline.mark("response_created")
        await self._cancel(self._silent_task)
        self._silent_task = None
        if did and did not in self._delegations:
            # Backend work counts as model activity for the thinking watchdog,
            # bounded by DELEGATION_TIMEOUT_S.
            if self.turn_liveness is not None:
                self.turn_liveness.tool_started()
            self._delegations[did] = self._spawn(self._delegation_timeout(did), f"live-deleg-{did}")
        await self._call_event_handler("on_delegation_created", delegation)
        if target == "client":
            # This runtime is configured for Responses delegation; a client
            # delegation would never be answered. Tell the model so.
            await self._send(context_append(
                "commentary", "No backend is available for that request in this session.", did or None))

    async def _delegation_timeout(self, did: str) -> None:
        try:
            await asyncio.sleep(DELEGATION_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        logger.warning(f"⚠️ live delegation {did} did not finish within {DELEGATION_TIMEOUT_S:.0f}s")
        self._finish_delegation(did, cancel_task=False)

    def _finish_delegation(self, did: Optional[str], cancel_task: bool = True) -> None:
        if not did:
            # Uncorrelated events: release the oldest open delegation.
            did = next(iter(self._delegations), None)
        task = self._delegations.pop(did, None) if did else None
        if task is None:
            return
        if cancel_task and task is not asyncio.current_task():
            task.cancel()
        if self.turn_liveness is not None:
            self.turn_liveness.tool_finished()

    async def _release_delegations(self, reason: str) -> None:
        for did in list(self._delegations):
            self._finish_delegation(did)

    async def _on_response_event(self, evt: dict) -> None:
        kind = inner_type(evt)
        evidence, generation = self._evidence, self._evidence.generation
        continuation_was_pending = self._evidence.pending_continuations > 0
        if not self._evidence.accept(evt):
            return
        if kind == "response.created" and not continuation_was_pending:
            await self._invalidate_queued_reply()
        if self._evidence is not evidence or evidence.generation != generation:
            return
        call, continue_now = self.ledger.observe(evt)
        if call is not None:
            await self._invalidate_queued_reply()
            if self._evidence is not evidence or evidence.generation != generation:
                return
            await self._dispatch_function_call(call)
        if self._evidence is not evidence or evidence.generation != generation:
            return
        if kind == "response.completed":
            usage = backend_usage(evt)
            if usage:
                for key, value in usage.items():
                    self.backend_tokens[key] = self.backend_tokens.get(key, 0) + value
                logger.info(
                    f"💰 live backend usage: in {usage['in_text']} (cached {usage['cached']}) "
                    f"out {usage['out_text']} (+{usage['reasoning']} reasoning); "
                    f"voice {self.live_seconds:.0f}s cumulative"
                )
        if kind in ("response.failed", "response.incomplete"):
            response = (evt.get("event") or {}).get("response") or {}
            error = response.get("error") or response.get("incomplete_details") or {}
            message = error.get("message") or error.get("reason") if isinstance(error, dict) else None
            await self.push_error(
                error_msg=f"live delegated response {kind.split('.')[-1]}: {message or response.get('status', 'unknown')}"
            )
        self._backend_answer_ready = self._evidence.ready and not self.ledger.busy
        if self._backend_answer_ready and self.reply_admission is not None:
            admission = self.reply_admission
            if admission.boundary_pending:
                reply = admission.feed(OutputChunk(reason="idle run exceeded max_silence_ms"), False)
                if reply is not None:
                    self._audio_queue.put_nowait(reply)
        if kind in ("response.completed", "response.failed", "response.incomplete") and not self.ledger.busy:
            self._finish_delegation(evt.get("delegation_id"))
        if continue_now:
            # The results landed before the backend finished emitting items,
            # so the continuation is due now rather than with the last result.
            if await self._send_continuation(evt.get("delegation_id")):
                self.calls.note_continuation(evt.get("delegation_id"))

    async def _dispatch_function_call(self, call) -> None:
        """Run one delegated call through the shared tool guards.

        Every step is recorded (``FunctionCallLog``) because each failure mode
        needs a different fix: a call never observed is a protocol problem, a
        call with no registered handler is a wiring problem, and a call whose
        result is never submitted leaves the model waiting for a tool that will
        never answer — which is how an action can "fail" with the assistant
        still talking about it.

        Two things happen before the call reaches a handler:

        * its arguments are stripped of the placeholder values GPT-Live's
          backend supplies for every optional parameter it has no value for.
          One ``floor: ""`` made every Home Assistant action in the second
          canary fail validation before any entity was resolved
          (``app/tool_arguments.py``);
        * the per-utterance :class:`~app.tool_results.ActionLedger` is asked
          whether this call is an exact repeat, or one target's third failing
          attempt. Either way it is answered from the ledger rather than sent
          to the house.

        Sanitizing happens here, upstream of the guards, so the speaker and
        action gates judge exactly the arguments Home Assistant will receive.
        It can only ever remove an empty value, so no confirmation can be
        skipped by it.
        """
        arguments, dropped = sanitize_tool_arguments(
            call.name, call.arguments, self._tool_schemas.get(call.name)
        )
        record = self.calls.observe(
            call_id=call.call_id, name=call.name, delegation_id=call.delegation_id,
            arguments=arguments, now=time.monotonic(),
        )
        # Sorted, like arg_keys: a stable line compares across turns.
        record.dropped_keys = sorted(dropped)
        registered = self.has_function(call.name)
        self.calls.mark_dispatched(record, registered)
        logger.info(
            f"🔧 live backend calls {call.name} ({call.call_id}) "
            f"args={record.arg_keys or '-'}"
            + (f" (dropped placeholder {','.join(dropped)})" if dropped else "")
            + ("" if registered else " — NO HANDLER REGISTERED")
        )
        if not registered:
            # pipecat would log and drop it, leaving the delegation open
            # forever. Answer the model instead so the turn can finish.
            await self._submit_result(
                call.call_id,
                {"error": f"The tool {call.name} is not available in this session."},
                outcome="no handler registered",
            )
            return
        # From this point a handler may perform a side effect. If the session
        # dies before reply audio is accepted, the original audio must not be
        # replayed into a fresh model session.
        self._unsafe_to_replay_input = True
        refusal = self.actions.check(call.name, arguments)
        if refusal is not None:
            logger.info(
                f"🔁 live refused a repeated action {call.name} ({call.call_id}): "
                f"answered from the per-utterance ledger, nothing sent to Home Assistant"
            )
            await self._submit_result(call.call_id, refusal,
                                      outcome="refused as a repeat")
            return
        # Bounded like FunctionCallLog: a call abandoned mid-flight (a reconnect
        # killed its handler) never pops its entry, and a session runs for an
        # hour. Oldest first, because a call still waiting is the recent one.
        while len(self._in_flight) >= IN_FLIGHT_LIMIT:
            self._in_flight.pop(next(iter(self._in_flight)))
        self._in_flight[call.call_id] = (call.name, arguments)
        try:
            await self.run_function_calls([
                FunctionCallFromLLM(
                    function_name=call.name,
                    tool_call_id=call.call_id,
                    arguments=arguments,
                    context=self._context or LLMContext(),
                )
            ])
        except Exception as e:
            logger.error(f"❌ live could not start {call.name} ({call.call_id}): {e!r}")
            await self._submit_result(
                call.call_id, {"error": f"The tool {call.name} could not be started."},
                outcome=f"dispatch failed: {e!r}",
            )

    async def _handle_function_call_result(self, frame: FunctionCallResultFrame) -> None:
        """Classify what the tool answered before handing it back to the model.

        pipecat treats any returned string as success, so Home Assistant's
        ``Error calling tool: Received invalid slot info for HassTurnOn`` went
        back to the model untouched and was logged as "completed successfully".
        The model then had to guess, and guessed by firing a second action.
        """
        call_id = frame.tool_call_id
        future = self._router_futures.get(call_id)
        if future is not None:
            if not future.done():
                future.set_result(frame.result)
            return
        if (not self.ledger.has_open(call_id)
                or self._evidence.calls.get(call_id) != self._evidence.generation):
            return
        name, arguments = self._in_flight.pop(call_id, (frame.function_name, None))
        outcome = classify_result(frame.result)
        self.actions.record(name, arguments, outcome)
        if outcome.pending_confirmation and isinstance(frame.result, dict):
            confirm_id = frame.result.get("confirm_id")
            if confirm_id:
                self._confirmation_prompts_pending.add(str(confirm_id))
        if not outcome.ok and not outcome.pending_confirmation:
            logger.warning(
                f"⚠️ live tool {name} reported {outcome.kind}: {outcome.log_detail}"
            )
        result = explain_for_model(name, frame.result, outcome)
        await self._submit_result(call_id, result, outcome="result submitted",
                                  verdict=outcome.describe())

    async def _submit_result(self, call_id: str, result: Any, outcome: str,
                             verdict: str = "") -> None:
        if self._evidence.calls.get(call_id) != self._evidence.generation:
            return
        output = json.dumps(result, ensure_ascii=False) if result is not None else "COMPLETED"
        payload = function_call_output(call_id, output)
        event_id = str(payload.get("event_id") or "")
        if event_id:
            self._pending_result_events[event_id] = call_id
            while len(self._pending_result_events) > IN_FLIGHT_LIMIT:
                self._pending_result_events.pop(next(iter(self._pending_result_events)))
        submitted = await self._send(payload)
        if not submitted:
            self._pending_result_events.pop(event_id, None)
            record = self.calls.mark_result(
                call_id, now=time.monotonic(), submitted=False, continued=False,
                outcome="result delivery failed; outcome may be unknown",
            )
            if record is not None and verdict:
                record.result = verdict
            return
        if not self._evidence.result(call_id):
            return
        continued = self.ledger.complete(call_id)
        continuation_sent = False
        if continued:
            continuation_sent = await self._send_continuation(
                record.delegation_id if (record := self.calls._by_id.get(call_id)) else None
            )
        record = self.calls.mark_result(
            call_id, now=time.monotonic(), submitted=True,
            continued=continued and continuation_sent,
            outcome=outcome,
        )
        if record is not None and verdict:
            record.result = verdict
        if continued and continuation_sent and not self.ledger.busy:
            # Result submission is not completed continuation evidence.
            self._backend_answer_ready = False
            # The captured event order is output_item.done -> response.completed
            # -> (our result), so the backend response usually finishes while
            # the tool is still running and _on_response_event cannot release
            # the delegation then. Release it here instead; otherwise the
            # thinking watchdog and `response_active` stay held for
            # DELEGATION_TIMEOUT_S after a perfectly successful action.
            self._finish_delegation(record.delegation_id if record else None)

    # ------------------------------------------------------------------
    # router mode (client delegation; app/live_router.py)
    #
    # The app routes every utterance once, when its transcript closes or the
    # voice delegates, whichever is first. The voice may acknowledge at once;
    # anything else it says before the verified result is held until the
    # router's verdict, then played (no tool was needed) or dropped (a result
    # is coming). The result goes in as one commentary append and is spoken
    # as it streams.
    # ------------------------------------------------------------------

    def schedule_progress_ack(self) -> None:  # type: ignore[override]
        """Router mode: the voice acknowledges itself; see _fallback_ack_watch."""
        if self.router is not None:
            return
        super().schedule_progress_ack()

    def _schedule_slow_tool_ack(self, function_name: str):  # type: ignore[override]
        if self.router is not None:
            return None
        return super()._schedule_slow_tool_ack(function_name)

    def _turn_meta(self) -> Optional[dict]:
        timeline = self.turn_timeline
        turn = timeline.current if timeline is not None else None
        return turn.meta if turn is not None else None

    def _stopped_ms(self) -> Optional[int]:
        """Milliseconds since this turn's speech_stopped stamp (the user turn
        closing), on the timeline's own clock: the same reference point the
        turn summary's speech_end_to_first_audio_sent_ms uses."""
        timeline = self.turn_timeline
        turn = timeline.current if timeline is not None else None
        if turn is None or "speech_stopped" not in turn.stamps:
            return None
        return int((timeline._clock() - turn.stamps["speech_stopped"]) * 1000)

    def _since_speech_end_ms(self, now: Optional[float] = None) -> Optional[int]:
        if self._speech_end_mono is None:
            return None
        return int(((now or time.monotonic()) - self._speech_end_mono) * 1000)

    async def _router_on_user_speech(self, continuation: bool) -> None:
        op = self._op
        if continuation:
            # More of the same request. Work that has not touched the house yet
            # is cancelled and the merged text is routed when it closes; work
            # that has already run a tool finishes in the background, and the
            # merged text is routed as its own request (or joins it, when it
            # is the same request again).
            if (op is not None and not op.done and not op.tools_started and not op.background
                    and op.task is not None):
                logger.info(f"🧩 router: {op.op_id} cancelled before any tool ran; the "
                            "request continued and is routed again when it closes")
                op.abandoned = True
                op.task.cancel()
                self._op = None
            await self._cancel(self._fallback_task)
            self._fallback_task = None
            return
        await self._router_supersede("new utterance")

    async def _router_supersede(self, reason: str) -> None:
        op, self._op = self._op, None
        if op is not None and op.done:
            # b11 (twelfth review B11), merged with the task list: a finished
            # op's unheard question is dropped (on a disconnect too, without
            # the spoken line). A running op that is detached below has its
            # confirmations withdrawn in _task_detach (longtask review B1).
            self._drop_unheard_confirmations(op, announce=reason != "disconnect")
        if op is not None and not op.done and op.task is not None:
            if op.tools_started or op.background:
                # NEVER cancelled, not even on a disconnect: a tool has run,
                # or may be running right now. Cancelling here is what dropped
                # a 30 s result on the floor and let an impatient repeat run
                # the action twice (audit 2026-10-12 §2.2). It finishes in the
                # background and its result is delivered (app/live_tasks.py).
                self._task_detach(op, reason)
            else:
                logger.info(f"🧭 router: {op.op_id} superseded ({reason}) before any tool ran")
                op.abandoned = True
                op.task.cancel()
        await self._cancel(self._fallback_task)
        self._fallback_task = None
        await self._cancel(self._progress_task)
        self._progress_task = None
        await self._cancel(self._gate_release_task)
        self._gate_release_task = None
        dropped = len(self._gate_lag) + len(self._gate_held)
        if dropped:
            self._gate_dropped += dropped
        self._gate_turn_text = ""
        self._gate_reset("idle" if reason == "disconnect" else "pre")
        self._speech_end_mono = None
        self._turn_heard_mono = None
        self._commentary_sent_mono = None

    def _arm_router_silent_close(self) -> None:
        """Close the turn silently if the voice says nothing (accidental wake,
        background speech): the same end the 14 s watch gave such turns,
        without waiting for the thinking watchdog."""
        task = self._silent_task
        if task is not None and not task.done():
            task.cancel()
        self._silent_task = self._spawn(self._router_silent_close(), "live-router-silent")

    async def _router_silent_close(self) -> None:
        started = time.monotonic()
        await asyncio.sleep(ROUTER_SILENT_CLOSE_S)
        op = self._op
        if (self._user_turn.open or (op is not None and not op.done)
                or (self._turn_heard_mono is not None and self._turn_heard_mono >= started)
                or self.audio.speaking or self._audio_queue.qsize()):
            return
        logger.info(f"🤐 router: nothing to say {ROUTER_SILENT_CLOSE_S:.0f}s after an "
                    "unrouted or no-request utterance; closing the turn silently")
        self._note(self.on_silent_response)

    def _gate_reset(self, mode: str) -> None:
        self._gate = mode
        self._result_acked = False
        self._result_heard = False
        self._gate_lag.clear()
        self._gate_held.clear()
        self._gate_text = ""
        self._gate_blocked = False
        self._gate_drop_until_ack = False
        self._gate_ack_end_ms = None
        self._gate_timeline_seen = False

    async def _router_start(self, text: str) -> None:
        """Route the utterance that just closed. Once per utterance."""
        if self._routing_suspended:
            return  # the session is closing; a half transcript is not a request
        seq = self.turn_timeline.user_turn_seq if self.turn_timeline is not None else 0
        op = self._op
        if op is not None and op.seq == seq and op.text == text:
            return  # this utterance is already routed (the delegation came first)
        self._speech_end_mono = self._last_user_delta_mono or time.monotonic()
        short_answer = bool(re.fullmatch(r"\W*(?:yes|yeah|yep|no|nope|ok|okay)\W*", text, re.I))
        if len(text) < ROUTE_MIN_CHARS and not self._confirmation_awaiting and not short_answer:
            logger.info(f"🧭 router: not routing a {len(text)}-character transcript (accidental wake)")
            self._gate_open()
            self._task_flush_soon("short utterance")
            self._arm_router_silent_close()
            return
        if op is not None and not op.done:
            if op.tools_started or op.background:
                self._task_detach(op, "the request continued")
            elif op.task is not None:
                op.abandoned = True
                op.task.cancel()
        key = normalize_request(text)
        joined = self._task_find_joinable(key)
        if joined is None and op is not None and op.background and text.startswith(op.text):
            # An impatient repeat inside the fragment-merge window arrives
            # as "<request><request>": the tail is the same request again.
            if normalize_request(text[len(op.text):]) == op.key:
                joined = self._task_find_joinable(op.key)
        if joined is not None:
            await self._task_join(joined, seq, text)
            return
        self._op_count += 1
        op = RouterOp(op_id=f"op{self._op_count}-{new_event_id('r')[-6:]}", seq=seq, text=text,
                      started_mono=time.monotonic(), key=key)
        self._op = op
        self.router_stats["routed"] += 1
        self._gate_text = ""
        self._gate_blocked = False
        self._gate_ack_end_ms = None
        self._gate_timeline_seen = False
        if self._gate != "pre":
            self._gate_lag.clear()
            self._gate_held.clear()
        self._gate = "pre"
        meta = self._turn_meta()
        if meta is not None:
            meta["route_op"] = op.op_id
        op.task = self._spawn(self._router_run(op), f"live-route-{op.op_id}")
        # The classifier's call-time veto is taken before the flag below is
        # cleared: a reply to a pending confirmation is never fast chat.
        self._start_chat_classifier(op)
        # This utterance is the reply to a pending confirmation, if one was
        # asked; the router and the action gate decide what it means.
        self._confirmation_awaiting = False
        await self._cancel(self._fallback_task)
        self._fallback_task = self._spawn(self._fallback_ack_watch(op), "live-fallback-ack")
        await self._cancel(self._progress_task)
        self._progress_task = None
        # After the soft budget the request becomes a background task: the
        # voice says so, the gate opens and the session may close (the old
        # in-place narration, _router_progress_watch, held both).
        op.watch = self._spawn(self._task_promote_watch(op), f"live-task-watch-{op.op_id}")

    # -- the fast chat path (app/live_classifier.py) ----------------------
    #
    # A cheap tool-less verdict runs beside the full route. Only CHAT can act,
    # and only when every check below agrees; anything else (REQUEST,
    # NO_REQUEST, a timeout, an error, a veto) leaves the turn exactly as the
    # backend's verdict would have handled it. The classifier is a Responses
    # call: it never opens, keeps open, or extends a billed Live session (no
    # liveness tick, no backend-activity note, not part of router_pending).

    def _chat_call_veto(self, text: str) -> Optional[str]:
        """Why no classifier call should be made for this text at all."""
        tool_names = getattr(getattr(self.router, "backend", None), "tool_names", []) or []
        veto = chat_fast_path_text_veto(text, tool_names) or self._chat_context_veto()
        if veto is None and self.router_allowed is not None and not self.router_allowed():
            veto = "spend cutoff"
        return veto

    async def _speculative_classify(self) -> None:
        """Classify the open user turn after a short pause, ahead of its close.
        A Responses call only: no Live session is opened or extended."""
        await asyncio.sleep(CHAT_SPECULATIVE_PAUSE_S)
        turn = self._user_turn
        text = (turn.text or "").strip()
        if (not turn.open or not text or len(text) < ROUTE_MIN_CHARS
                or self._spec_count >= CHAT_SPECULATIVE_MAX or self._routing_suspended):
            return
        if self._spec_classify is not None and self._spec_classify[0] == text:
            return
        if self._chat_call_veto(text) is not None:
            return
        self._spec_count += 1
        context = self._classifier_context()
        task = asyncio.ensure_future(self.chat_classifier.classify(text, context))
        self._spec_classify = (text, task)

    def _classifier_context(self) -> str:
        try:
            return self.router.last_assistant() if hasattr(self.router, "last_assistant") else ""
        except Exception:  # pragma: no cover - defensive
            return ""

    def _start_chat_classifier(self, op: RouterOp) -> None:
        classifier = self.chat_classifier
        if classifier is None or self.router is None:
            return
        spec, self._spec_classify = self._spec_classify, None
        veto = self._chat_call_veto(op.text)
        if veto is not None:
            # No call is made: the wording already says this is not plain chat.
            op.fast_veto = veto
            if spec is not None and not spec[1].done():
                spec[1].cancel()
            return
        pending = None
        if spec is not None:
            if spec[0] == op.text.strip():
                pending = spec[1]  # the same words, already being classified
            elif not spec[1].done():
                spec[1].cancel()
        op.classifier_task = self._spawn(self._chat_classifier_run(op, pending), f"live-classify-{op.op_id}")

    def _chat_context_veto(self) -> Optional[str]:
        """The conversation, not the words, rules chat out: the assistant just
        asked a question, or a spoken yes is awaited."""
        if self._last_route_kind in (ROUTE_CONFIRM, ROUTE_REPLY):
            return "answer to the assistant's question"
        if self._confirmation_awaiting:
            return "confirmation pending"
        gate = getattr(self, "action_gate", None)
        has_pending = getattr(gate, "has_pending", None)
        if callable(has_pending):
            try:
                if has_pending(self.device_id):
                    return "confirmation pending"
            except Exception:  # pragma: no cover - defensive: fail closed
                return "confirmation state unknown"
        if self._confirmation_prompts_pending:
            return "confirmation pending"
        return None

    async def _intent_engine_veto(self, op: RouterOp) -> Optional[str]:
        """Home Assistant's intent engine matched this utterance (it runs first
        inside route()): it is a command, never chat. Undecided = no fast path."""
        check = getattr(self.router, "intent_matched", None)
        if not callable(check):
            return None
        try:
            matched = await check(op.text, CHAT_INTENT_WAIT_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive: fail closed
            return "intent engine state unknown"
        if matched is None:
            return "intent engine undecided"
        return "the home's intent engine matched" if matched else None

    def _chat_fast_path_veto(self, op: RouterOp) -> Optional[str]:
        """Checked at the moment a CHAT verdict arrives."""
        if self._op is not op or op.done:
            return "verdict already in"
        if op.tools_started:
            return "a tool already ran"
        if op.background:
            return "a background task"
        if any(t.explain and not t.delivered for t in self._tasks.values()):
            # The voice offered a text for a pending explanation; the user's
            # answer ("text would be great") must reach the router.
            return "an explanation is pending"
        if self._gate != "pre":
            return f"gate {self._gate}"
        if op.delegations:
            return "the voice delegated"
        if self._user_turn.open:
            return "the user is still speaking"
        held = (self._gate_turn_text or "") + " " + (self._gate_text or "")
        if claims_success(held) or claims_action(held):
            return "held speech claims an outcome"
        return self._chat_context_veto()

    async def _chat_classifier_run(self, op: RouterOp, pending=None) -> None:
        if pending is not None:
            try:
                verdict = await pending
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - classify() never raises
                from app.live_classifier import Verdict
                verdict = Verdict("UNKNOWN", 0, f"error {e!r}"[:120])
        else:
            verdict = await self.chat_classifier.classify(op.text, self._classifier_context())
        op.classifier = verdict.label
        op.classifier_ms = verdict.ms
        if verdict.label != CLASSIFIED_CHAT:
            logger.info(f"🏷️ classifier {op.op_id}: {verdict.label} in {verdict.ms} ms"
                        + (f" ({verdict.reason})" if verdict.reason else "") + "; the route decides")
            return
        veto = await self._intent_engine_veto(op)
        veto = veto or self._chat_fast_path_veto(op)
        if veto is not None:
            op.fast_veto = veto
            logger.info(f"🏷️ classifier {op.op_id}: CHAT in {verdict.ms} ms, not used ({veto})")
            return
        # Chat: no tool is wanted. Stop the full route before it can reach a
        # tool (none has started: checked above, synchronously with this
        # cancel) and answer as the backend's CHAT verdict would.
        task = op.task
        if task is not None and not task.done():
            task.cancel()
        op.result = RouteResult(CHAT_KIND, "", "classifier", [],
                                {"classifier_ms": verdict.ms,
                                 "route_ms": int((time.monotonic() - op.started_mono) * 1000)})
        op.done = True
        self.router_stats["fast_chat"] += 1
        try:
            self.router.note_chat(op.text)
        except Exception:  # pragma: no cover - defensive
            pass
        logger.info(f"⚡ classifier {op.op_id}: CHAT in {verdict.ms} ms; gate opened before the backend's verdict")
        await self._router_deliver(op)

    async def _router_run(self, op: RouterOp) -> None:
        liveness = self.turn_liveness
        if liveness is not None:
            liveness.tool_started()
        self._note(self.on_backend_activity)
        try:
            if self.router_allowed is not None and not self.router_allowed():
                result = RouteResult(ROUTE_FAILED, "Today's voice usage limit has been reached.", "none")
            else:
                result = await asyncio.wait_for(
                    self.router.route(op.text, lambda name, args: self._router_execute(op, name, args)),
                    ROUTE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ router failed for {op.op_id}: no result within {ROUTE_TIMEOUT_S:.0f}s")
            result = RouteResult(ROUTE_FAILED, "That took too long and was stopped; it may not have finished.",
                                 "none")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ router failed for {op.op_id}: {e!r}")
            result = RouteResult(ROUTE_FAILED, "Something went wrong while handling that.", "none")
        finally:
            if liveness is not None:
                liveness.tool_finished()
        if op.abandoned:
            return
        op.result = result
        op.done = True
        op.result_ready_mono = time.monotonic()
        self.router_stats["local" if result.source == "ha_local" else "backend"] += 1
        if op.background or self._op is not op:
            if not op.background:
                self._task_detach(op, "overtaken by newer speech")
            await self._task_complete(op)
            return
        self._note(self.on_backend_activity)
        await self._router_deliver(op)

    async def _router_execute(self, op: RouterOp, name: str, arguments: dict):
        """Run one tool for the router through the guarded handler path.

        Returns ``(result for the backend, ToolOutcome)``. The per-utterance
        repeat ledger answers an exact repeat without touching the house.
        """
        arguments, dropped = sanitize_tool_arguments(name, arguments, self._tool_schemas.get(name))
        call_id = new_event_id("rcall")
        record = self.calls.observe(call_id=call_id, name=name, delegation_id=op.op_id,
                                    arguments=arguments, now=time.monotonic())
        record.dropped_keys = sorted(dropped)
        registered = self.has_function(name)
        self.calls.mark_dispatched(record, registered)
        logger.info(f"🔧 router {op.op_id} calls {name} ({call_id}) args={record.arg_keys or '-'}"
                    + (f" (dropped placeholder {','.join(dropped)})" if dropped else "")
                    + ("" if registered else " — NO HANDLER REGISTERED"))
        if not registered:
            raw = {"error": f"The tool {name} is not available in this session."}
            outcome = classify_result(raw)
            self.calls.mark_result(call_id, now=time.monotonic(), submitted=True, continued=True,
                                   outcome="no handler registered")
            return explain_for_model(name, raw, outcome), outcome
        op.tools_started = True
        if SLOW_TOOL.search(name):
            op.slow_tool = True
        if name.rsplit("__", 1)[-1] == "ask_openclaw" and \
                str((arguments or {}).get("mode") or "").strip().lower() == "explain":
            op.explain_route = True
            op.speaker = op.speaker or self._speaker_first_name()
        self._unsafe_to_replay_input = True
        running_key = json.dumps([name, arguments], sort_keys=True, default=str)
        running = self._router_running.get(running_key)
        if running is not None and not running.done():
            logger.info(f"🔁 router {op.op_id}: {name} ({call_id}) is identical to a call still "
                        "running; waiting for that one instead of running it again")
            try:
                raw, outcome = await asyncio.shield(running)
            except asyncio.CancelledError:
                raise
            except Exception:
                raw = {"error": f"{name} is already running for this request and its outcome "
                                "is unknown. Do not retry automatically."}
                outcome = classify_result(raw)
            self.calls.mark_result(call_id, now=time.monotonic(), submitted=True, continued=True,
                                   outcome="joined a running identical call")
            return explain_for_model(name, raw, outcome), outcome
        refusal = self.actions.check(name, arguments)
        if refusal is not None:
            logger.info(f"🔁 router refused a repeated action {name} ({call_id}): "
                        "answered from the per-utterance ledger, nothing sent to Home Assistant")
            outcome = classify_result(refusal)
            self.calls.mark_result(call_id, now=time.monotonic(), submitted=True, continued=True,
                                   outcome="refused as a repeat")
            return refusal, outcome
        future = asyncio.get_running_loop().create_future()
        self._router_futures[call_id] = future
        shared = asyncio.get_running_loop().create_future()
        self._router_running[running_key] = shared
        while len(self._in_flight) >= IN_FLIGHT_LIMIT:
            self._in_flight.pop(next(iter(self._in_flight)))
        self._in_flight[call_id] = (name, arguments)
        try:
            await self.run_function_calls([FunctionCallFromLLM(
                function_name=name, tool_call_id=call_id, arguments=arguments,
                context=self._context or LLMContext(),
            )])
            raw = await asyncio.wait_for(future, DELEGATION_TIMEOUT_S)
        except asyncio.TimeoutError:
            raw = {"error": f"{name} did not answer in time; it may or may not have completed. "
                            "Do not retry automatically."}
        except Exception as e:
            logger.error(f"❌ router could not start {name} ({call_id}): {e!r}")
            raw = {"error": f"The tool {name} could not be started."}
        except BaseException:
            # Cancellation (a superseding utterance, a close): release the
            # running-call entry and fail any joiner closed, so an identical
            # later request runs instead of waiting on a call nobody awaits.
            self._router_futures.pop(call_id, None)
            self._in_flight.pop(call_id, None)
            if self._router_running.get(running_key) is shared:
                self._router_running.pop(running_key, None)
            if not shared.done():
                shared.set_exception(RuntimeError("cancelled"))
                shared.exception()  # retrieved: a joiner may never come
            raise
        self._router_futures.pop(call_id, None)
        self._in_flight.pop(call_id, None)
        outcome = classify_result(raw)
        self.actions.record(name, arguments, outcome)
        if not shared.done():
            shared.set_result((raw, outcome))
        if self._router_running.get(running_key) is shared:
            self._router_running.pop(running_key, None)
        if (outcome.pending_confirmation and isinstance(raw, dict) and raw.get("confirm_id")
                and (op.background or self._op is not op)):
            # Longtask review B1: an op that already left the foreground (it
            # was talked over, or promoted to the background) can never hold
            # an action for a yes. Its question would not be voiced in turn,
            # and newer audio could arm it. Withdrawn at once, in the action
            # gate too; the result says it was not done.
            op.confirm_ids.add(str(raw["confirm_id"]))
            self._withdraw_confirmations(op)
        elif outcome.pending_confirmation and isinstance(raw, dict) and raw.get("confirm_id"):
            self._confirmation_prompts_pending.add(str(raw["confirm_id"]))
            op.confirm_ids.add(str(raw["confirm_id"]))
            # The user's next reply, however short ("Yes" with no
            # punctuation), must reach the router, never the voice alone.
            self._confirmation_awaiting = True
        if outcome.pending:
            self._register_external_task(op, name, outcome.task_id or call_id,
                                         deferred=outcome.deferred or is_deferred_request(op.text),
                                         explain=bool(getattr(outcome, "explain", False)))
        if not outcome.ok and not outcome.pending_confirmation:
            logger.warning(f"⚠️ router tool {name} reported {outcome.kind}: {outcome.log_detail}")
        record = self.calls.mark_result(call_id, now=time.monotonic(), submitted=True,
                                        continued=True, outcome="router result")
        if record is not None:
            record.result = outcome.describe()
        return explain_for_model(name, raw, outcome), outcome

    def _withdraw_confirmations(self, op: RouterOp) -> None:
        """A held action whose question was overtaken by newer speech is never
        armed: a "yes" could otherwise answer a question nobody heard yet
        (eleventh review B9). Its result says it was not done."""
        if not op.confirm_ids:
            return
        op.confirm_withdrawn = True
        self._confirmation_prompts_pending.difference_update(op.confirm_ids)
        # merge (v35): the held action itself is withdrawn too, so a fence
        # armed by some other reply in the meantime can never be taken.
        gate = getattr(self, "action_gate", None)
        withdraw = getattr(gate, "withdraw", None)
        if callable(withdraw):
            for confirm_id in op.confirm_ids:
                withdraw(confirm_id)
        logger.info(f"🔐 router: {op.op_id} confirmation withdrawn (overtaken by newer speech "
                    "or finished in the background)")
        if op.result is not None and op.result.kind == "confirm":
            op.result = RouteResult(ROUTE_FAILED, "That earlier request needed a spoken yes first and "
                                    "was not done; the user can ask for it again.", op.result.source,
                                    op.result.tools, op.result.timings)
        op.confirm_ids.clear()

    def _drop_unheard_confirmations(self, op: RouterOp, announce: bool = True) -> None:
        """A finished operation's question was sent but not yet heard when a
        new utterance began; its audio is dropped with the reset gate. Its
        held action is never armed (and is withdrawn from the action gate),
        and the voice says it was not done (twelfth review B11). A question
        already heard is armed and no longer pending, so it is untouched.
        The line goes through the task list, after the new words' verdict."""
        unheard = op.confirm_ids & self._confirmation_prompts_pending
        if not unheard:
            return
        self._confirmation_prompts_pending.difference_update(unheard)
        op.confirm_ids.difference_update(unheard)
        op.confirm_withdrawn = True
        gate = getattr(self, "action_gate", None)
        withdraw = getattr(gate, "withdraw", None)
        if callable(withdraw):
            for confirm_id in unheard:
                withdraw(confirm_id)
        logger.info(f"🔐 router: {op.op_id} confirmation withdrawn (its question was not heard "
                    "before a new utterance)")
        if not announce or self.router is None:
            return
        now = time.monotonic()
        note = RouterOp(op_id=f"{op.op_id}-unheard", seq=op.seq, text=op.text, started_mono=op.started_mono,
                        tools_started=True, background=True, done=True, result_ready_mono=now,
                        confirm_withdrawn=True)
        note.result = RouteResult(ROUTE_FAILED, "That needed a spoken yes first and was not done; "
                                  "the user can ask again.", op.result.source if op.result else "none")
        self._tasks[note.op_id] = note
        self._task_flush_soon("unheard confirmation withdrawn")

    async def _router_deliver(self, op: RouterOp) -> None:
        result = op.result
        if self._user_turn.open:
            # The user has started speaking again (a continuation within the
            # merge window). Opening the gate now would let the voice's guess
            # at the new words play (tenth review B8); this result becomes a
            # background task, delivered once the session is free for it
            # (_session_free_for_delivery), after the new words' verdict.
            if op.classifier_task is not None and not op.classifier_task.done():
                op.classifier_task.cancel()
            self._task_detach(op, "the user is speaking again")
            logger.info(f"🧭 router: {op.op_id} result held until the user's new words are routed")
            await self._task_complete(op)
            return
        if result is not None and result.kind == ROUTE_NOTHING and self._turn_heard_mono is not None:
            # The user already heard an acknowledgement, so they asked
            # something; silence now leaves the device "thinking" until the
            # 20 s watchdog (Kitchen 2026-10-13: "Turn on the kitchen lights"
            # came back NO_REQUEST, 45 s of nothing). Say it was not caught.
            logger.warning(f"🧭 router: {op.op_id} found no request after an acknowledgement was heard; "
                           "saying it was not caught")
            result = op.result = RouteResult(ROUTE_REPLY, NOT_CAUGHT_TEXT, result.source, [],
                                             result.timings)
        if result is not None and (result.kind == ROUTE_NOTHING or (
                result.kind == ROUTE_REPLY and not result.tools
                and _NOT_CAUGHT_REPLY.search(result.commentary() or ""))):
            now_nc = time.monotonic()
            self._not_caught.append(now_nc)
            while self._not_caught and now_nc - self._not_caught[0] > BACKGROUND_NOT_CAUGHT_WINDOW_S:
                self._not_caught.popleft()
            if len(self._not_caught) >= BACKGROUND_MAX_NOT_CAUGHT and self._close_for_background(
                    f"{len(self._not_caught)} utterances with no request in them within "
                    f"{BACKGROUND_NOT_CAUGHT_WINDOW_S:.0f}s"):
                # Not cleared: a false wake into the same talk closes at its
                # first not-caught utterance.
                await self._task_complete(op)
                return
        elif result is not None and result.verified:
            self._not_caught.clear()  # a real request was served
        text = result.commentary() if result is not None else None
        if result is not None and result.kind == ROUTE_PENDING and result.explain and not result.deferred:
            # The explain acknowledgement, with the identified speaker's name.
            op.explain_ack_sent = True
            text = explain_ack_commentary(op.speaker or self._speaker_first_name())
        now = time.monotonic()
        self._last_route_kind = result.kind if result is not None else None
        if op.classifier_task is not None and not op.classifier_task.done():
            op.classifier_task.cancel()  # the verdict is in; the guess is moot
        if text is None and (result is None or result.kind != CHAT_KIND):
            # Nothing was asked: whatever the voice says in this turn answers
            # nothing. Held speech is dropped, and so is the rest of it, until
            # the user speaks again (fifth review N16).
            dropped = len(self._gate_lag) + len(self._gate_held)
            self._log_held_sample("dropped, no request")
            self._gate_reset("idle")
            if dropped:
                self._gate_dropped += dropped
                logger.info(f"⏹️ live dropped {dropped} held chunks: no request was found")
        if text is None:
            # No tool was needed (chat): the voice's own speech, held so
            # far, is released now in order.
            note = ("No tool was needed for this. Answer the user yourself, briefly."
                    if result is not None and result.kind == CHAT_KIND
                    else "There is no request to act on. Say nothing.")
            for did in op.delegations:
                if did not in op.answered_delegations:
                    op.answered_delegations.add(did)
                    await self._send_commentary(note, did)
            if result is not None and result.kind == CHAT_KIND:
                self._gate_open()
            if self._turn_heard_mono is None:
                self._arm_router_silent_close()
        else:
            did = next((d for d in reversed(op.delegations) if d not in op.answered_delegations), None)
            if did is not None:
                op.answered_delegations.update(op.delegations)
            if self._gate_post() and DROPPED_SPEECH_NOTE:
                # The voice believes it said what was dropped; tell it the
                # user did not hear it, in the same append (improvement 5).
                text = DROPPED_SPEECH_NOTE + " " + text
            await self._send_commentary(text, did)
            op.result_sent_mono = time.monotonic()
        self._log_route(op, now)
        self._task_flush_soon("result delivered")

    def _log_route(self, op: RouterOp, now: float, meta: bool = True) -> None:
        result = op.result
        if result is None:
            return
        timings = dict(result.timings)
        steps = timings.pop("backend_steps", None) or []
        summary = {
            "op": op.op_id, "kind": result.kind, "source": result.source,
            "tools": [f"{r.name.rsplit('__', 1)[-1]}:{r.kind}" for r in result.tools],
            "delegated": len(op.delegations),
            "route_ms": int((now - op.started_mono) * 1000),
            "speech_end_to_result_ms": self._since_speech_end_ms(now),
            "backend_usd_estimate": round(sum(float(st.get("usd") or 0) for st in steps), 5),
            "backend_calls": [{k: st.get(k) for k in ("s", "in", "cached", "out", "tier", "calls")}
                              for st in steps],
        }
        if op.classifier is not None or op.fast_veto is not None:
            summary["classifier"] = op.classifier
            summary["classifier_ms"] = op.classifier_ms
            if op.fast_veto:
                summary["fast_veto"] = op.fast_veto
        summary.update({k: v for k, v in timings.items() if k not in ("route_ms",)})
        if op.background:
            summary["background"] = True
            summary["joins"] = op.joins
        logger.info("🧭 router " + json.dumps(summary, separators=(",", ":"), default=str))
        meta = self._turn_meta() if meta else None
        if meta is not None:
            meta["route_kind"] = result.kind
            meta["route_source"] = result.source
            meta["speech_end_to_result_ms"] = summary["speech_end_to_result_ms"]
            meta["backend_calls"] = len(steps)
            meta["backend_cached_tokens"] = [st.get("cached") for st in steps]
            if op.classifier is not None:
                meta["classifier"] = op.classifier
                meta["classifier_ms"] = op.classifier_ms

    async def _router_on_delegation(self, did: str, target) -> None:
        """The voice delegated. It never causes work by itself: the delegation
        attaches to this utterance's operation (routing it now if the
        transcript is still open) and is answered with that result."""
        logger.info(f"🧠 live delegation {did or '?'} → {target} (router mode)")
        self._note(self.on_backend_activity)
        timeline = self.turn_timeline
        if timeline is not None:
            timeline.mark("response_created")
        if self._user_turn.open:
            await self._cancel(self._user_turn.timer)
            self._user_turn.timer = None
            await self._end_turn("user")
        op = self._op
        if op is None:
            pending = next((t for t in reversed(list(self._tasks.values())) if not t.done), None)
            if pending is not None and did:
                # The voice delegated for a request that is already a
                # background task: answer it truthfully, run nothing.
                pending.delegations.append(did)
                pending.answered_delegations.add(did)
                self._gate_open()
                await self._send_commentary(progress_commentary(pending.text), did)
                return
            self._gate_open()
            await self._send_commentary(
                "No request was heard clearly. Ask the user to say it again.", did or None)
            return
        if did:
            op.delegations.append(did)
        if not op.done:
            return
        if did and did not in op.answered_delegations:
            op.answered_delegations.add(did)
            text = op.result.commentary() if op.result is not None else None
            if op.result_sent_mono is not None:
                text = "That request has already been answered above. Do not repeat it; say nothing."
            elif text is None:
                text = ("No tool was needed for this. Answer the user yourself, briefly."
                        if op.result is not None and op.result.kind == CHAT_KIND
                        else "There is no request to act on. Say nothing.")
            await self._send_commentary(text, did)

    async def _send_commentary(self, text: str, delegation_id: Optional[str]) -> None:
        for chunk in chunk_text(text):
            payload = context_append("commentary", chunk, delegation_id)
            self._pending_appends[payload["event_id"]] = time.monotonic()
            while len(self._pending_appends) > IN_FLIGHT_LIMIT:
                self._pending_appends.pop(next(iter(self._pending_appends)))
            await self._send_or_queue(payload)
        self._commentary_sent_mono = time.monotonic()
        logger.info(f"🗣️ router commentary sent ({len(text)} chars"
                    + (f", delegation {delegation_id}" if delegation_id else ", session-wide") + ")")

    def _on_appended(self, evt: dict) -> None:
        kind = evt.get("type")
        ref = evt.get("event_id") or evt.get("client_event_id")
        if kind == "session.commentary.appended":
            if ref in self._pending_appends:
                sent = self._pending_appends.pop(ref)
            elif self._pending_appends:
                sent = self._pending_appends.pop(next(iter(self._pending_appends)))
            else:
                sent = None
            if sent is not None:
                logger.info(f"🗣️ commentary acknowledged {int((time.monotonic() - sent) * 1000)} ms after sending")
            self._commentary_acked_mono = time.monotonic()
            # Whatever non-acknowledgement speech was still streaming when the
            # result went in has ended its claim on the speaker.
            self._gate_drop_until_ack = False
            if self._gate == "post":
                self._result_acked = True
        else:
            logger.debug(f"live: ack {kind}")

    # -- the pre-result gate -------------------------------------------------

    def _gate_push(self, chunk, end_ms: Optional[float] = None) -> None:
        if self._gate == "idle":
            self._gate_idle_dropped += 1
            self._need_segment_start = True
            if self._gate_idle_dropped == 1:
                logger.info("🔇 live audio before any user speech in this session dropped "
                            "(nobody asked anything yet)")
            return
        if self._gate == "pre":
            if self._gate_blocked:
                if len(self._gate_held) < GATE_HOLD_MAX_CHUNKS:
                    self._gate_held.append(chunk)
                else:
                    self._gate_dropped += 1
                return
            self._gate_lag.append((time.monotonic(), chunk,
                                   float(end_ms) if isinstance(end_ms, (int, float)) else None))
            if self._gate_release_task is None:
                self._gate_release_task = self._spawn(self._gate_release(), "live-gate-release")
            return
        if self._gate in ("post", "open") and self._gate_drop_until_ack:
            self._gate_dropped += 1
            self._need_segment_start = True
            return
        self._enqueue_audio(chunk)

    async def _gate_release(self) -> None:
        """Forward acknowledgement audio ACK_LAG_S behind its arrival."""
        try:
            while self._gate == "pre" and not self._gate_blocked and self._gate_lag:
                arrived, chunk, end_ms = self._gate_lag[0]
                wait = arrived + ACK_LAG_S - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                if acknowledgement_state(self._gate_text) != ACK:
                    # Only a complete acknowledgement is heard; a phrase still
                    # forming ("Sure, one…") waits for its next words.
                    await asyncio.sleep(0.05)
                    continue
                if not self._gate_timeline_allows(end_ms):
                    # The audio runs past the last word known to be part of
                    # the acknowledgement: wait for its transcript.
                    await asyncio.sleep(0.05)
                    continue
                if self._gate != "pre" or self._gate_blocked or not self._gate_lag:
                    break
                self._gate_lag.popleft()
                self._enqueue_audio(chunk)
        finally:
            self._gate_release_task = None

    def _gate_timeline_allows(self, end_ms: Optional[float]) -> bool:
        """Session-timeline check for one lagged chunk (improvement 5)."""
        if not GATE_TIMELINE or not self._gate_timeline_seen:
            return True  # no timestamped transcript: the v33 rule alone
        if end_ms is None:
            return False  # timestamps exist but this audio has no position: hold
        return self._gate_ack_end_ms is not None and end_ms <= self._gate_ack_end_ms

    def _gate_note_transcript(self, delta: str, end_ms=None) -> None:
        if self._gate != "pre":
            return
        self._gate_text += delta
        self._gate_turn_text = (self._gate_turn_text + delta)[-2000:]
        if isinstance(end_ms, (int, float)):
            self._gate_timeline_seen = True
            if not self._gate_blocked and acknowledgement_state(self._gate_text) == ACK:
                self._gate_ack_end_ms = float(end_ms)
        if self._gate_blocked or acknowledgement_state(self._gate_text) in (ACK, ACK_PREFIX):
            return
        self._gate_blocked = True
        self._gate_held.extend(chunk for _, chunk, _end in self._gate_lag)
        self._gate_lag.clear()
        logger.info(
            "⏸️ live speech held until the router's verdict: not an acknowledgement "
            f"[{len(self._gate_text.strip())} chars]"
        )

    def _log_held_sample(self, outcome: str) -> None:
        """One line per resolved pre-result gate: the voice's words so far
        (redacted) and what happened to them. Grammar tuning reads these."""
        if not LOG_HELD_SPEECH_SAMPLE:
            return
        text = (self._gate_text or "").strip()
        if not text:
            return
        state = "held" if self._gate_blocked else acknowledgement_state(text)
        logger.info("🔎 pre-result speech " + json.dumps(
            {"op": self._op.op_id if self._op is not None else None, "outcome": outcome,
             "state": state, "text": redact_for_log(text)}, separators=(",", ":"), ensure_ascii=False))

    def _gate_open(self) -> None:
        """No result is coming: play what was held, in order, and stream on."""
        chunks = [chunk for _, chunk, _end in self._gate_lag] + list(self._gate_held)
        if self._gate_blocked and is_non_speech(self._gate_text):
            chunks = []  # a held "[hum]" is not a reply
        self._log_held_sample("released" if self._gate_blocked else "streamed")
        self._gate_reset("open")
        for chunk in chunks:
            self._enqueue_audio(chunk)
        if chunks:
            logger.info(f"▶️ live held speech released ({len(chunks)} chunks): no tool was needed")

    def _gate_post(self) -> int:
        """A verified result is going in. Acknowledgement audio still in the
        lag plays; held speech (a guess at the outcome) is dropped unheard.
        Returns the number of chunks dropped."""
        # Only a complete acknowledgement still in the lag may play; a phrase
        # that never completed ("Absolutely.") is dropped with the guesses.
        if not self._gate_blocked and acknowledgement_state(self._gate_text) != ACK:
            self._gate_held = [chunk for _, chunk, _end in self._gate_lag] + list(self._gate_held)
            self._gate_lag.clear()
            self._gate_blocked = True
        if not self._gate_blocked and self._gate_lag:
            # Improvement 5: lagged audio past the acknowledgement's last
            # timestamped word is words whose transcript has not arrived yet:
            # they are dropped with the guesses, not played as "ack".
            keep: Deque = deque()
            while self._gate_lag and self._gate_timeline_allows(self._gate_lag[0][2]):
                keep.append(self._gate_lag.popleft())
            if self._gate_lag:
                self._gate_held = [chunk for _, chunk, _end in self._gate_lag] + list(self._gate_held)
                self._gate_blocked = True
            self._gate_lag = keep
            if self._gate_blocked:
                ack_now = [chunk for _, chunk, _end in keep]
                held_now = len(self._gate_held)
                self._need_segment_start = True
                self._log_held_sample("dropped past the acknowledgement")
                self._gate_reset("post")
                self._gate_drop_until_ack = True
                for chunk in ack_now:
                    self._enqueue_audio(chunk)
                self._gate_dropped += held_now
                self.router_stats["held_dropped"] += 1
                logger.info(f"⏹️ live dropped {held_now} chunks past the acknowledgement before the verified result")
                return held_now
        ack = [chunk for _, chunk, _end in self._gate_lag] if not self._gate_blocked else []
        held = len(self._gate_held) + (len(self._gate_lag) if self._gate_blocked else 0)
        if held:
            self._need_segment_start = True
        blocked = self._gate_blocked
        self._log_held_sample("dropped" if held else "ack played")
        self._gate_reset("post")
        self._gate_drop_until_ack = blocked
        for chunk in ack:
            self._enqueue_audio(chunk)
        if held:
            self._gate_dropped += held
            self.router_stats["held_dropped"] += 1
            logger.info(f"⏹️ live dropped {held} held chunks spoken before the verified result")
        return held

    def _note_heard(self) -> None:
        """Audible audio went to the device. Stamps the acknowledgement timing."""
        now = time.monotonic()
        self._last_audible_mono = now
        if self._turn_heard_mono is not None or self._speech_end_mono is None:
            return
        self._turn_heard_mono = now
        meta = self._turn_meta()
        ms = self._since_speech_end_ms(now)
        source = "result" if self._gate == "post" else ("live" if self._gate == "pre" else "reply")
        if source == "live":
            self.router_stats["live_acks"] += 1
        if meta is not None:
            meta.setdefault("ack_source", source)
            meta.setdefault("speech_end_to_heard_ms", ms)
            stopped = self._stopped_ms()
            if stopped is not None:
                meta.setdefault("stopped_to_heard_ms", stopped)
        logger.info(f"👂 first audible audio {ms} ms after speech end ({source})")

    def _note_result_heard(self) -> None:
        """The verified result became audible: log where the time went.

        result ready -> commentary sent is ours; sent -> acknowledged and
        acknowledged -> first audible audio are the provider's; the enqueue to
        the device is the line itself."""
        self._result_heard = True
        now = time.monotonic()
        op = self._op
        sent = self._commentary_sent_mono
        acked = self._commentary_acked_mono
        segments = {
            "commentary_to_ack_ms": int((acked - sent) * 1000) if sent and acked else None,
            "ack_to_audio_ms": int((now - acked) * 1000) if acked else None,
            "commentary_to_audio_ms": int((now - sent) * 1000) if sent else None,
            "speech_end_to_result_audio_ms": self._since_speech_end_ms(now),
        }
        logger.info("⏱️ live result audio " + json.dumps(
            {"op": op.op_id if op else None, **segments}, separators=(",", ":")))
        meta = self._turn_meta()
        if meta is not None:
            meta.update({k: v for k, v in segments.items() if v is not None})
            stopped = self._stopped_ms()
            if stopped is not None:
                meta["stopped_to_result_audio_ms"] = stopped

    # -- background tasks (app/live_tasks.py) ---------------------------------
    #
    # A task list, not a slot. A route that outlives its soft budget, or that
    # the user talks over once a tool has started, continues here. It is never
    # cancelled, an exact repeat joins it, the voice narrates it only when the
    # user is not talking, and its result is delivered when it lands: into an
    # open session, or (COST RULE: the session is never held open for it)
    # through a short new session opened through the spend gate, the
    # announcement lane, or a text.

    def _task_detach(self, op: RouterOp, reason: str) -> None:
        """A started operation leaves the foreground and keeps running."""
        self._withdraw_confirmations(op)  # longtask review B1 / eleventh review B9
        if not op.background:
            op.background = True
            self.router_stats["promoted"] += 1
        self._tasks[op.op_id] = op
        logger.info(f"🧺 router: {op.op_id} continues in the background ({reason}); "
                    "its result will be delivered")
        if op.watch is None or op.watch.done():
            op.watch = self._spawn(self._task_progress_watch(op), f"live-task-progress-{op.op_id}")

    def _task_find_joinable(self, key: str) -> Optional[RouterOp]:
        """A pending (or finished but undelivered) task for the same request."""
        if not key:
            return None
        now = time.monotonic()
        for op in reversed(list(self._tasks.values())):
            if op.key != key or op.delivered or op.abandoned or op.confirm_withdrawn:
                continue
            if op.session_sent_mono is not None:
                continue  # its result is being spoken right now: a repeat is a new request
            if op.done and now - (op.result_ready_mono or now) > TASK_JOIN_DONE_WINDOW_S:
                continue
            return op
        return None

    async def _task_join(self, op: RouterOp, seq: int, text: str) -> None:
        """An exact repeat of a pending request joins it: nothing runs again."""
        op.joins += 1
        self.router_stats["joined"] += 1
        meta = self._turn_meta()
        if meta is not None:
            meta["route_op"] = op.op_id
            meta["route_joined"] = True
        logger.info(f"🔁 router: the request repeats {op.op_id}, which is "
                    f"{'finished' if op.done else 'still running'}; joined, nothing runs again")
        if op.external_id and not op.done:
            # Finishing elsewhere: the voice can only say it is still on it.
            self._gate_open()
            await self._send_commentary(joined_commentary(op.text), None)
            return
        # Re-attach as the foreground operation: its result answers this
        # utterance, through the ordinary pre-result gate.
        self._tasks.pop(op.op_id, None)
        op.background = False
        op.seq = seq
        self._op = op
        self._gate_text = ""
        self._gate_blocked = False
        self._gate_ack_end_ms = None
        self._gate_timeline_seen = False
        if self._gate != "pre":
            self._gate_lag.clear()
            self._gate_held.clear()
        self._gate = "pre"
        if op.done:
            await self._router_deliver(op)
            self._finish_task(op, SESSION)
            return
        await self._cancel(self._fallback_task)
        self._fallback_task = self._spawn(self._fallback_ack_watch(op), "live-fallback-ack")
        await self._cancel(op.watch)
        op.watch = self._spawn(self._task_promote_watch(op), f"live-task-watch-{op.op_id}")

    async def _task_promote_watch(self, op: RouterOp) -> None:
        """After the soft budget a foreground route becomes a background task;
        then the task is narrated while it runs."""
        await asyncio.sleep(self.task_policy.promote_s)
        # 0.18.30: a house command is not a long task. "Turn on boost heat"
        # took 8.8 s and the user heard "Still on it, ask me anything while I
        # finish" first. Only a slow tool (the agent, a web search) promotes
        # at the soft budget; anything else gets until fast_promote_s.
        while not (op.done or op.abandoned or op.slow_tool) and \
                time.monotonic() - op.started_mono < self.task_policy.fast_promote_s:
            await asyncio.sleep(0.1)
        if op.done or op.abandoned:
            return
        if not op.background and self._op is op:
            await self._task_promote(op)
        await self._task_progress_watch(op)
        self._task_flush_soon("hold expired")

    async def _task_promote(self, op: RouterOp) -> None:
        now = time.monotonic()
        op.background = True
        op.promoted_mono = now
        self._withdraw_confirmations(op)  # longtask review B1: it leaves the foreground
        self._tasks[op.op_id] = op
        self.router_stats["promoted"] += 1
        logger.info(f"🧺 router: {op.op_id} promoted to a background task after "
                    f"{now - op.started_mono:.1f}s; the voice says so, the conversation goes on, "
                    "and the session may close")
        await self._cancel(self._progress_task)
        self._progress_task = None
        heard_before = self._last_audible_mono
        self._gate_background()
        did = next((d for d in reversed(op.delegations) if d not in op.answered_delegations), None)
        if did is not None:
            op.answered_delegations.update(op.delegations)
        if op.explain_route and not op.explain_ack_sent:
            # An explanation outliving the soft budget: the explain
            # acknowledgement replaces the generic "still working" line, so
            # the user hears one acknowledgement, not a stack of them.
            op.explain_ack_sent = True
            await self._send_commentary(explain_ack_commentary(op.speaker or self._speaker_first_name()), did)
        else:
            await self._send_commentary(promotion_commentary(op.text), did)
        self._note(self.on_backend_activity)
        # If the voice says nothing, the static clip tells the user the
        # request is still being worked on (as the old narration did).
        await asyncio.sleep(PROGRESS_SILENT_FALLBACK_S)
        await self._settle_imminent_ack()
        if op.done or op.abandoned or self._user_turn.open:
            return
        # fixack (v35 merge): only audio that reaches the device counts; held
        # or dropped speech never silences the clip. The voice speaking the
        # promotion line moves _last_audible_mono, so it counts as heard.
        if self._last_audible_mono == heard_before and not self._device_audio_active() \
                and self.on_fallback_ack is not None:
            self._filler_playing = True
            self._filler_done.clear()
            try:
                played = await self.on_fallback_ack(self.device_id)
            except Exception:
                played = False
            finally:
                self._filler_playing = False
                self._filler_done.set()
            if played:
                self.router_stats["fallback_acks"] += 1
            logger.warning(f"🔈 fallback acknowledgement {'fired' if played else 'wanted'} "
                           f"for the promotion of {op.op_id}: the voice said nothing")

    def _gate_background(self) -> None:
        """The request became a background task. A complete acknowledgement
        still in the lag plays; a held guess at the outcome is dropped; from
        here the voice converses freely while the work goes on."""
        if self._gate != "pre":
            if self._gate == "idle":
                self._gate_reset("open")
            return
        if not self._gate_blocked and acknowledgement_state(self._gate_text) != ACK:
            self._gate_held = [chunk for _, chunk, _end in self._gate_lag] + list(self._gate_held)
            self._gate_lag.clear()
            self._gate_blocked = True
        if not self._gate_blocked and self._gate_lag:
            # merge (v35): the fastchat timeline rule applies here as at
            # _gate_post: lagged audio past the acknowledgement's last
            # timestamped word is words whose transcript has not arrived yet.
            keep: Deque = deque()
            while self._gate_lag and self._gate_timeline_allows(self._gate_lag[0][2]):
                keep.append(self._gate_lag.popleft())
            if self._gate_lag:
                self._gate_held = [chunk for _, chunk, _end in self._gate_lag] + list(self._gate_held)
                self._gate_lag = keep
                ack_now = [chunk for _, chunk, _end in keep]
                held_now = len(self._gate_held)
                self._need_segment_start = True
                self._log_held_sample("dropped past the acknowledgement")
                self._gate_reset("open")
                self._gate_drop_until_ack = True
                for chunk in ack_now:
                    self._enqueue_audio(chunk)
                self._gate_dropped += held_now
                self.router_stats["held_dropped"] += 1
                logger.info(f"⏹️ live dropped {held_now} chunks past the acknowledgement "
                            "before the request went to the background")
                return
            self._gate_lag = keep
        ack = [chunk for _, chunk, _end in self._gate_lag] if not self._gate_blocked else []
        held = len(self._gate_held) + (len(self._gate_lag) if self._gate_blocked else 0)
        if held:
            self._need_segment_start = True
        blocked = self._gate_blocked
        self._log_held_sample("dropped" if held else "ack played")
        # Only a response actually in flight (a guess being spoken) is dropped
        # until the promotion commentary is acknowledged; a silent voice is
        # free to acknowledge the promotion at once.
        in_flight = bool(self._gate_text.strip()) or held > 0
        self._gate_reset("open")
        self._gate_drop_until_ack = blocked and in_flight
        for chunk in ack:
            self._enqueue_audio(chunk)
        if held:
            self._gate_dropped += held
            self.router_stats["held_dropped"] += 1
            logger.info(f"⏹️ live dropped {held} held chunks spoken before the request went to the background")

    def _task_quiet_moment(self) -> bool:
        """May the voice mention a background task right now? Only in an open
        session, while nobody is talking and nothing is being routed or
        spoken. Never opens a session for it."""
        if not self.session_ready or self._closing or self._disconnecting:
            return False
        if self._user_turn.open:
            return False
        fg = self._op
        if fg is not None and not fg.done and not fg.background:
            return False
        if self.audio.speaking or self._audio_queue.qsize() or self._gate_lag:
            return False
        last = self._last_user_turn_closed_mono
        if last is not None and time.monotonic() - last < self.task_policy.progress_quiet_s:
            return False
        return True

    async def _task_progress_watch(self, op: RouterOp) -> None:
        """"Still working on X", at most progress_max times, only at a quiet
        moment of an open conversation. A hand-off that never reports back is
        closed as failed after EXTERNAL_TASK_MAX_S."""
        policy = self.task_policy
        while (not op.deferred and not op.explain and not op.done and not op.abandoned
               and op.progress_count < policy.progress_max):
            await asyncio.sleep(policy.progress_every_s)
            deadline = time.monotonic() + policy.progress_every_s
            while (time.monotonic() < deadline and not op.done and not op.abandoned
                   and not self._task_quiet_moment()):
                await asyncio.sleep(0.5)
            if op.done or op.abandoned or not self._task_quiet_moment():
                continue
            op.progress_count += 1
            logger.info(f"⏳ task {op.op_id} still running after {time.monotonic() - op.started_mono:.0f}s; "
                        f"the voice may say so ({op.progress_count})")
            await self._send_commentary(progress_commentary(op.text), None)
        if op.external_id and not op.done:
            bound = (policy.deferred_max_s if op.deferred
                     else EXPLAIN_TASK_MAX_S if op.explain else EXTERNAL_TASK_MAX_S)
            remaining = bound - (time.monotonic() - op.started_mono)
            if remaining > 0:
                await asyncio.sleep(remaining)
            if not op.done and not op.abandoned:
                if op.deferred:
                    logger.info(f"🧺 task {op.op_id} ({op.external_id}): the watch never fired within "
                                f"{bound:.0f}s; forgotten")
                    op.abandoned = True
                    self._tasks.pop(op.op_id, None)
                    return
                logger.warning(f"⚠️ task {op.op_id} ({op.external_id}) never reported back; closing it as failed")
                await self._claim_external_result(
                    op.external_id, "The assistant did not report back in time.", False)

    def _register_external_task(self, op: RouterOp, name: str, task_id: str,
                                deferred: bool = False, explain: bool = False) -> None:
        """A tool accepted the work and will report back: wait for its result
        under the id it will arrive with. A deferred watch waits for its
        event without holding a session or being narrated; so does an
        explanation (the voice sets the expectation when it starts)."""
        if any(t.external_id == task_id for t in self._tasks.values()):
            return
        ext = RouterOp(op_id=f"{op.op_id}-ext", seq=op.seq, text=op.text, key=op.key,
                       started_mono=op.started_mono, tools_started=True, background=True,
                       external_id=task_id, tool_name=name, deferred=deferred, explain=explain,
                       text_requested=explain and wants_text(op.text),
                       speaker=op.speaker or (self._speaker_first_name() if explain else ""))
        self._tasks[ext.op_id] = ext
        ext.watch = self._spawn(self._task_progress_watch(ext), f"live-task-progress-{ext.op_id}")
        logger.info(f"🧺 router {op.op_id}: {name} continues elsewhere as "
                    f"{'a watch' if deferred else 'an explanation' if explain else 'task'} {task_id}; "
                    f"its {'event' if deferred else 'result'} is delivered when it arrives")

    def request_text_delivery(self) -> str:
        """The user asked for the pending explanation as a text (the voice
        offered one): the newest undelivered explanation will be texted
        instead of spoken. Returns its request, or "" when nothing is pending
        or no text route is configured (the ``deliver_by_text`` tool)."""
        if self._text_route() is None:
            return ""
        for op in reversed(list(self._tasks.values())):
            if op.explain and not op.delivered and not op.abandoned:
                op.text_requested = True
                logger.info(f"💬 task {op.op_id}: the user asked for the explanation by text")
                return op.text
        return ""

    def _speaker_first_name(self) -> str:
        """The identified speaker's first name for the explain acknowledgement,
        or "" (no name is better than a wrong one)."""
        probe = self.speaker_probe
        if probe is None:
            return ""
        try:
            name = probe.name_for(probe.gate_speaker())
        except Exception:
            return ""
        return str(name or "").strip().split(" ")[0] if name else ""

    async def _claim_external_result(self, task_id: str, text: str, ok: bool = True,
                                     detail: str = "") -> bool:
        """A result arrived for a hand-off (POST /task-result, or /announce
        with the id). True when a pending task here was waiting for it.
        ``detail`` is an explanation's fuller write-up, kept for a text."""
        for op in list(self._tasks.values()):
            if op.external_id and op.external_id == task_id and not op.done:
                now = time.monotonic()
                run = ToolRun(name=op.tool_name or "ask_openclaw", ok=bool(ok), kind="ok" if ok else "error")
                op.result = RouteResult(ROUTE_LOOKUP if ok else ROUTE_FAILED, (text or "").strip(),
                                        "openclaw", [run],
                                        {"external_ms": int((now - op.started_mono) * 1000)})
                op.detail = (detail or "").strip() if ok else ""
                op.done = True
                op.result_ready_mono = now
                logger.info(f"📬 task {op.op_id}: result for {task_id} arrived after "
                            f"{now - op.started_mono:.0f}s ({'ok' if ok else 'failed'})")
                await self._task_complete(op)
                return True
        return False

    async def _task_complete(self, op: RouterOp) -> None:
        """A background task finished: log it and deliver when possible. This
        deliberately does NOT touch the session's idle deadlines."""
        self.router_stats["tasks_done"] += 1
        # merge (v35), safety over the longtask design (longtask review B1):
        # a held action that finished in the background is withdrawn, never
        # asked later. Any reply drained meanwhile could have armed it, so a
        # later "yes" might answer a question the user never heard (eleventh
        # review B9). The result says it was not done; it is not joinable.
        self._withdraw_confirmations(op)
        if op.confirm_withdrawn or (op.result is not None and op.result.kind == ROUTE_CONFIRM):
            op.confirm_withdrawn = True
            op.key = ""
            if op.result is not None and op.result.kind == ROUTE_CONFIRM:
                op.result = RouteResult(ROUTE_FAILED, "That needed a spoken yes first and was not done; "
                                        "the user can ask again.", op.result.source,
                                        op.result.tools, op.result.timings)
        self._log_route(op, time.monotonic(), meta=False)
        if op.watch is not None and op.watch is not asyncio.current_task():
            await self._cancel(op.watch)
            op.watch = None
        self._task_flush_soon("task done")

    def _task_flush_soon(self, reason: str) -> None:
        if self.router is None or not any(t.done and not t.delivered for t in self._tasks.values()):
            return
        if self._task_flush_task is not None and not self._task_flush_task.done():
            # The running pass takes another look before it ends (a session
            # it asked for may have started while it was still delivering).
            self._task_flush_again = True
            return
        self._task_flush_task = self._spawn(self._task_flush(reason), "live-task-flush")

    def _session_free_for_delivery(self) -> bool:
        if self._user_turn.open:
            return False
        if (not self._user_transcript_seen_for_response and not self._task_opened_session
                and self._session_started_mono is not None
                and time.monotonic() - self._session_started_mono < TASK_WAKE_DEFER_S):
            return False  # the user woke the device to say something: that first
        fg = self._op
        if fg is not None and not fg.done and not fg.background:
            return False
        if fg is None and self._gate == "pre":
            # The user's new words are not routed yet: an earlier result must
            # not open the gate over the voice's guess at them (tenth review B8).
            return False
        if (fg is not None and fg.result_sent_mono is not None and not self._result_acked
                and time.monotonic() - fg.result_sent_mono < TASK_AFTER_RESULT_S):
            return False  # the foreground result is still going in
        if self.audio.speaking or self._audio_queue.qsize() or self._gate_lag:
            return False
        return True

    async def _task_flush(self, reason: str) -> None:
        """Deliver every finished task: into the open session once it is
        free, otherwise through the fallback chain."""
        async with self._task_flush_lock:
            retries = 0
            while True:
                ready = [t for t in list(self._tasks.values())
                         if t.done and not t.delivered and t.session_sent_mono is None]
                if not ready:
                    return
                deferred = False
                for op in ready:
                    if op.op_id not in self._tasks or op.delivered:
                        continue
                    now = time.monotonic()
                    if op.result is None or op.result.kind == ROUTE_NOTHING:
                        self._finish_task(op, "none")
                        continue
                    if (op.result.kind == ROUTE_PENDING and op.result.explain
                            and not (self.session_ready and not self._closing and not self._disconnecting)):
                        # The acceptance of an explanation is not worth a new
                        # session; its answer comes with its own delivery.
                        self._finish_task(op, "none")
                        continue
                    if (now - (op.result_ready_mono or now) > self.task_policy.result_ttl_s
                            and not op.deferred):
                        logger.warning(f"⚠️ task {op.op_id}: result undeliverable for "
                                       f"{self.task_policy.result_ttl_s:.0f}s; dropped")
                        self._finish_task(op, "dropped")
                        continue
                    if op.text_requested and await self._task_deliver_text(op):
                        continue  # the user chose a text; voice only if that failed
                    if self.session_ready and not self._closing and not self._disconnecting:
                        if not self._session_free_for_delivery():
                            deferred = True
                            continue
                        await self._task_deliver_session(op)
                        continue
                    await self._task_deliver_fallback(op)
                if self._task_flush_again:
                    self._task_flush_again = False
                    retries = 0
                    continue
                if not deferred or retries >= TASK_FLUSH_RETRIES:
                    return
                retries += 1
                await asyncio.sleep(TASK_FLUSH_RETRY_S)

    def _text_route(self):
        """The hook for a text the user asked for: ``on_task_text(text,
        recipient)`` (wired whenever the bridge is configured), else the
        fallback chain's ``on_task_message`` (a configured destination)."""
        return self.on_task_text or self.on_task_message

    async def _send_task_text(self, body: str, recipient: str = ""):
        hook = self._text_route()
        try:
            params = inspect.signature(hook).parameters
            takes_recipient = len(params) >= 2 or any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values())
        except (TypeError, ValueError):
            takes_recipient = False
        if takes_recipient:
            return await hook(body, recipient or "")
        return await hook(body)

    async def _task_deliver_text(self, op: RouterOp) -> bool:
        """The user asked for this explanation as a text: send the write-up
        through the bridge's verified iMessage sender and, if a conversation
        is open and free, say that it was sent. A failure (not configured,
        unverified) or a failed explanation falls back to the voice."""
        result = op.result
        if self._text_route() is None or result is None or result.kind != ROUTE_LOOKUP or not result.text:
            return False
        if op.spoken_to_end or op.delivered:
            return False  # already heard in the conversation: never also texted
        try:
            ok = bool(await self._send_task_text(text_message_body(op.text, result.text, op.detail), op.speaker))
        except Exception as e:
            ok = False
            logger.debug(f"task text failed: {e!r}")
        if not ok:
            op.delivery.note(IMESSAGE, False, "requested text not delivered; spoken instead")
            logger.warning(f"⚠️ task {op.op_id}: the requested text was not delivered; the voice says it")
            op.text_requested = False
            return False
        if (self.session_ready and not self._closing and not self._disconnecting
                and self._session_free_for_delivery()):
            if self._gate == "pre":
                self._gate_open()
            await self._send_commentary(text_delivered_commentary(op.text), None)
        self._finish_task(op, IMESSAGE)
        return True

    async def _task_deliver_session(self, op: RouterOp) -> None:
        result = op.result
        text = result_commentary(result.kind, result.text, op.text, result.source, deferred=op.deferred,
                                 explain=op.explain)
        if (text is None and result.kind == ROUTE_PENDING and result.explain and not result.deferred
                and not op.explain_ack_sent):
            # The explanation was accepted after the soft budget had already
            # made the voice say it was still working, and the promotion did
            # not know it was an explanation (the tool was not dispatched yet).
            # The expectation and the text offer are still owed, once.
            op.explain_ack_sent = True
            text = explain_offer_commentary()
        self._task_opened_session = False
        if text is None:
            self._finish_task(op, "none")
            return
        if self._gate == "idle":
            # A session opened for this result: nobody has spoken in it, and
            # the result must be heard.
            self._gate_reset("open")
        elif self._gate == "pre":
            self._gate_open()
        self._gate_drop_until_ack = False
        await self._send_commentary(text, None)
        op.result_sent_mono = time.monotonic()
        if result.kind in (ROUTE_LOOKUP, ROUTE_ACTION, ROUTE_REPLY, ROUTE_FAILED):
            # 0.19.2: every result spoken from the task list (an explanation,
            # a slow lookup, a late action report) is finished only once it
            # has been heard; while the voice is speaking it the task holds
            # the session (_task_holds_session, bounded), and a session that
            # closes under it hands the result back for another route.
            op.session_sent_mono = op.result_sent_mono
            self._spawn(self._task_heard_watch(op), f"live-task-heard-{op.op_id}")
            return
        self._finish_task(op, SESSION)

    async def _task_heard_watch(self, op: RouterOp) -> None:
        """A result spoken from the task list is finished once the voice has
        spoken it to the end in a session that stayed open: audible output for at least
        TASK_HEARD_MIN_S after the commentary, then quiet past the speech
        hangover. A session closing while it is still being spoken, or
        silence for TASK_HEARD_WAIT_S, hands the result back to the task
        list. While this runs the task holds the session (_task_holds_session)."""
        sent = op.session_sent_mono or time.monotonic()
        deadline = sent + TASK_HEARD_WAIT_S
        quiet_s = self.audio.speech_hangover_s + 0.5
        while True:
            await asyncio.sleep(0.2)
            if op.delivered or op.abandoned or op.op_id not in self._tasks:
                return
            now = time.monotonic()
            audible = self._last_audible_mono or 0.0
            spoken_s = audible - sent
            closed = not self.session_ready or self._closing or self._disconnecting
            if spoken_s >= TASK_HEARD_MIN_S and not closed and not self._audio_queue.qsize() \
                    and now - audible >= quiet_s:
                op.spoken_to_end = True
                self._finish_task(op, SESSION)
                return
            if closed:
                if spoken_s >= TASK_SPOKEN_HOLD_S - TASK_HEARD_WAIT_S:
                    self._finish_task(op, SESSION)  # spoken to (near) the end of the bound
                    return
                why = "the session closed before the result was heard to the end"
                break
            if spoken_s <= 0 and now > deadline:
                why = f"nothing audible {TASK_HEARD_WAIT_S:.0f}s after the result went in"
                break
            if now - sent > TASK_SPOKEN_HOLD_S:
                self._finish_task(op, SESSION)  # the hold is bounded; it was speaking all along
                return
        op.session_sent_mono = None
        op.result_ready_mono = time.monotonic()  # the TTL restarts: this result is still owed
        op.delivery.note(SESSION, False, why)
        logger.warning(f"⚠️ task {op.op_id}: {why}; delivering it again")
        self._task_flush_soon("result not heard")

    def _device_idle_s(self) -> float:
        last = max(self._last_user_delta_mono or 0.0, self._last_audible_mono or 0.0)
        return time.monotonic() - last if last else 0.0

    async def _task_deliver_fallback(self, op: RouterOp) -> None:
        """No open session: a short new one through the spend gate, the
        announcement lane, or a text, in the configured order."""
        policy = self.task_policy
        result = op.result
        spoken = spoken_result(result.kind, result.text, op.text, deferred=op.deferred, explain=op.explain)
        available = {
            REOPEN: self.on_task_reopen is not None,
            ANNOUNCE: self.on_task_announce is not None and spoken is not None,
            IMESSAGE: self.on_task_message is not None and spoken is not None,
        }
        # Quiet hours hold every result before a session is reopened or an
        # announcement plays, including a lookup and the 30-minute
        # "never reported back" failure (longtask review iii).
        quiet = in_quiet_hours(policy.quiet_hours)
        plan = policy.plan(session_ready=False, idle_s=self._device_idle_s(),
                           available=lambda channel: available.get(channel, False),
                           deferred=op.deferred, wants_text=wants_text(op.text), quiet=quiet)
        if op.explain and not op.text_requested and IMESSAGE in plan:
            # An explanation is texted only when asked for, or when it could
            # not be spoken at all (owner, 2026-10-14): a text is the last route.
            plan = [c for c in plan if c != IMESSAGE] + [IMESSAGE]
        if quiet and not plan:
            # Quiet hours: no voice in the house. The event is told when they end.
            wait = quiet_hours_end(policy.quiet_hours)
            logger.info(f"🤫 task {op.op_id}: quiet hours; the result waits {wait / 60:.0f} min")
            op.result_ready_mono = time.monotonic() + wait  # the TTL starts when it may speak
            self._spawn(self._task_wait_then_flush(wait, "quiet hours ended"), f"live-task-quiet-{op.op_id}")
            return
        now = time.monotonic()
        for channel in plan:
            if channel == REOPEN:
                if op.reopen_requested_mono is not None:
                    if now - op.reopen_requested_mono < policy.reopen_wait_s:
                        return  # the session is opening; it delivers on session.started
                    op.delivery.note(REOPEN, False, "no session arrived")
                    continue
                self._task_opened_session = True
                try:
                    opened = bool(await self.on_task_reopen())
                except Exception as e:
                    opened = False
                    logger.debug(f"task reopen failed: {e!r}")
                if not opened:
                    self._task_opened_session = False
                if opened:
                    op.reopen_requested_mono = time.monotonic()
                    logger.info(f"📬 task {op.op_id}: opening a short session to deliver the result")
                    self._spawn(self._task_reopen_watch(op), f"live-task-reopen-{op.op_id}")
                    return
                op.delivery.note(REOPEN, False, "refused")
                continue
            if channel == ANNOUNCE:
                try:
                    ok = bool(await self.on_task_announce(spoken, self.device_id))
                except Exception as e:
                    ok = False
                    logger.debug(f"task announce failed: {e!r}")
                if ok:
                    self._finish_task(op, ANNOUNCE)
                    return
                op.delivery.note(ANNOUNCE, False)
                continue
            if channel == IMESSAGE:
                try:
                    ok = bool(await self.on_task_message(spoken))
                except Exception as e:
                    ok = False
                    logger.debug(f"task message failed: {e!r}")
                if ok:
                    self._finish_task(op, IMESSAGE)
                    return
                op.delivery.note(IMESSAGE, False)
                continue
        logger.warning(f"⚠️ task {op.op_id}: no delivery route worked "
                       f"({[a.channel for a in op.delivery.attempts] or 'none configured'}); "
                       "kept for the next session")

    async def _task_wait_then_flush(self, seconds: float, reason: str) -> None:
        await asyncio.sleep(seconds)
        self._task_flush_soon(reason)

    async def _task_reopen_watch(self, op: RouterOp) -> None:
        await asyncio.sleep(self.task_policy.reopen_wait_s)
        if op.op_id in self._tasks and not op.delivered and not self.session_ready:
            logger.info(f"📬 task {op.op_id}: no session after the reopen; trying the next route")
            self._task_flush_soon("reopen timed out")

    def _finish_task(self, op: RouterOp, channel: str) -> None:
        op.delivered = True
        op.delivery.note(channel, True)
        self._tasks.pop(op.op_id, None)
        counts = self.router_stats["delivered"]
        counts[channel] = counts.get(channel, 0) + 1
        kind = op.result.kind if op.result is not None else "none"
        logger.info(f"📬 task {op.op_id} result delivered via {channel} "
                    f"{time.monotonic() - op.started_mono:.0f}s after the request ({kind})")

    async def _tasks_shutdown(self) -> None:
        """The pipeline is going away: nothing can run or deliver a task now."""
        if self.router is None:
            return
        live_tasks.unregister_sink(self._claim_external_result)
        for op in list(self._tasks.values()):
            await self._cancel(op.watch)
            op.watch = None
            if op.task is not None and not op.task.done():
                op.abandoned = True
                op.task.cancel()
                logger.warning(f"⚠️ background task {op.op_id} abandoned at shutdown; "
                               "its result could not be delivered")
        self._tasks.clear()
        await self._cancel(self._task_flush_task)
        self._task_flush_task = None

    async def _router_progress_watch(self, op: RouterOp) -> None:
        """The pre-task narration: kept for reference and tests; no longer
        armed, since promotion (_task_promote_watch) replaces it. It held the
        gate and the billed session for the whole wait."""
        delay = PROGRESS_FIRST_S
        for count in range(1, PROGRESS_MAX + 1):
            await asyncio.sleep(delay)
            delay = PROGRESS_EVERY_S
            if self._op is not op or op.done:
                return
            heard_before = self._last_audible_mono
            logger.info(f"⏳ router: {op.op_id} still running after "
                        f"{time.monotonic() - op.started_mono:.0f}s; the voice says so ({count})")
            self._note(self.on_backend_activity)
            await self._send_commentary(PROGRESS_TEXT, None)
            await asyncio.sleep(PROGRESS_SILENT_FALLBACK_S)
            await self._settle_imminent_ack()
            if self._op is not op or op.done:
                return
            if self._last_audible_mono == heard_before and not self._device_audio_active() \
                    and self.on_fallback_ack is not None:
                self._filler_playing = True
                self._filler_done.clear()
                try:
                    played = await self.on_fallback_ack(self.device_id)
                except Exception:
                    played = False
                finally:
                    self._filler_playing = False
                    self._filler_done.set()
                if played:
                    self.router_stats["fallback_acks"] += 1
                logger.warning(f"🔈 fallback acknowledgement {'fired' if played else 'wanted'} "
                               f"for progress on {op.op_id}: the voice said nothing")

    def _device_audio_active(self) -> bool:
        """Audio is actually reaching the device: queued for it, or audible
        audio pushed within the speech hangover. Speech held in the
        pre-result gate, a phrase still forming in its lag and dropped audio
        are never heard, so they never count (v33 hand-back: held speech kept
        ``audio.speaking`` true and silenced the fallback clip)."""
        if self._audio_queue.qsize():
            return True
        last = self._last_audible_mono
        return last is not None and time.monotonic() - last < self.audio.speech_hangover_s

    async def _settle_imminent_ack(self) -> None:
        """A complete acknowledgement still in the lag is due within
        ACK_LAG_S: give it that long to be heard before the clip decides."""
        if self._gate == "pre" and not self._gate_blocked and self._gate_lag \
                and acknowledgement_state(self._gate_text) == ACK:
            await asyncio.sleep(ACK_LAG_S + 0.1)

    async def _fallback_ack_watch(self, op: RouterOp) -> None:
        """Play one static acknowledgement only if nothing has been heard
        FALLBACK_ACK_AFTER_S after the end of speech and the result is not in."""
        base = self._speech_end_mono or time.monotonic()
        await asyncio.sleep(max(0.0, base + FALLBACK_ACK_AFTER_S - time.monotonic()))
        if self._op is not op or op.done or self._turn_heard_mono is not None:
            return
        await self._settle_imminent_ack()
        if self._op is not op or op.done or self._turn_heard_mono is not None:
            return
        if self._device_audio_active():
            return  # audio is on its way to the device: never on top of it
        player = self.on_fallback_ack
        meta = self._turn_meta()
        if player is None:
            return
        self._filler_playing = True
        self._filler_done.clear()
        ms = self._since_speech_end_ms()
        if self._op is op and self._gate == "pre" and not self._gate_blocked:
            # The clip is the acknowledgement. The voice's own "One moment."
            # arriving behind it would be a second one, and its tail got cut
            # at the result (Kitchen, 0.18.33): the voice's pre-result speech
            # is held like a guess from the moment the clip STARTS. Blocking
            # only once the clip's paced send ended let a result landing
            # during the clip release the voice's acknowledgement after it
            # (Workshop 2026-10-16: clip, then "One moment, Jordan.").
            self._gate_held = [chunk for _, chunk, _end in self._gate_lag] + list(self._gate_held)
            self._gate_lag.clear()
            self._gate_blocked = True
        try:
            played = await player(self.device_id)
        except Exception as e:
            played = False
            logger.debug(f"fallback acknowledgement failed: {e!r}")
        finally:
            self._filler_playing = False
            self._filler_done.set()
        if played:
            self.router_stats["fallback_acks"] += 1
            if self._turn_heard_mono is None:
                self._turn_heard_mono = time.monotonic()
            if meta is not None:
                meta.setdefault("ack_source", "fallback")
                meta.setdefault("speech_end_to_heard_ms", ms)
        logger.warning(
            f"🔈 fallback acknowledgement {'fired' if played else 'wanted but no clip in the Live voice'}: "
            f"nothing heard {ms} ms after speech end on {op.op_id} "
            f"(fired {self.router_stats['fallback_acks']} of {self.router_stats['routed']} routed turns)"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _send_continuation(self, delegation_id: Optional[str]) -> bool:
        payload = response_create()
        event_id = str(payload.get("event_id") or "")
        # ``response.create`` carries no field naming the delegation it
        # continues, so the endpoint may answer either tagged with that
        # delegation or untagged, and which it picks is not ours to choose.
        # Register every create, with the delegation only as a correlation
        # hint: whichever labelling comes back claims it exactly once. Doing
        # this only for null-delegation creates is what left a successful
        # tool call with a continuation the evidence tracker could never
        # resolve, and therefore no admissible final reply.
        if not self._evidence.request_client_response(event_id, delegation_id):
            self._unresolved_continuation = True
            return False
        self._evidence.continuation()
        self._backend_answer_ready = False
        if event_id:
            self._pending_continuation_events[event_id] = delegation_id
            while len(self._pending_continuation_events) > IN_FLIGHT_LIMIT:
                self._pending_continuation_events.pop(
                    next(iter(self._pending_continuation_events))
                )
        sent = await self._send(payload)
        if not sent:
            self._pending_continuation_events.pop(event_id, None)
        return sent

    async def _send_input_audio(self, payload: dict, nbytes: int) -> bool:
        sent = await self._send(payload)
        if sent:
            self._input_audio_ms_sent += 1000.0 * nbytes / (LIVE_SAMPLE_RATE * 2)
        return sent

    def _note(self, callback: Optional[Callable[[], None]]) -> None:
        """Fire one lifecycle observer. Never lets a watcher break the audio path."""
        if callback is None:
            return
        try:
            callback()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"live lifecycle observer failed: {e!r}")

    def _spawn(self, coro, name: str = "") -> asyncio.Task:
        """A background task owned (and cancelled on disconnect) by this service.

        Plain asyncio rather than pipecat's task manager: the manager refuses
        new tasks while the pipeline is being cancelled, which left transcript
        timers created by a late server event as never-awaited coroutines.
        """
        return asyncio.create_task(coro, name=f"{self.__class__.__name__}::{name}")

    async def _cancel(self, task: Optional[asyncio.Task]) -> None:
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
