"""Main application entry point using Pipecat."""
import os
import sys
import asyncio
import logging
from typing import Optional
import dotenv
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from app.mcp_service import HomeAssistantMCPService
from app.phase_emitter import TurnLiveness
from app.disconnect_tool import get_disconnect_tool_definition, create_disconnect_tool_handler
from app.web_search_tool import get_web_search_tool_definition, create_web_search_tool_handler
from app.audio_recording_service import AudioRecordingService
from app.session_manager import SessionManager
from app.websocket_handler import WebSocketHandler
from app.speaker_context import SpeakerProbe
from app.timers import TimerRegistry, get_timer_tool_definitions, register_timer_tools
from app.announce_http import start_announce_server
from app.openclaw_tool import (
    get_openclaw_tool_definition,
    get_recall_tool_definition,
    openclaw_url,
    register_openclaw_tool,
)
from app.voice_memory import (
    memory_instructions,
    get_memory_tool_definitions,
    register_memory_tools,
)
from app.realtime_payload import transform_gpt_transcription_language
from app.turn_admission import turn_admission_instructions
from app.enrollment import (
    EnrollmentRecorder,
    EnrollmentConductor,
    get_enrollment_tool_definition,
    create_enrollment_tool_handler,
    get_false_alarm_tool_definition,
    create_false_alarm_tool_handler,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce verbosity of noisy loggers
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("__main__").setLevel(logging.INFO)


def _resolve_choice(env_var: str, custom_env_var: str, default: str) -> str:
    """Resolve a dropdown option that supports a 'custom' escape hatch.

    The add-on UI renders these as a `list(...|custom)` dropdown plus a sibling
    free-text *_custom field. When the dropdown is set to "custom", use the
    custom field's value; otherwise use the dropdown value. Falls back to
    `default` if the resolved value is empty (e.g. "custom" picked but the custom
    field left blank).
    """
    choice = os.environ.get(env_var, default).strip()
    if choice.lower() == "custom":
        custom = os.environ.get(custom_env_var, "").strip()
        if custom:
            return custom
        logger.warning(
            f"⚠️ {env_var}=custom but {custom_env_var} is empty; falling back to {default!r}"
        )
        return default
    return choice or default

dotenv.load_dotenv()


class SafeRealtimeLLMService(OpenAIRealtimeLLMService):
    """OpenAIRealtimeLLMService with audio-truncation-on-interruption disabled.

    pipecat's `_truncate_current_audio_response()` (called by `_handle_interruption`
    on EVERY interruption — both our device "stop" AND pipecat's own server-VAD
    barge-in when the user wakes/speaks mid-reply) sends a
    `conversation.item.truncate` with `audio_end_ms = wall-clock ms since audio
    start`. But OpenAI BURSTS the reply faster than real-time, so that elapsed
    value massively overshoots the audio that actually exists, and OpenAI rejects
    it with `invalid_request_error("Audio content of N ms is already shorter than
    M ms")`. That errored truncate wedges the realtime session, so the user's very
    next turn gets NO response — the recurring "interrupt, then immediately ask
    again → silence" bug (confirmed in logs: session goes quiet right after
    `_truncate_current_audio_response`).

    The device stops playback authoritatively on its own, so server-side
    truncation buys us nothing. No-op it. (Cost: OpenAI's conversation history
    keeps the full assistant text the user may not have fully heard — purely
    cosmetic for context.)
    """

    async def _truncate_current_audio_response(self):  # type: ignore[override]
        return

    async def send_client_event(self, event):  # type: ignore[override]
        """Serialize GPT transcription models with their required `languages` field.

        Pipecat 0.0.97 only exposes the legacy singular `language` field on
        InputAudioTranscription. OpenAI rejects that field for newer GPT
        transcription models, which instead expect `languages: [<ISO code>]`.
        """
        payload = event.model_dump(exclude_none=True)
        transform_gpt_transcription_language(payload)
        await self._ws_send(payload)

    # Per-response cost accounting (fork). The API reports exact token usage in
    # every response.done; pipecat only pushes it as metrics frames. Log the
    # breakdown + estimated $ (measured 2026-07-12: warm turn ≈ $0.003-0.013,
    # cold session-first turn ≈ $0.019 — the 4.4k-token instruction+tool prefix
    # uncached) and publish a daily cost sensor to HA.
    _RATES = (  # $/1M tokens: (text_in, text_out, audio_in, audio_out, cached)
        (0.60, 2.40, 10.0, 20.0, 0.06)
        if "mini" in (os.environ.get("OPENAI_MODEL_CUSTOM") or os.environ.get("OPENAI_MODEL") or "")
        else (4.0, 24.0, 32.0, 64.0, 0.40)
    )

    async def _handle_evt_response_done(self, evt):  # type: ignore[override]
        try:
            u = evt.response.usage
            itd = getattr(u, "input_token_details", None)
            otd = getattr(u, "output_token_details", None)
            ctd = getattr(itd, "cached_tokens_details", None)
            in_text = getattr(itd, "text_tokens", 0) or 0
            in_audio = getattr(itd, "audio_tokens", 0) or 0
            cached = getattr(itd, "cached_tokens", 0) or 0
            c_text = getattr(ctd, "text_tokens", 0) or 0
            c_audio = getattr(ctd, "audio_tokens", 0) or 0
            out_text = getattr(otd, "text_tokens", 0) or 0
            out_audio = getattr(otd, "audio_tokens", 0) or 0
            ti, to, ai, ao, ca = self._RATES
            cost = (max(0, in_text - c_text) * ti + max(0, in_audio - c_audio) * ai
                    + cached * ca + out_text * to + out_audio * ao) / 1e6
            logger.info(
                f"💰 usage: in {in_text}txt+{in_audio}aud (cached {cached}) "
                f"out {out_text}txt+{out_audio}aud ≈ ${cost:.4f}"
            )
            from .ha_sensors import PUBLISHER
            asyncio.get_running_loop().create_task(PUBLISHER.usage(cost, {
                "in_text": in_text, "in_audio": in_audio, "cached": cached,
                "out_text": out_text, "out_audio": out_audio}))
        except Exception as e:
            logger.debug(f"usage accounting failed: {e!r}")
        await super()._handle_evt_response_done(evt)

    async def reset_conversation(self):  # type: ignore[override]
        """Reconnect WITHOUT forcing a response on the reconnected session.

        pipecat's reset_conversation() (used by ConnectionRecovery on a 60-min cap
        / keepalive drop) reconnects and leaves `_llm_needs_conversation_setup =
        True`. The collision: if a turn was mid-flight when the WS dropped,
        `_create_response()` had already set `_run_llm_when_api_session_ready =
        True` (because `_api_session_ready` went False on disconnect). After the
        reconnect, the `session.updated` handler sees that flag and fires
        `_create_response()` — but under semantic_vad (`create_response=true`) the
        SERVER also auto-creates a response for the user's next turn. Two
        response.create events collide → `conversation_already_has_active_response`,
        and that turn gets no answer (observed: first turn right after a reconnect
        fails, ~1 in 20 reconnects — whenever the user happens to speak in the few
        seconds just after a reconnect).

        Fix: after the normal reconnect, clear `_run_llm_when_api_session_ready` so
        the reconnected session does NOT self-create a response, and set
        `_llm_needs_conversation_setup = False` (same as the startup pre-seed) — the
        server-VAD drives every user-turn response, so we never need to create one
        ourselves on reconnect. The live context is untouched (it's restored by the
        SessionManager on the next real turn).
        """
        # A replacement Realtime conversation does not contain the function
        # calls from the old socket. Pipecat keeps their results in the shared
        # LLM context, though, and clears its own sent-result bookkeeping on
        # reconnect. Without preserving those IDs, the next tool completion
        # replays an old function_call_output into the new conversation. OpenAI
        # rejects it with invalid_tool_call_id after the requested action has
        # already succeeded, leaving the user with no spoken acknowledgement.
        old_tool_call_ids = set()
        context = getattr(self, "_context", None)
        if context is not None:
            try:
                old_tool_call_ids = {
                    message.get("tool_call_id")
                    for message in context.get_messages()
                    if isinstance(message, dict) and message.get("tool_call_id")
                }
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"⚠️ could not snapshot tool-call history before reconnect: {e!r}")

        self._resetting_conversation = True
        try:
            await super().reset_conversation()
            try:
                self._run_llm_when_api_session_ready = False
                self._llm_needs_conversation_setup = False
                self._completed_tool_calls.update(old_tool_call_ids)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"⚠️ could not clear post-reconnect response flags: {e!r}")
        finally:
            self._resetting_conversation = False

    # Error codes that must NOT kill the realtime session. pipecat 0.0.97's
    # _receive_task_handler does `_handle_evt_error(evt); return` on EVERY
    # error event — the reader task dies, the in-flight reply cuts off
    # mid-sentence and the session is deaf until the next connection death.
    # Observed live (2026-06-10): semantic_vad split one utterance into two
    # turns, the server's auto-created second response collided with the
    # first → conversation_already_has_active_response → the playing reply
    # stopped at 4.4 s and the session wedged. These codes are harmless
    # protocol races; the right move is to keep reading.
    BENIGN_ERROR_CODES = {
        # The server auto-created a response while one was still active
        # (VAD split a sentence into two turns). The active response keeps
        # streaming — nothing is broken.
        "conversation_already_has_active_response",
        # response.cancel landed after the response already finished (device
        # "stop" / the post-interrupt racing-response kill) — nothing to
        # cancel, nothing broken.
        "response_cancel_not_active",
        # input_audio_buffer.commit raced our input_audio_buffer.clear (device
        # "stop"): an empty commit is exactly the outcome we wanted.
        "input_audio_buffer_commit_empty",
    }

    async def _maybe_handle_evt_retrieve_conversation_item_error(self, evt):  # type: ignore[override]
        """Generic benign-error filter, hooked into pipecat's receive loop.

        pipecat's `_receive_task_handler` treats a True return from this
        method as "error handled — keep the receive loop alive"; every other
        error event kills the reader task (`_handle_evt_error` + `return`).
        It is the ONLY surviving path, so besides the original retrieve-item
        case (super()), we declare our benign protocol races handled here
        instead of letting them cut off live audio and wedge the session.
        """
        if await super()._maybe_handle_evt_retrieve_conversation_item_error(evt):
            return True
        code = getattr(getattr(evt, "error", None), "code", None)
        if code in self.BENIGN_ERROR_CODES:
            logger.warning(
                f"⚠️ benign realtime error ignored (session stays alive): {code}"
            )
            return True
        return False

    def register_function(self, function_name, handler, start_callback=None, *,
                          cancel_on_interruption: bool = True):  # type: ignore[override]
        """Force cancel_on_interruption=False for every tool registration.

        pipecat cancels in-flight function-call tasks on EVERY user-speech
        interruption — and semantic_vad fires one per utterance fragment, so
        merely continuing your own sentence kills the tool call your previous
        fragment started. By then the HTTP request to Home Assistant has
        usually already been SENT: the action executes, but its result never
        reaches the model, which then tells the user it failed (observed
        live: the lights turned ON while the assistant claimed they
        wouldn't). Our tools are all short-lived (HA service calls, one web
        search), so letting them finish and report the truth always beats
        killing them halfway. This single override covers every registration
        path (MCP tools via pipecat's MCPClient, web_search, disconnect).

        The handler is also wrapped to tick its connection's liveness around its run, so
        the PhaseEmitter's thinking-watchdog knows a tool is in flight and a
        slow tool (web search: 10-20 s of pipeline silence) is never mistaken
        for a dead turn. All our handlers use the single-param
        FunctionCallParams signature, so the wrapper does too (pipecat
        inspects the signature to pick the calling convention).
        """
        async def liveness_tracked(params):
            # Speaker gate (fork): tools listed in male_only_tools only execute
            # when the last voice-type verdict is "male". Enforced HERE — below
            # the model — so prompt tricks can't bypass it. Fails closed on
            # uncertain/stale/absent verdicts. This is convenience gating on a
            # voice-type heuristic, not biometric auth.
            if self.male_only_tools and function_name in self.male_only_tools:
                speaker = self.speaker_probe.gate_speaker() if self.speaker_probe else "unknown"
                if speaker != "male":
                    owner = (self.speaker_probe.male_name if self.speaker_probe else "") or "the owner"
                    logger.info(f"⛔ speaker gate blocked '{function_name}' (speaker={speaker})")
                    await params.result_callback({
                        "error": (
                            f"Not available: this capability is reserved for {owner}, "
                            f"and the current speaker's voice was not recognized as {owner}. "
                            f"Relay this politely."
                        )
                    })
                    return
            self.turn_liveness.tool_started()
            try:
                return await handler(params)
            finally:
                self.turn_liveness.tool_finished()

        super().register_function(
            function_name, liveness_tracked, start_callback, cancel_on_interruption=False
        )

    async def _receive_task_handler(self):  # type: ignore[override]
        """Surface OpenAI reader death as an ErrorFrame so recovery can act.

        pipecat's receive loop can end without producing ANY ErrorFrame: a
        silent server-side close ends the `async for` normally, and a network
        drop raises ConnectionClosed, which the task manager merely LOGS
        ("unexpected exception"). Nothing reaches ConnectionRecovery either
        way, so the session sat deaf for HOURS until the next user utterance
        hit the dead socket — losing that utterance (observed live twice).
        Wrap the loop and report its end; ConnectionRecovery treats the
        message as a reconnect trigger.
        """
        try:
            await super()._receive_task_handler()
        except asyncio.CancelledError:
            raise  # our own disconnect/reset tearing the task down — not a death
        except Exception as e:
            await self.push_error(error_msg=f"realtime receive loop died: {e!r}")
            return
        # reset_conversation() intentionally closes the old reader before
        # connecting the replacement session. That normal close is not a
        # recoverable failure and may arrive after the processor has stopped.
        if getattr(self, "_resetting_conversation", False):
            return
        # Loop ended without an exception: a clean server-side close, or the
        # fatal-error path (which already pushed its own ErrorFrame —
        # duplicates collapse in ConnectionRecovery's cooldown/guard).
        await self.push_error(error_msg="realtime receive loop ended — connection closed")


