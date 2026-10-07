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

from app.live_audio import WITHHELD_REASONS, LiveOutputAudio, OutputAudioCapture
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
# After a user turn ends: no speech, no delegation → treat as a silent
# (admission) response so the phase machine can close the turn early.
SILENT_RESPONSE_S = 4.0
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
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.base_url = base_url
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
        # Raw model-output PCM capture is OFF unless an operator asks for it:
        # the raw output IS the assistant's voice. The format metrics and the
        # bounded hash that a static report needs are always collected.
        self.audio_capture = (
            OutputAudioCapture(limit_bytes=LIVE_SAMPLE_RATE * 2 * audio_capture_ms // 1000)
            if audio_capture_ms > 0 else None
        )
        self.audio = LiveOutputAudio(capture=self.audio_capture)
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
        self._user_transcript_seen_for_response = False
        # Once any tool has started, replaying the captured user audio after a
        # disconnect could execute it twice. This deliberately survives a
        # session reset and is cleared only once reply audio is accepted.
        self._unsafe_to_replay_input = False
        self._last_user_chars = 0
        self._last_user_text = ""
        self._last_user_text_seq = 0
        self._last_user_end_ms: Optional[float] = None
        self._current_user_start_ms: Optional[float] = None
        self._last_user_start_ms: Optional[float] = None
        self._confirmation_prompts_pending: set = set()
        self._pending_result_events: Dict[str, str] = {}
        self._pending_continuation_events: Dict[str, Optional[str]] = {}
        self._unresolved_continuation = False
        self._suppressed_deltas = 0
        self._audio_overflow = 0
        self.live_seconds = 0.0
        self.backend_tokens = {"in_text": 0, "cached": 0, "out_text": 0, "reasoning": 0}

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

        self._register_event_handler("on_session_started")
        self._register_event_handler("on_delegation_created")

    # ------------------------------------------------------------------
    # pipecat lifecycle
    # ------------------------------------------------------------------

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self.disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()

    async def disconnect(self) -> None:
        """Graceful close (session.close → session.closed), then drop the socket."""
        await self._close_session()
        await self._disconnect()

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
    def response_active(self) -> bool:
        return (self.audio.speaking or self.ledger.busy or bool(self._delegations)
                or self._unresolved_continuation)

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
            self._websocket = await websocket_connect(
                uri=self.base_url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                max_size=16 * 1024 * 1024,
            )
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

    def _session_start_event(self) -> dict:
        return build_session_start(
            model=self.config.model,
            instructions=self.instructions,
            backend_model=self.config.backend_model,
            backend_instructions=self.backend_instructions,
            tools=self.tools,
            voice=self.config.voice,
            reasoning_effort=self.config.reasoning_effort,
            service_tier=self.config.service_tier,
            max_output_tokens=self.config.max_output_tokens,
            history=self._seed_history,
            sample_rate=LIVE_SAMPLE_RATE,
        )

    async def _disconnect(self) -> None:
        try:
            self._disconnecting = True
            self._session_started = False
            self._prestart_audio.clear()
            self._last_user_end_ms = None
            self._current_user_start_ms = None
            self._last_user_start_ms = None
            self._input_audio_ms_sent = 0.0
            self._last_user_text = ""
            self._last_user_text_seq = 0
            self._unresolved_continuation = False
            await self._cancel(self._silence_task)
            self._silence_task = None
            socket, self._websocket = self._websocket, None
            if socket is not None:
                try:
                    await socket.close()
                except Exception as e:  # pragma: no cover - defensive
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
                [r.call_id for r in self.calls.unanswered()], "abandoned at disconnect"
            ):
                logger.warning(f"⚠️ live function call unanswered: {record.describe()}")
            self._log_audio_summary(f"disconnect (dropped {dropped} unsent audio chunks)")
            self.ledger.reset()
            self.audio.reset()
            self.actions.reset()
            self._in_flight.clear()
            await self._cancel(self._silent_task)
            self._silent_task = None
        finally:
            self._disconnecting = False

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
            f"deltas forwarded, {report['bytes_out']}/{report['bytes_in']} bytes, "
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

    async def _close_session(self) -> None:
        if self._websocket is None or not self._session_started:
            return
        self._closing = True
        try:
            await self._send(session_close())
            await asyncio.wait_for(self._session_closed.wait(), SESSION_CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.info("live: session.closed did not arrive before the close timeout")
        except Exception as e:
            logger.debug(f"live close: {e!r}")

    async def reset_conversation(self) -> None:
        """Replace the session in place (ConnectionRecovery's reconnect).

        The Live API only takes history at session start, so the replacement
        is seeded with the recent text conversation from the shared context.
        The old session is abandoned, not closed gracefully: a graceful close
        waits for in-flight delegations whose results belong to the dead
        session. Never called from the receive task.
        """
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
        elif kind.endswith(".appended") or kind.endswith(".muted") or kind.endswith(".unmuted"):
            logger.debug(f"live: ack {kind}")
        else:
            logger.debug(f"live: unhandled event {kind}")

    async def _on_session_started(self, evt: dict) -> None:
        self._session_started = True
        self._startup_failures = 0
        session = evt.get("session") or {}
        self._session_id = session.get("id")
        self._closing = False
        logger.info(f"✅ live session started ({self._session_id})")
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
                if time.monotonic() - self._last_input_mono >= SILENCE_AFTER_MS / 1000.0:
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
        chunk = self.audio.feed(evt.get("delta"), evt.get("start_ms"), evt.get("end_ms"))
        if chunk.reason and chunk.reason not in WITHHELD_REASONS:
            logger.warning(f"⚠️ live output audio rejected a delta: {chunk.reason}")
        if not chunk.pcm:
            return
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

    async def _audio_task_handler(self) -> None:
        """Push assembled output audio downstream, strictly in order."""
        while True:
            chunk = await self._audio_queue.get()
            try:
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
                    self._arm_confirmation_prompts()
                await self.push_frame(TTSAudioRawFrame(
                    audio=chunk.pcm, sample_rate=LIVE_SAMPLE_RATE, num_channels=1
                ))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"⚠️ live output audio push failed: {e!r}")

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
        """Arm held actions on the first accepted audible post-hold chunk.

        A short acknowledgement and the later confirmation question can share
        one continuous output segment. Segment-start callbacks therefore
        cannot be the arming boundary; every device-bound chunk is checked.
        """
        if not self._confirmation_prompts_pending:
            return
        gate = getattr(self, "action_gate", None)
        timeline = self.turn_timeline
        if gate is not None and timeline is not None:
            for confirm_id in self._confirmation_prompts_pending:
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
        turn = self._user_turn if role == "user" else self._assistant_turn
        continuation = False
        if role == "user" and not turn.open:
            start_ms = evt.get("start_ms")
            if isinstance(start_ms, (int, float)) and self._last_user_end_ms is not None:
                continuation = (
                    self._last_user_end_ms <= float(start_ms)
                    <= self._last_user_end_ms + USER_TRANSCRIPT_CONTINUATION_MS
                )
        if not turn.open:
            turn.open = True
            turn.text = ""
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
            # A new utterance: lift any post-stop output suppression and start
            # the per-turn bookkeeping the Realtime path gets from server VAD.
            self._suppress_output = False
            self._stop_sent = False
            if not continuation:
                self._heard_audio_this_turn = False
                self._user_transcript_seen_for_response = True
                # A new utterance is a new instruction: the repeat/retry limits
                # must never carry over and refuse a fresh request.
                self.actions.reset()
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
            self._last_user_text = text.strip()
            self._last_user_start_ms = self._current_user_start_ms
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
            self._silent_task = self._spawn(self._silent_response_watch(), "live-silent")
        else:
            await self.push_frame(LLMFullResponseEndFrame())
            # The reply is over: write the turn's audio and function-call
            # evidence now, while it is still correlatable to what the
            # household just heard. The canary's logs were lost to a container
            # restart; a per-reply line survives that.
            self._log_audio_summary(f"reply end, {len(text.strip())} transcript chars")

    async def _broadcast(self, frame_cls) -> None:
        await self.push_frame(frame_cls(), FrameDirection.UPSTREAM)
        await self.push_frame(frame_cls(), FrameDirection.DOWNSTREAM)

    async def _silent_response_watch(self) -> None:
        await asyncio.sleep(SILENT_RESPONSE_S)
        if self._heard_audio_this_turn or self._delegations or self.ledger.busy or self._user_turn.open:
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

    # ------------------------------------------------------------------
    # delegation
    # ------------------------------------------------------------------

    async def _on_delegation_created(self, evt: dict) -> None:
        delegation = evt.get("delegation") or {}
        did = str(delegation.get("id") or "")
        target = delegation.get("target")
        logger.info(f"🧠 live delegation {did or '?'} → {target}")
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
        call, continue_now = self.ledger.observe(evt)
        if call is not None:
            await self._dispatch_function_call(call)
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
        if not self.ledger.has_open(call_id):
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
            # The captured event order is output_item.done -> response.completed
            # -> (our result), so the backend response usually finishes while
            # the tool is still running and _on_response_event cannot release
            # the delegation then. Release it here instead; otherwise the
            # thinking watchdog and `response_active` stay held for
            # DELEGATION_TIMEOUT_S after a perfectly successful action.
            self._finish_delegation(record.delegation_id if record else None)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _send_continuation(self, delegation_id: Optional[str]) -> bool:
        payload = response_create()
        event_id = str(payload.get("event_id") or "")
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