class Application:
    """Main application class using Pipecat."""
    
    def __init__(self):
        """Initialize application."""
        # NB: there is deliberately no application-wide pipeline, transport or
        # OpenAI service any more. Each connected device owns its own — see
        # WebSocketHandler.serve_connection — because sharing one of each is
        # what made a second device evict the first.
        self.websocket_handler: Optional[WebSocketHandler] = None
        self.mcp_service: Optional[HomeAssistantMCPService] = None
        self.audio_recording_service: Optional[AudioRecordingService] = None
        self.session_manager: Optional[SessionManager] = None
        self._pipeline_lock: Optional[asyncio.Lock] = None
        self.speaker_male_name = ""
        self.speaker_female_name = ""
        self.male_only_tools: set[str] = set()
        
    async def initialize(self) -> None:
        """Initialize all components."""
        # Get configuration from environment
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        websocket_port = int(os.environ.get("WEBSOCKET_PORT", "8080"))
        websocket_host = os.environ.get("WEBSOCKET_HOST", "0.0.0.0")
        
        # Get turn detection settings with defaults
        vad_threshold = float(os.environ.get("VAD_THRESHOLD", "0.5"))
        vad_prefix_padding_ms = int(os.environ.get("VAD_PREFIX_PADDING_MS", "300"))
        vad_silence_duration_ms = int(os.environ.get("VAD_SILENCE_DURATION_MS", "800"))

        # Turn detection mode. "semantic_vad" is OpenAI's recommended mode for
        # natural conversation: it detects a *semantic* end-of-utterance instead
        # of a fixed silence window, so it doesn't cut the user off on a pause
        # and is more resistant to speaker->mic echo. "server_vad" is the classic
        # silence-based detector tuned by the vad_* values above.
        turn_detection_type = os.environ.get("TURN_DETECTION_TYPE", "semantic_vad").strip().lower()
        if turn_detection_type not in ("semantic_vad", "server_vad"):
            logger.warning(f"⚠️ Unknown TURN_DETECTION_TYPE '{turn_detection_type}', falling back to semantic_vad")
            turn_detection_type = "semantic_vad"
        # semantic_vad eagerness: "low" waits longest before deciding the user is
        # done (fewest mid-sentence cut-offs). low | medium | high | auto.
        vad_eagerness = os.environ.get("VAD_EAGERNESS", "low").strip().lower()
        if vad_eagerness not in ("low", "medium", "high", "auto"):
            logger.warning(f"⚠️ Unknown VAD_EAGERNESS '{vad_eagerness}', falling back to low")
            vad_eagerness = "low"
        # Whether detected user speech may interrupt the assistant's reply
        # (handsfree barge-in). With imperfect device-side AEC, set this false so
        # speaker echo can't cut replies short; interrupt then only via the
        # device "stop" wake word / center button.
        interrupt_response = os.environ.get("INTERRUPT_RESPONSE", "false").strip().lower() == "true"
        # Who creates the OpenAI response each user turn (semantic_vad only).
        # TRUE (default) = the server creates a response on every detected
        # end-of-turn. This is REQUIRED for multi-turn: Pipecat 0.0.97's realtime
        # service only auto-creates a response for the FIRST context (turn 1) and
        # after tool results; plain 2nd/3rd user turns get NO response unless the
        # server makes it. FALSE reproduces the old single-turn-only behaviour
        # (turn 1 answers, turn 2 hangs in "thinking"). See create_openai_service.
        semantic_vad_create_response = os.environ.get("SEMANTIC_VAD_CREATE_RESPONSE", "true").strip().lower() == "true"
        # Expose the `disconnect_client` tool to the model. DEFAULT FALSE: on the
        # Voice PE the device owns its own session lifecycle (wake word starts a
        # turn, the no-speech watchdog / idle phase ends it), so a model-driven
        # disconnect just tears down the persistent WebSocket mid-conversation —
        # it was seen closing the socket DURING the first reply ("conversation_ended").
        # Only enable if your device relies on the backend to hang up.
        enable_disconnect_tool = os.environ.get("ENABLE_DISCONNECT_TOOL", "false").strip().lower() == "true"
        speaker_male_name = os.environ.get("SPEAKER_MALE_NAME", "").strip()
        speaker_female_name = os.environ.get("SPEAKER_FEMALE_NAME", "").strip()
        male_only_tools = {t.strip() for t in os.environ.get("MALE_ONLY_TOOLS", "").split(",") if t.strip()}
        # Pin the input-transcription language (ISO code, e.g. "nl"). Empty = let
        # the model auto-detect. Helps stop the model drifting to another
        # language; pair it with an explicit language lock in `instructions`.
        transcription_language = os.environ.get("TRANSCRIPTION_LANGUAGE", "").strip()
        # Model that transcribes the user's speech to TEXT (the transcript shown
        # in logs + put in the context). NOTE: this is NOT what gpt-realtime-2
        # uses to understand you — the main model hears the audio natively; this
        # only affects the side-channel transcript. Default "gpt-4o-transcribe".
        # Alternatives include "gpt-live-transcribe" (optimized for low-latency
        # live transcription) and "gpt-transcribe" (optimized for completed
        # audio). If the API rejects a value, transcription silently falls back;
        # check the logs.
        transcription_model = _resolve_choice(
            "TRANSCRIPTION_MODEL", "TRANSCRIPTION_MODEL_CUSTOM", "gpt-4o-transcribe"
        )

        # Get instructions with default
        instructions = os.environ.get("INSTRUCTIONS", "You are the Home Assistant Voice Agent and can control the Smart Home.")

        # OpenAI Realtime model + voice. These are dropdowns in the add-on UI with
        # a "custom" sentinel + a sibling *_CUSTOM free-text field; _resolve_choice
        # returns the custom value when the dropdown is "custom", else the dropdown.
        openai_model = _resolve_choice("OPENAI_MODEL", "OPENAI_MODEL_CUSTOM", "gpt-realtime-2")
        openai_voice = _resolve_choice("OPENAI_VOICE", "OPENAI_VOICE_CUSTOM", "marin")

        # Playback speed (post-generation rate): 0.25-1.5, 1.0 = normal. Clamped.
        try:
            openai_speed = float(os.environ.get("OPENAI_SPEED", "1.0"))
        except (TypeError, ValueError):
            openai_speed = 1.0
        openai_speed = max(0.25, min(1.5, openai_speed))
        # Max reply length in output tokens. 0 = unlimited (API default). Caps a
        # runaway monologue + bounds per-response output-token cost.
        try:
            max_output_tokens = int(os.environ.get("MAX_OUTPUT_TOKENS", "0"))
        except (TypeError, ValueError):
            max_output_tokens = 0
        # Pass None when 0/unset so SessionProperties omits it (API default "inf").
        max_output_tokens = max_output_tokens if max_output_tokens > 0 else None
        # Input noise reduction: "near_field" | "far_field" | "" (off). Anything
        # else is treated as off so a typo can't reach the API.
        noise_reduction = os.environ.get("NOISE_REDUCTION", "").strip().lower()
        if noise_reduction not in ("near_field", "far_field"):
            noise_reduction = ""

        # Optional allow-list to trim the (large) ha-mcp tool set exposed to the
        # model. Comma-separated tool names; empty means expose all.
        mcp_tool_allowlist = [t.strip() for t in os.environ.get("MCP_TOOL_ALLOWLIST", "").split(",") if t.strip()]
        
        # Web search: let the assistant look things up online (weather, news,
        # facts). ON by default; existing installs keep their saved option, so an
        # Update won't silently flip it. When on, a `web_search` function tool
        # calls OpenAI's Responses web_search built-in tool server-side (using
        # OPENAI_API_KEY) and returns a short spoken answer. The model is
        # configurable so a different price/quality — or a renamed model — needs
        # no code change.
        enable_web_search = os.environ.get("ENABLE_WEB_SEARCH", "true").lower() == "true"
        web_search_model = _resolve_choice(
            "WEB_SEARCH_MODEL", "WEB_SEARCH_MODEL_CUSTOM", "gpt-5.5"
        )

        # Get recording setting (optional, defaults to false)
        enable_recording = os.environ.get("ENABLE_RECORDING", "false").lower() == "true"
        
        # Post-reply follow-up window: how many seconds the device keeps the mic
        # open after the assistant finishes so the user can answer back without
        # re-saying the wake word. Sent to the device in the `hello` handshake as
        # follow_up_ms; the device opens the mic (after its TTS tail drains) and
        # shows the listening LED for that long. 0 disables (turn-based).
        try:
            follow_up_listen_seconds = int(os.environ.get("FOLLOW_UP_LISTEN_SECONDS", "8"))
        except (TypeError, ValueError):
            follow_up_listen_seconds = 8
        follow_up_listen_seconds = max(0, min(60, follow_up_listen_seconds))
        follow_up_ms = follow_up_listen_seconds * 1000
        # Delay (ms) before the follow-up mic opens, bridging the device speaker's
        # hardware tail so the mic doesn't catch the reply's own end. Sent to the
        # device in `hello`; lower = snappier, higher = safer against echo.
        try:
            follow_up_open_delay_ms = int(os.environ.get("FOLLOW_UP_OPEN_DELAY_MS", "700"))
        except (TypeError, ValueError):
            follow_up_open_delay_ms = 700
        follow_up_open_delay_ms = max(0, min(5000, follow_up_open_delay_ms))
        # Same idea at the WAKE boundary: delay (ms) after the wake chime before
        # the mic opens, so the chime's own hardware tail doesn't leak into the
        # fresh mic and become a ghost turn (the wake-path twin of
        # follow_up_open_delay_ms — the yaml wake handler reads it via a lambda).
        try:
            wake_open_delay_ms = int(os.environ.get("WAKE_OPEN_DELAY_MS", "700"))
        except (TypeError, ValueError):
            wake_open_delay_ms = 700
        wake_open_delay_ms = max(0, min(5000, wake_open_delay_ms))
        # Playback jitter buffer (ms): the device holds incoming TTS until this
        # much has accumulated before playing, so a brief network hiccup doesn't
        # dry out the speaker chain mid-word (audible crackle). Sent in `hello`.
        try:
            playback_prebuffer_ms = int(os.environ.get("PLAYBACK_PREBUFFER_MS", "150"))
        except (TypeError, ValueError):
            playback_prebuffer_ms = 150
        playback_prebuffer_ms = max(0, min(2000, playback_prebuffer_ms))
        # Relay-side output lead buffer (ms): hold the first LEAD_MS of each
        # reply and burst it to prime the device against the resampler
        # cold-start (app/output_lead_buffer.py). 0 = disabled — the default,
        # opt-in until runtime-validated per install.
        try:
            output_lead_buffer_ms = int(os.environ.get("OUTPUT_LEAD_BUFFER_MS", "0"))
        except (TypeError, ValueError):
            output_lead_buffer_ms = 0
        output_lead_buffer_ms = max(0, min(2000, output_lead_buffer_ms))

        # Get session reuse timeout and initialize session manager
        session_reuse_timeout = float(os.environ.get("SESSION_REUSE_TIMEOUT_SECONDS", "300"))
        # Cap on restored conversation history (0 = unlimited). Bounds per-turn
        # tokens so a long chat doesn't trip OpenAI's TPM rate limit (gpt-realtime
        # re-bills the whole conversation on every response; pipecat has no
        # truncation). Default 12 keeps recent continuity cheaply.
        try:
            max_context_messages = int(os.environ.get("MAX_CONTEXT_MESSAGES", "12"))
        except (TypeError, ValueError):
            max_context_messages = 12
        max_context_messages = max(0, max_context_messages)
        self.session_manager = SessionManager(
            reuse_timeout=session_reuse_timeout,
            max_restored_messages=max_context_messages,
        )
        logger.info(
            f"Session reuse timeout: {session_reuse_timeout} seconds, "
            f"max restored messages: {max_context_messages or 'unlimited'}"
        )
        
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        
        # Initialize Home Assistant MCP Service
        mcp_client = None
        try:
            supervisor_token = os.environ.get("LONGLIVED_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
            ha_mcp_url = os.environ.get("HA_MCP_URL", "http://supervisor/core/api/mcp")
            if supervisor_token:
                logger.info("Loading Home Assistant MCP tools...")
                self.mcp_service = HomeAssistantMCPService(url=ha_mcp_url, access_token=supervisor_token)
                mcp_client = await self.mcp_service.initialize()
                logger.info("✅ Home Assistant MCP Client initialized")
            else:
                logger.warning("⚠️ SUPERVISOR_TOKEN not set, skipping Home Assistant MCP integration")
        except Exception as e:
            logger.warning(f"⚠️ Failed to initialize Home Assistant MCP Client: {e}")
        
        # Initialize audio recording before the handler so its pipeline can
        # grant exactly one connection ownership of the shared file recorder.
        self.audio_recording_service = AudioRecordingService(
            enable_recording=enable_recording,
            sample_rate=24000,
            chunk_duration_seconds=30,
            output_dir="recordings"
        )

        # Initialize WebSocket handler
        self.websocket_handler = WebSocketHandler(
            host=websocket_host,
            port=websocket_port,
            session_manager=self.session_manager,
            audio_recording_service=self.audio_recording_service,
            follow_up_ms=follow_up_ms,
            follow_up_open_delay_ms=follow_up_open_delay_ms,
            wake_open_delay_ms=wake_open_delay_ms,
            playback_prebuffer_ms=playback_prebuffer_ms,
            output_lead_buffer_ms=output_lead_buffer_ms,
        )
        logger.info(
            f"🔁 Follow-up window: {follow_up_listen_seconds}s "
            f"({'enabled' if follow_up_ms > 0 else 'disabled — turn-based'}), "
            f"mic-open delay {follow_up_open_delay_ms}ms, "
            f"wake-open delay {wake_open_delay_ms}ms, "
            f"playback prebuffer {playback_prebuffer_ms}ms, "
            f"output lead buffer {output_lead_buffer_ms}ms"
        )
        # Speaker probes are created per connection when a session starts.
        self.speaker_male_name = speaker_male_name
        self.speaker_female_name = speaker_female_name
        self.male_only_tools = male_only_tools
        if speaker_male_name or speaker_female_name:
            logger.info(
                f"🗣️ Speaker context enabled: male={speaker_male_name or '-'} "
                f"female={speaker_female_name or '-'}"
                f"{f', male-only tools: {sorted(male_only_tools)}' if male_only_tools else ''}"
            )
        elif male_only_tools:
            logger.warning("⚠️ male_only_tools set but no speaker names configured — gate inactive")

        # Surface enrolled voice prints in HA from boot (not just after builds).
        try:
            from .ha_sensors import PUBLISHER as _PUB
            asyncio.get_running_loop().create_task(_PUB.voice_prints())
        except Exception:
            pass

        # Voice timers: backend-owned registry, device rings via TIMER_RING_ENTITY.
        self.timer_registry = TimerRegistry()

        # Voice enrollment (fork): guided on-device voice capture, always available.
        self.enrollment_recorder = EnrollmentRecorder()
        self.websocket_handler.enrollment_recorder = self.enrollment_recorder
        self.enrollment_conductor = EnrollmentConductor(
            self.enrollment_recorder,
            self.websocket_handler.send_json_to,
            self.websocket_handler.send_bytes_to,
            openai_api_key,
            phrase=os.environ.get("ENROLLMENT_PHRASE", "").strip(),
            tts_voice=os.environ.get("ENROLLMENT_TTS_VOICE", "fable").strip() or "fable",
        )
        self.websocket_handler.enrollment_conductor = self.enrollment_conductor

        # Auto-build the voice print when enrollment finishes (fork, 0.16.5):
        # recording alone used to require a manual `python3 -m app.build_voiceprint`
        # step that most users never found — enrollments silently did nothing.
        # Now: build in a worker thread, tell the user out loud, and warn when
        # the enrolled name isn't in the speaker_*_name options (recognition
        # stays inactive until it is).
        async def _auto_build_voiceprint(info):
            person, path = (info.get("person") or "").strip(), info.get("path")
            if not person or not path or (info.get("seconds") or 0) < 20:
                return
            from .build_voiceprint import build
            from .ha_sensors import PUBLISHER
            try:
                result = await asyncio.to_thread(build, person, [path])
            except Exception as e:
                logger.warning(f"⚠️ voice-print auto-build failed: {e!r}")
                return
            if not result["ok"]:
                logger.warning(f"⚠️ voice-print for '{person}': {result['error']}")
                await self.enrollment_conductor._say(
                    "I couldn't build a reliable voice print from that session — "
                    "there wasn't enough clear speech. Say 'train my voice' to try again.")
                return
            logger.info(f"🪪 voice print built for '{person}' ({result['chunks']} chunks) → {result['path']}")
            known = {n.strip().lower() for n in (
                os.environ.get("SPEAKER_MALE_NAME", ""), os.environ.get("SPEAKER_FEMALE_NAME", "")) if n.strip()}
            if person.lower() in known:
                await self.enrollment_conductor._say(
                    f"Your voice print is ready, {person.capitalize()}. I'll recognize you from now on.")
            else:
                logger.warning(
                    f"⚠️ '{person}' is enrolled but not in speaker_male_name/speaker_female_name — "
                    "recognition inactive until the add-on configuration names this person")
                await self.enrollment_conductor._say(
                    f"Your voice print is built, {person.capitalize()} — one more step: add your name "
                    "to the speaker settings in the add-on configuration, then restart it.")
            await PUBLISHER.voice_prints()
        self.enrollment_conductor.on_finished = _auto_build_voiceprint
        # Timers: personalized spoken expiry via the conductor's TTS lane,
        # owner from the live speaker verdict, wake-ack from the serializer.
        async def _guarded_say(text, device_id=None):
            # Speak on ONE device. With several connected, "the device" is
            # whichever was named, else the one most recently spoken to.
            # Suppress that device's inbound mic while the announcement plays
            # (+ tail) so the assistant can't hear itself and reply.
            ser = self.websocket_handler.serializer_for(device_id)
            import time as _t
            if ser is not None:
                ser.suppress_inbound_until = _t.monotonic() + 3600
            try:
                return await self.enrollment_conductor._say(text, device_id=device_id)
            finally:
                if ser is not None:
                    ser.suppress_inbound_until = _t.monotonic() + 1.2
        self.timer_registry.announcer = _guarded_say
        self.timer_registry.get_owner = lambda device_id: self._speaker_name(
            self.websocket_handler.resolve_device(device_id)
        )
        def _last_wake(device_id: str) -> float:
            connection = self.websocket_handler.resolve_device(device_id)
            ser = connection.serializer if connection else None
            if ser is None:
                return 0.0
            return max(
                getattr(ser, "_last_wake_mono", 0.0),
                getattr(ser, "_last_button_mono", 0.0),
            )

        self.timer_registry.last_wake = _last_wake
        self.timer_registry.allow_legacy_ring = lambda device_id: (
            len(self.websocket_handler.devices) == 1
            and self.websocket_handler.resolve_device(device_id) is not None
        )

        # Announce endpoint (fork): a LAN route back to the device so the
        # household's agent can speak results of long-running work. Reuses the
        # guarded announcer above; off unless both port and token are set.
        announce_port = int(os.environ.get("ANNOUNCE_PORT", "0") or 0)
        announce_token = os.environ.get("ANNOUNCE_TOKEN", "").strip()
        if announce_port and announce_token:
            await start_announce_server(
                announce_port, announce_token, _guarded_say,
                lambda device_id: self.websocket_handler.resolve_device(device_id) is not None,
            )
        elif announce_port or announce_token:
            logger.warning("⚠️ announce endpoint needs BOTH announce_port and announce_token — disabled")

        # Store configuration for session creation
        self.openai_api_key = openai_api_key
        self.vad_threshold = vad_threshold
        self.vad_prefix_padding_ms = vad_prefix_padding_ms
        self.vad_silence_duration_ms = vad_silence_duration_ms
        self.turn_detection_type = turn_detection_type
        self.vad_eagerness = vad_eagerness
        self.interrupt_response = interrupt_response
        self.semantic_vad_create_response = semantic_vad_create_response
        self.enable_disconnect_tool = enable_disconnect_tool
        self.transcription_language = transcription_language
        self.transcription_model = transcription_model
        self.instructions = instructions
        self.model = openai_model
        self.voice = openai_voice
        self.openai_speed = openai_speed
        self.max_output_tokens = max_output_tokens
        self.noise_reduction = noise_reduction
        self.mcp_tool_allowlist = mcp_tool_allowlist
        self.mcp_client = mcp_client
        self.enable_web_search = enable_web_search
        self.web_search_model = web_search_model

        logger.info("✅ Application initialized - ready to accept WebSocket connections")
    
    def _update_session_activity(self):
        """Update session activity timestamp (called by SessionActivityTracker)."""
        pass

    def _speaker_name(self, connection) -> Optional[str]:
        """Return the current speaker name for one connection."""
        probe = getattr(connection, "speaker_probe", None)
        return probe.name_for(probe.gate_speaker()) if probe else None
    
    async def create_openai_service(self, connection):
        """Create an OpenAI Realtime session for ONE device.

        This used to assign the single `self.openai_service`, so a second
        device connecting replaced the first device's live session and wiped
        its conversation. It now returns a fresh service that belongs to the
        calling connection and to nothing else.

        Args:
            connection: The DeviceConnection the session will serve. Its
                transport is needed so device-scoped tools act on that device.

        Returns:
            A newly created SafeRealtimeLLMService.
        """
        client_id = connection.device_id
        if self._pipeline_lock is None:
            self._pipeline_lock = asyncio.Lock()

        async with self._pipeline_lock:
            if client_id is None:
                logger.warning("⚠️ No client_id provided to create_openai_service")

            # Create new session
            if client_id:
                logger.info(f"🆕 Creating new OpenAI Session for Client {client_id}...")
            else:
                logger.info("🆕 Creating new OpenAI Session...")

            # Cache context from this DEVICE's previous session (if it is
            # reconnecting) so the conversation survives the reconnect.
            if client_id and self.session_manager.get_current_service(client_id) is not None:
                try:
                    self.session_manager.cleanup_before_new_session(client_id)
                    logger.debug(f"Cached context from previous session for client {client_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Error caching context from old service for client {client_id}: {e}")
            
            # Create session properties with audio configuration
            from pipecat.services.openai.realtime.events import (
                SessionProperties,
                AudioConfiguration,
                AudioInput,
                AudioOutput,
                TurnDetection,
                SemanticTurnDetection,
                InputAudioTranscription,
                InputAudioNoiseReduction,
            )
            
            # Collect all tool definitions for session properties. The
            # disconnect_client tool is opt-in (see enable_disconnect_tool): by
            # default we do NOT expose it, so the model can't hang up the device
            # mid-conversation.
            all_tools = []
            if self.enable_disconnect_tool:
                all_tools.append(get_disconnect_tool_definition())

            # Web search tool (optional). Lets the model look things up online via
            # a secondary OpenAI Responses web_search call in the handler.
            if self.enable_web_search:
                all_tools.append(get_web_search_tool_definition())

            # Voice enrollment tool (fork): guided voice-training capture.
            all_tools.append(get_enrollment_tool_definition())
            all_tools.append(get_false_alarm_tool_definition())
            all_tools.extend(get_timer_tool_definitions())
            all_tools.extend(get_memory_tool_definitions())
            # Direct OpenClaw escalation (fork): with OPENCLAW_URL set the tool
            # is native (no HA-MCP 60s cap); the same-named MCP tool is skipped
            # below so the model sees exactly one ask_openclaw.
            if openclaw_url():
                all_tools.append(get_openclaw_tool_definition())
                all_tools.append(get_recall_tool_definition())

            # Get MCP tool definitions if available
            mcp_tools_schema = None
            if self.mcp_client:
                try:
                    logger.info("🔧 Fetching MCP tool definitions...")
                    mcp_tools_schema = await self.mcp_client.get_tools_schema()
                    
                    # Convert MCP tool schemas to OpenAI format, applying the
                    # optional allow-list so the realtime session isn't flooded
                    # with ha-mcp's 80+ tools.
                    exposed = 0
                    for function_schema in mcp_tools_schema.standard_tools:
                        if self.mcp_tool_allowlist and function_schema.name not in self.mcp_tool_allowlist:
                            continue
                        if openclaw_url() and function_schema.name == "ask_openclaw":
                            continue
                        openai_tool = {
                            "type": "function",
                            "name": function_schema.name,
                            "description": function_schema.description,
                            "parameters": {
                                "type": "object",
                                "properties": function_schema.properties,
                                "required": function_schema.required
                            }
                        }
                        all_tools.append(openai_tool)
                        exposed += 1

                    if self.mcp_tool_allowlist:
                        logger.info(f"✅ Fetched {len(mcp_tools_schema.standard_tools)} MCP tools, exposing {exposed} per allow-list")
                    else:
                        logger.info(f"✅ Fetched {len(mcp_tools_schema.standard_tools)} MCP tools")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to fetch MCP tool definitions: {e}")
            
            # Turn detection: semantic_vad (recommended — semantic end-of-turn,
            # echo-resistant, doesn't cut the user off) or classic server_vad.
            if self.turn_detection_type == "semantic_vad":
                turn_detection = SemanticTurnDetection(
                    eagerness=self.vad_eagerness,
                    # create_response=True (default): the SERVER creates a
                    # response on every detected end-of-turn. This is required for
                    # multi-turn conversation. Pipecat 0.0.97's
                    # OpenAIRealtimeLLMService._handle_context only auto-creates a
                    # response for the FIRST context (turn 1) and after tool
                    # results (its else-branch just updates the context); a plain
                    # 2nd/3rd user turn therefore gets NO response unless the
                    # server makes it. We previously set this False to stop a
                    # turn-1 double-response (server + Pipecat first-context both
                    # creating → `conversation_already_has_active_response`), but
                    # that silently broke every turn after the first (device hung
                    # in "thinking"). True is the correct trade: the server drives
                    # all user-turn responses; Pipecat still creates the post-tool
                    # response via _process_completed_function_calls. To stop the
                    # turn-1 double (server + Pipecat-first-context both creating →
                    # conversation_already_has_active_response), run() seeds
                    # self._context once at startup with a kickoff LLMRunFrame, so
                    # the user's first real turn hits the else-branch too.
                    create_response=self.semantic_vad_create_response,
                    interrupt_response=self.interrupt_response,
                )
            else:
                turn_detection = TurnDetection(
                    type="server_vad",
                    threshold=self.vad_threshold,
                    prefix_padding_ms=self.vad_prefix_padding_ms,
                    silence_duration_ms=self.vad_silence_duration_ms,
                )

            # Optionally pin the input-transcription language to stop the model
            # drifting between languages (e.g. "nl"). Empty -> auto-detect.
            # transcription_model picks the STT used for the transcript text.
            transcription = (
                InputAudioTranscription(
                    model=self.transcription_model,
                    language=self.transcription_language,
                )
                if self.transcription_language
                else None
            )

            # Optional near/far-field input noise reduction (helps the VAD reject
            # background noise / residual speaker leak). None = off (default).
            noise_reduction = (
                InputAudioNoiseReduction(type=self.noise_reduction)
                if self.noise_reduction
                else None
            )

            session_properties = SessionProperties(
                # Voice-instructed memory: standing household notes are folded
                # into the instructions at every session creation.
                instructions=(
                    self.instructions
                    + memory_instructions()
                    + turn_admission_instructions()
                ),
                # Cap the reply length: bounds runaway monologues + per-response
                # output-token cost. None = unlimited (the API default "inf").
                max_output_tokens=self.max_output_tokens,
                audio=AudioConfiguration(
                    input=AudioInput(
                        turn_detection=turn_detection,
                        transcription=transcription,
                        noise_reduction=noise_reduction,
                    ),
                    # speed is a post-generation playback rate (0.25-1.5, 1.0 = normal).
                    output=AudioOutput(voice=self.voice, speed=self.openai_speed)
                ),
                tools=all_tools
            )

            if self.turn_detection_type == "semantic_vad":
                logger.info(
                    f"🎚️ Turn detection: semantic_vad (eagerness={self.vad_eagerness}, "
                    f"create_response={self.semantic_vad_create_response}, "
                    f"interrupt_response={self.interrupt_response})"
                    + (f", transcription={self.transcription_model} (lang={self.transcription_language})" if self.transcription_language else " (transcription off)")
                )
            else:
                logger.info(
                    f"🎚️ Turn detection: server_vad (threshold={self.vad_threshold}, "
                    f"silence_duration_ms={self.vad_silence_duration_ms})"
                    + (f", transcription={self.transcription_model} (lang={self.transcription_language})" if self.transcription_language else " (transcription off)")
                )

            logger.info(f"🔧 Creating session with {len(all_tools)} tools: {[tool.get('name', 'unknown') for tool in all_tools]}")
            
            # Create new service instance
            service = SafeRealtimeLLMService(
                api_key=self.openai_api_key,
                model=self.model,
                session_properties=session_properties,
                start_audio_paused=False
            )
            service.speaker_probe = None
            service.male_only_tools = set()
            connection.turn_liveness = TurnLiveness()
            service.turn_liveness = connection.turn_liveness
            if self.speaker_male_name or self.speaker_female_name:
                connection.speaker_probe = SpeakerProbe(
                    self.speaker_male_name, self.speaker_female_name
                )
                service.speaker_probe = connection.speaker_probe
                service.male_only_tools = self.male_only_tools
            logger.info(f"✅ OpenAI Service created: {type(service).__name__}")
            
            # Register disconnect tool handler (only when the tool is exposed)
            if self.enable_disconnect_tool:
                disconnect_tool_handler = create_disconnect_tool_handler(connection.transport)
                service.register_function("disconnect_client", disconnect_tool_handler)
                logger.info("✅ Registered disconnect tool handler")

            # Register web search tool handler (only when the tool is exposed)
            if self.enable_web_search:
                service.register_function(
                    "web_search",
                    create_web_search_tool_handler(self.openai_api_key, self.web_search_model),
                )
                logger.info(f"✅ Registered web_search tool handler (model={self.web_search_model})")
            
            # Register voice enrollment tool handler (fork). The speaker-name
            # getter lets the tool default to the voice-identified person.
            def _current_speaker_name():
                return self._speaker_name(connection)

            service.register_function(
                "voice_enrollment", create_enrollment_tool_handler(
                    self.enrollment_conductor, connection.device_id, _current_speaker_name
                ),
            )
            logger.info("✅ Registered voice_enrollment tool handler")
            service.register_function(
                "mark_false_wake", create_false_alarm_tool_handler()
            )
            register_timer_tools(service, self.timer_registry, connection.device_id)
            register_memory_tools(service, _current_speaker_name)
            if openclaw_url():
                register_openclaw_tool(service)
                logger.info("✅ Registered DIRECT ask_openclaw tool (bypassing HA MCP 60s cap)")
            logger.info("✅ Registered timer + memory tools")

            # Register MCP tool handlers if available
            if self.mcp_client and mcp_tools_schema:
                try:
                    await self.mcp_client.register_tools_schema(mcp_tools_schema, service)
                    logger.info(f"✅ Registered {len(mcp_tools_schema.standard_tools)} MCP tool handlers")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to register MCP tool handlers: {e}")
            # MUST come AFTER register_tools_schema: pipecat registers a handler
            # for EVERY MCP tool (our allow-list/dedup only trims the definitions
            # sent to the model, not handler registration), so a same-named
            # ask_openclaw script silently rebinds the tool back onto the HA MCP
            # path and its 60s cap. Observed live 2026-07-13: "It failed. I
            # couldn't send the text" at exactly 60s — while the text sent fine.
            if openclaw_url():
                register_openclaw_tool(service)
                logger.info("✅ DIRECT ask_openclaw re-registered after MCP handlers (wins)")
            
            # Register service with session manager
            if client_id:
                self.session_manager.set_current_service(client_id, service)

            self._preseed_context(service)

            logger.info("✅ New OpenAI Session created")
            return service

    def _preseed_context(self, service) -> None:
        """Stop pipecat speaking spontaneously on a brand-new session.

        pipecat 0.0.97's `_handle_context` does `if not self._context: ...
        await self._create_response()` — the very first context it sees
        triggers a real, audible reply. With semantic_vad the SERVER also
        creates a response per user turn, so the first real turn would
        double-create → `conversation_already_has_active_response`.

        Pre-setting an empty context sends the first real turn down the else
        branch instead, so there is no double and no startup speech. This runs
        per SESSION rather than once at startup: with a session per device,
        every new connection has its own service that would otherwise greet
        the room unprompted on connect.

        Args:
            service: The freshly created service.
        """
        if not (self.turn_detection_type == "semantic_vad" and self.semantic_vad_create_response):
            return
        try:
            from pipecat.processors.aggregators.llm_context import LLMContext
            if getattr(service, "_context", None) is not None:
                return
            service._context = LLMContext()
            # pipecat re-sends the context's messages as ConversationItemCreate
            # events on the first _create_response. On a fresh realtime session
            # OpenAI already builds the conversation from the live audio + tool
            # flow, so that re-injects items it has — which made the first
            # post-tool reply come out as meaningless filler. Instructions are
            # sent separately via _update_settings() on session.created, so
            # clearing this is safe.
            if hasattr(service, "_llm_needs_conversation_setup"):
                service._llm_needs_conversation_setup = False
            logger.info("🌱 Pre-seeded empty context (no spontaneous greeting on connect)")
        except Exception as e:
            logger.warning(f"⚠️ Could not pre-seed context (turn-1 double may occur): {e}")
    
    def build_web_app(self):
        """Build the FastAPI app that accepts device connections.

        The listening socket used to belong to a single pipecat
        WebsocketServerTransport, which closes the incumbent connection every
        time a new one arrives. Owning the socket here means each accepted
        connection can get its own transport, session and pipeline, so devices
        no longer evict one another.

        Returns:
            The configured FastAPI application.
        """
        from fastapi import FastAPI, WebSocket

        web_app = FastAPI(title="Voice PE Realtime backend")

        def _on_client_disconnected(connection):
            if self.session_manager:
                self.session_manager.handle_client_disconnect(
                    connection.device_id, connection.openai_service
                )

        @web_app.websocket("/{path:path}")
        async def device_endpoint(websocket: WebSocket, path: str):
            # The previous transport accepted every path. Keep that compatibility
            # for firmware configured with a non-root WebSocket URL.
            await self.websocket_handler.serve_connection(
                websocket,
                on_client_disconnected=_on_client_disconnected,
                activity_callback=self._update_session_activity,
            )

        @web_app.get("/healthz")
        async def healthz():
            """Liveness plus the currently connected device ids."""
            return {"status": "ok", "devices": self.websocket_handler.devices.ids()}

        return web_app

    async def run(self) -> None:
        """Run the application."""
        import uvicorn

        await self.initialize()

        # No pipeline is built here any more. There is no process-wide session
        # to build one around: each device brings its own when it connects.
        self.websocket_handler.openai_service_factory = self.create_openai_service

        config = uvicorn.Config(
            self.build_web_app(),
            host=self.websocket_handler.host,
            port=self.websocket_handler.port,
            log_level="warning",
            # The add-on installs its own signal handling; uvicorn's would
            # fight with it and with the per-connection pipeline runners.
            lifespan="off",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None

        logger.info(
            f"✅ Listening for devices on ws://{self.websocket_handler.host}:"
            f"{self.websocket_handler.port}/ (multi-device)"
        )
        try:
            await server.serve()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up application...")

        if self.websocket_handler:
            try:
                await self.websocket_handler.cleanup()
            except Exception as e:
                logger.warning(f"⚠️ Error cleaning up WebSocket handler: {e}")

        if self.audio_recording_service:
            self.audio_recording_service.cleanup()

        logger.info("✅ Application cleanup complete")


async def main() -> None:
    """Main entry point."""
    app = Application()

    try:
        await app.run()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
