"""Main application entry point using Pipecat."""
import os
import sys
import asyncio
import logging
import time
from typing import Optional
import dotenv
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from app import ha_api
from app.mcp_service import HomeAssistantMCPService
from app.phase_emitter import TurnLiveness
from app.disconnect_tool import get_disconnect_tool_definition, create_disconnect_tool_handler
from app.follow_up_tool import (
    get_follow_up_tool_definition,
    create_follow_up_tool_handler,
)
from app.web_search_tool import get_web_search_tool_definition, create_web_search_tool_handler
from app.search_home_tool import get_search_home_tool_definition, create_search_home_tool_handler
from app.play_media_tool import get_play_media_tool_definition, create_play_media_tool_handler
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


def build_router():
    """Build the engine router from the add-on options.

    Returns:
        A ProviderRouter. A backup equal to the primary, or the literal
        "none", means no failover -- the same as not configuring one.
    """
    from app.provider_router import ProviderRouter
    from app.providers import OPENAI, PROVIDERS

    primary = (os.environ.get("VOICE_PROVIDER") or OPENAI).strip().lower()
    if primary not in PROVIDERS:
        logger.warning(f"⚠️ unknown voice_provider {primary!r}, using {OPENAI}")
        primary = OPENAI

    backup = (os.environ.get("VOICE_PROVIDER_BACKUP") or "none").strip().lower()
    if backup in ("none", "", primary) or backup not in PROVIDERS:
        backup = None

    try:
        minutes = float(os.environ.get("PROVIDER_COOLDOWN_MINUTES") or 30)
    except ValueError:
        minutes = 30.0

    logger.info(
        f"🔀 voice engine: {primary}"
        + (f", backup {backup} (cooldown {minutes:.0f} min)" if backup else ", no backup")
    )
    return ProviderRouter(primary, backup, cooldown_s=minutes * 60.0)


dotenv.load_dotenv()


def _env_seconds(name: str, default: float) -> float:
    """A float env knob in seconds, falling back to `default` when unparsable."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _device_idle(connection, quiet_s: float) -> bool:
    """Nothing is happening on this device, and nothing has for `quiet_s` s.

    Busy: a turn in progress (phase), an announcement playing (it has no
    phase), or less than `quiet_s` since the last wake, turn end or
    announcement end. The phase stays "idle" between a wake and the first
    speech, and the device holds its follow-up mic open after a turn or an
    announcement with no wake at all, so the caller folds the follow-up
    window into `quiet_s` (raawr D-72).
    """
    if connection.says_playing:
        return False
    emitter = connection.phase_emitter
    phase = getattr(emitter, "phase", None) if emitter is not None else None
    if phase not in (None, "idle"):
        return False
    last = max(
        connection.last_active,
        connection.last_busy,
        getattr(emitter, "idle_since", 0.0),
    )
    return time.monotonic() - last >= quiet_s


class Application:
    """Main application class using Pipecat."""

    # Local turn end on Gemini; GEMINI_TURN_SILENCE_MS overrides it at start.
    gemini_turn_silence_ms = 1200

    def __init__(self):
        """Initialize application."""
        # NB: there is deliberately no application-wide pipeline, transport or
        # OpenAI service any more. Each connected device owns its own — see
        # WebSocketHandler.serve_connection — because sharing one of each is
        # what made a second device evict the first.
        self.websocket_handler: Optional[WebSocketHandler] = None
        self.mcp_service: Optional[HomeAssistantMCPService] = None
        self.audio_recording_service: Optional[AudioRecordingService] = None
        self.router = None
        self.session_manager: Optional[SessionManager] = None
        self._pipeline_lock: Optional[asyncio.Lock] = None
        self.speaker_male_name = ""
        self.speaker_female_name = ""
        self.male_only_tools: set[str] = set()
        # Bana 0 (raawr US-016): the local Wyoming STT, None = off.
        self.bana0_stt: Optional[tuple[str, int]] = None
        # device_id -> monotonic time of the last HA-recovery recycle, so a
        # flapping HA cannot bounce a device again and again (raawr D-72).
        self._last_recycle: dict[str, float] = {}
        
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
        # (turn 1 answers, turn 2 hangs in "thinking"). See create_service.
        semantic_vad_create_response = os.environ.get("SEMANTIC_VAD_CREATE_RESPONSE", "true").strip().lower() == "true"
        # Bana 0 (raawr US-016): plain home commands go to HA's own agent
        # first; the model is asked only on a miss, so with bana 0 on the
        # server must NOT create a response per turn (see provider_options).
        # semantic_vad only: the hook ends a turn the way semantic_vad does.
        from app.bana0 import stt_adress
        self.bana0_stt = stt_adress(os.environ.get("BANA0_STT", ""))
        if self.bana0_stt and turn_detection_type != "semantic_vad":
            logger.warning("⚠️ bana0_stt needs turn_detection_type semantic_vad — bana 0 off")
            self.bana0_stt = None
        bana0_timeouts = (
            int(os.environ.get("BANA0_STT_TIMEOUT_MS", "600") or 600) / 1000,
            int(os.environ.get("BANA0_COMMS_TIMEOUT_MS", "4000") or 4000) / 1000,
        )
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
            if ha_api.configured():
                logger.info("Loading Home Assistant MCP tools...")
                self.mcp_service = HomeAssistantMCPService()
                mcp_client = await self.mcp_service.initialize()
                logger.info("✅ Home Assistant MCP Client initialized")
            else:
                logger.warning("⚠️ HA_API_URL or COMMS_NYCKEL not set, skipping Home Assistant MCP integration")
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
        self.websocket_handler.bana0_stt = self.bana0_stt
        self.websocket_handler.bana0_timeouts = bana0_timeouts
        if self.bana0_stt:
            logger.info(f"⚡ bana 0 on: STT {self.bana0_stt[0]}:{self.bana0_stt[1]}, timeouts {bana0_timeouts}")

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
        # Bana 0 speaks HA's confirmation through the same guarded lane.
        self.websocket_handler.say = self._guarded_say
        self.timer_registry.allow_legacy_ring = lambda device_id: (
            len(self.websocket_handler.devices) == 1
            and self.websocket_handler.resolve_device(device_id) is not None
        )

        # Announce endpoint (fork): a LAN route back to the device so the
        # household's agent can speak results of long-running work. Reuses the
        # guarded announcer above; off unless both port and token are set.
        announce_port = int(os.environ.get("ANNOUNCE_PORT", "0") or 0)
        announce_token = os.environ.get("ANNOUNCE_TOKEN", "").strip()
        # 127.0.0.1 when a reverse proxy in front is the only way in (US-014).
        announce_host = os.environ.get("ANNOUNCE_HOST", "0.0.0.0")
        if announce_port and announce_token:
            await start_announce_server(
                announce_port, announce_token, self._guarded_say,
                lambda device_id: self.websocket_handler.resolve_device(device_id) is not None,
                host=announce_host,
            )
        elif announce_port or announce_token:
            logger.warning("⚠️ announce endpoint needs BOTH announce_port and announce_token — disabled")

        # Which engine builds the next session, and each engine's own key,
        # model and voice. The instructions/tools/VAD knobs below stay shared
        # -- see provider_options().
        self.router = build_router()
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.gemini_model = os.environ.get("GEMINI_MODEL", "").strip()
        self.gemini_voice = os.environ.get("GEMINI_VOICE", "").strip()
        # Gemini's own turn detection: easy to start a turn (START LOW never
        # opened one for most commands, 2026-10-02 -- see ProviderOptions),
        # slow to end one.
        self.gemini_vad_start_sensitivity = (
            os.environ.get("GEMINI_VAD_START_SENSITIVITY", "").strip() or "high"
        )
        self.gemini_vad_end_sensitivity = (
            os.environ.get("GEMINI_VAD_END_SENSITIVITY", "").strip() or "low"
        )
        try:
            self.gemini_vad_prefix_padding_ms = int(
                os.environ.get("GEMINI_VAD_PREFIX_PADDING_MS", "300")
            )
        except ValueError:
            self.gemini_vad_prefix_padding_ms = 300
        try:
            self.gemini_vad_silence_duration_ms = int(
                os.environ.get("GEMINI_VAD_SILENCE_DURATION_MS", "800")
            )
        except ValueError:
            self.gemini_vad_silence_duration_ms = 800
        try:
            self.gemini_turn_silence_ms = int(os.environ.get("GEMINI_TURN_SILENCE_MS", "1200"))
        except ValueError:
            self.gemini_turn_silence_ms = 1200
        self.gemini_proactive_audio = (
            os.environ.get("GEMINI_PROACTIVE_AUDIO", "").strip().lower() == "true"
        )
        self.gemini_affective_dialog = (
            os.environ.get("GEMINI_AFFECTIVE_DIALOG", "").strip().lower() == "true"
        )

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
    
    def provider_options(self, provider: str):
        """The knobs for one engine, from the add-on options.

        Args:
            provider: "openai" or "gemini".

        Returns:
            A ProviderOptions. The instructions are the same for both engines,
            memory included -- only the key, model and voice differ.
        """
        from app.providers import GEMINI, ProviderOptions

        instructions = self.instructions + memory_instructions()
        if provider == GEMINI:
            return ProviderOptions(
                api_key=self.gemini_api_key,
                model=self.gemini_model,
                voice=self.gemini_voice,
                instructions=instructions,
                max_output_tokens=self.max_output_tokens,
                language=self.transcription_language or "sv-SE",
                gemini_vad_start_sensitivity=self.gemini_vad_start_sensitivity,
                gemini_vad_end_sensitivity=self.gemini_vad_end_sensitivity,
                gemini_vad_prefix_padding_ms=self.gemini_vad_prefix_padding_ms,
                gemini_vad_silence_duration_ms=self.gemini_vad_silence_duration_ms,
                gemini_turn_silence_ms=self.gemini_turn_silence_ms,
                gemini_proactive_audio=self.gemini_proactive_audio,
                gemini_affective_dialog=self.gemini_affective_dialog,
            )
        return ProviderOptions(
            api_key=self.openai_api_key,
            model=self.model,
            voice=self.voice,
            instructions=instructions,
            max_output_tokens=self.max_output_tokens,
            speed=self.openai_speed,
            noise_reduction=self.noise_reduction,
            turn_detection_type=self.turn_detection_type,
            vad_eagerness=self.vad_eagerness,
            vad_threshold=self.vad_threshold,
            vad_prefix_padding_ms=self.vad_prefix_padding_ms,
            vad_silence_duration_ms=self.vad_silence_duration_ms,
            # Bana 0 on: the agent sends response.create itself, on a miss.
            semantic_vad_create_response=self.semantic_vad_create_response and not self.bana0_stt,
            interrupt_response=self.interrupt_response,
            transcription_model=self.transcription_model,
            transcription_language=self.transcription_language,
        )

    async def create_service(self, connection):
        """Create a voice-engine session for ONE device.

        This used to assign the single `self.openai_service`, so a second
        device connecting replaced the first device's live session and wiped
        its conversation. It now returns a fresh service that belongs to the
        calling connection and to nothing else.

        It also used to build only OpenAI sessions. It now builds whichever
        engine `connection.provider` already names -- WebSocketHandler.
        serve_connection decides that once per connection (see its own
        docstring) before this is ever called, so a failover recorded
        against the router (see Task 7/8) changes what gets built by
        changing what the caller stamps onto connection.provider, not by
        this method asking the router itself.

        Args:
            connection: The DeviceConnection the session will serve. Its
                transport is needed so device-scoped tools act on that
                device, and `connection.provider` must already hold the
                engine to build -- this method does not decide it and does
                not consult `self.router`.

        Returns:
            A newly created pipecat LLMService.
        """
        client_id = connection.device_id
        if self._pipeline_lock is None:
            self._pipeline_lock = asyncio.Lock()

        async with self._pipeline_lock:
            if client_id is None:
                logger.warning("⚠️ No client_id provided to create_service")

            # Create new session
            if client_id:
                logger.info(f"🆕 Creating new session for Client {client_id}...")
            else:
                logger.info("🆕 Creating new session...")

            # Cache context from this DEVICE's previous session (if it is
            # reconnecting) so the conversation survives the reconnect.
            if client_id and self.session_manager.get_current_service(client_id) is not None:
                try:
                    self.session_manager.cleanup_before_new_session(client_id)
                    logger.debug(f"Cached context from previous session for client {client_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Error caching context from old service for client {client_id}: {e}")
            
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

            # Keyword search across the house. The Hass* tools only match whole
            # sentences against exact names, so without this the assistant can
            # only read what it already knows the name of. See search_home_tool.
            all_tools.append(get_search_home_tool_definition())

            # Playing something by name. HassMediaSearchAndPlay ranks every
            # provider together and picks a Spotify track when the user asked
            # for a radio station; this one searches per kind. See
            # play_media_tool.
            all_tools.append(get_play_media_tool_definition())

            # Voice enrollment tool (fork): guided voice-training capture.
            all_tools.append(get_follow_up_tool_definition())
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
                    mcp_tools_schema = await self._fetch_ha_tools_schema()
                    ha_tools = self._ha_tool_definitions(mcp_tools_schema)
                    all_tools.extend(ha_tools)
                    if self.mcp_tool_allowlist:
                        logger.info(f"✅ Fetched {len(mcp_tools_schema.standard_tools)} MCP tools, exposing {len(ha_tools)} per allow-list")
                    else:
                        logger.info(f"✅ Fetched {len(mcp_tools_schema.standard_tools)} MCP tools")
                except Exception as e:
                    mcp_tools_schema = None
                    logger.warning(f"⚠️ Failed to fetch MCP tool definitions: {e}")
            
            from app.providers import build_service

            # The engine is decided exactly ONCE per connection, by
            # WebSocketHandler.serve_connection, before the transport is even
            # built (it needs to know the mic rate up front). That decision
            # is what's on connection.provider already -- read it back here
            # rather than asking self.router again. Querying the router a
            # second time here used to be able to disagree with the first
            # query (another connection's failure landing in the gap, which
            # includes the pipeline lock and an MCP tool-schema fetch above --
            # a real, awaited gap, not a theoretical one), splitting the
            # transport's declared mic rate and the actually-built session
            # across two different engines.
            provider = connection.provider
            options = self.provider_options(provider)
            if not options.api_key:
                logger.error(f"❌ no API key configured for {provider}")
            logger.info(
                f"🔧 Creating {provider} session with {len(all_tools)} tools: "
                f"{[tool.get('name', 'unknown') for tool in all_tools]}"
            )
            service = build_service(provider, options, all_tools)
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
            logger.info(f"✅ {provider} service created: {type(service).__name__}")
            
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

            service.register_function("search_home", create_search_home_tool_handler())
            logger.info("✅ Registered search_home tool handler")

            service.register_function("play_media", create_play_media_tool_handler())
            logger.info("✅ Registered play_media tool handler")
            
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

            def _note_follow_up():
                emitter = connection.phase_emitter
                if emitter is not None:
                    emitter.note_follow_up_requested()

            service.register_function(
                "request_follow_up", create_follow_up_tool_handler(_note_follow_up)
            )
            register_timer_tools(service, self.timer_registry, connection.device_id)
            register_memory_tools(service, _current_speaker_name)
            if openclaw_url():
                register_openclaw_tool(service)
                logger.info("✅ Registered DIRECT ask_openclaw tool (bypassing HA MCP 60s cap)")
            logger.info("✅ Registered timer + memory tools")

            # Register MCP tool handlers if available
            if self.mcp_client and mcp_tools_schema:
                await self._register_ha_handlers(service, mcp_tools_schema)
            elif self.mcp_client:
                # HA was down (restart) while this session was built. The
                # connection can live for hours, so fetch again in the
                # background rather than waiting for the device to reconnect
                # (raawr D-70).
                connection.ha_tools_task = asyncio.create_task(
                    self._recover_ha_tools(connection, service)
                )
            
            # Register service with session manager
            if client_id:
                self.session_manager.set_current_service(client_id, service)

            self._preseed_context(service)

            logger.info("✅ New session created")
            return service

    async def _fetch_ha_tools_schema(self):
        """Fetch HA's MCP tool schema, bounded.

        pipecat's MCP client lets one read hang for up to 300 s. A Home
        Assistant mid-restart held the pipeline lock and kept every new
        session out for 35 min (2026-09-30), so give up after
        MCP_TOOLS_TIMEOUT_SECONDS and let the caller go on without HA tools.

        Returns:
            The ToolsSchema. Raises on any failure, TimeoutError included.
        """
        try:
            mcp_timeout = float(os.environ.get("MCP_TOOLS_TIMEOUT_SECONDS", "5"))
        except ValueError:
            mcp_timeout = 5.0
        try:
            return await asyncio.wait_for(
                self.mcp_client.get_tools_schema(), timeout=mcp_timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"no answer from Home Assistant in {mcp_timeout:g} s")

    def _ha_tool_definitions(self, mcp_tools_schema) -> list:
        """The HA tools the model gets to see, in OpenAI Realtime shape.

        Applies the optional allow-list so the realtime session isn't flooded
        with ha-mcp's 80+ tools, and drops HA tools our own tools replace.
        """
        tools = []
        for function_schema in mcp_tools_schema.standard_tools:
            if self.mcp_tool_allowlist and function_schema.name not in self.mcp_tool_allowlist:
                continue
            if openclaw_url() and function_schema.name == "ask_openclaw":
                continue
            # Our play_media does the same job and can be told what kind of
            # thing to look for. Leaving both in place means the model
            # sometimes picks the one that answers "play P3" with a Spotify
            # track. See play_media_tool.
            if function_schema.name == "HassMediaSearchAndPlay":
                continue
            tools.append({
                "type": "function",
                "name": function_schema.name,
                "description": function_schema.description,
                "parameters": {
                    "type": "object",
                    "properties": function_schema.properties,
                    "required": function_schema.required
                }
            })
        return tools

    async def _register_ha_handlers(self, service, mcp_tools_schema) -> None:
        """Bind HA's MCP tools to `service`, then put the direct ask_openclaw back.

        register_function keys by name, so running this twice replaces the
        handlers rather than adding a second set.
        """
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

    async def _guarded_say(self, text, device_id=None):
        """The conductor's TTS lane, guarded so the device cannot hear itself.

        Used by the announce endpoint and bana 0. NOT by timers: a timer
        expiry is the bell alone, on the second (see app/timers.py).

        Speaks on ONE device: the one named, else the one most recently
        spoken to. Suppresses that device's inbound mic while the
        announcement plays (+ tail) so the assistant can't hear itself and
        reply, and marks the connection busy so the HA-recovery recycle
        cannot cut it (raawr D-72).
        """
        connection = self.websocket_handler.resolve_device(device_id)
        ser = self.websocket_handler.serializer_for(device_id)
        if ser is not None:
            ser.suppress_inbound_until = time.monotonic() + 3600
        if connection is not None:
            connection.says_playing += 1
        try:
            return await self.enrollment_conductor._say(text, device_id=device_id)
        finally:
            if ser is not None:
                ser.suppress_inbound_until = time.monotonic() + 1.2
            if connection is not None:
                connection.says_playing -= 1
                connection.last_busy = time.monotonic()

    async def _recover_ha_tools(self, connection, service) -> None:
        """Retry the HA tool fetch; once HA answers, recycle the connection.

        Runs outside the pipeline lock, and each attempt keeps the fetch
        timeout, so a turn never waits on HA. When HA is back the device's
        socket is closed (normal close) as soon as the device is idle; the
        firmware reconnects and create_service builds a fresh session with
        the full tool list through the normal path. 0.21.1 pushed the tools
        into the live OpenAI session instead (session.update), and live
        2026-10-01 that left the session deaf until the device reconnected.

        At most one recycle per device per MCP_RECYCLE_MIN_INTERVAL_SECONDS
        (600): if HA flaps, keep fetching and recycle once the interval is
        over. "Idle" includes announcements and the follow-up window
        (_device_idle).

        Cancelled by WebSocketHandler._teardown when the device disconnects.
        """
        retry_s = _env_seconds("MCP_TOOLS_RETRY_SECONDS", 15.0)
        poll_s = _env_seconds("MCP_RECYCLE_POLL_SECONDS", 3.0)
        follow_up_s = getattr(self.websocket_handler, "follow_up_ms", 0) / 1000.0
        quiet_s = _env_seconds("MCP_RECYCLE_QUIET_SECONDS", 30.0) + follow_up_s
        min_interval_s = _env_seconds("MCP_RECYCLE_MIN_INTERVAL_SECONDS", 600.0)
        device_id = connection.device_id
        held_back = False
        try:
            while True:
                await asyncio.sleep(retry_s)
                if connection.openai_service is not service:
                    return
                try:
                    schema = await self._fetch_ha_tools_schema()
                except Exception as e:
                    logger.debug(f"HA tools still unavailable for {device_id}: {e}")
                    continue
                since = time.monotonic() - self._last_recycle.get(device_id, float("-inf"))
                if since < min_interval_s:
                    # HA is flapping: keep fetching, recycle once the interval is over.
                    if not held_back:
                        logger.warning(
                            f"⚠️ HA back, but {device_id} was recycled {since:.0f} s ago — "
                            f"holding off until {min_interval_s:.0f} s have passed"
                        )
                        held_back = True
                    continue
                break
            exposed = len(self._ha_tool_definitions(schema))
            while not _device_idle(connection, quiet_s):
                await asyncio.sleep(poll_s)
                if connection.openai_service is not service:
                    return
            logger.info(
                f"✅ HA back — recycling connection for {connection.device_id} "
                f"to load {exposed} tools"
            )
            # Off the connection before closing: the close leads to
            # _teardown, which would otherwise cancel this very task.
            connection.ha_tools_task = None
            self._last_recycle[device_id] = time.monotonic()
            try:
                await connection.websocket.close(code=1000)
            except Exception as e:
                logger.warning(f"⚠️ could not recycle {connection.device_id}'s connection: {e!r}")
        finally:
            if connection.ha_tools_task is asyncio.current_task():
                connection.ha_tools_task = None

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
        # Bana 0 turns create_response off but still needs the pre-seed, or
        # the first context makes pipecat greet the room.
        if not (self.turn_detection_type == "semantic_vad"
                and (self.semantic_vad_create_response or self.bana0_stt)):
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

    def _wire_websocket_handler(self) -> None:
        """Point the handler at this app's session factory and router.

        Split out of run() so this wiring -- not uvicorn startup -- is what a
        test calls and asserts on directly. `self.websocket_handler.router`
        must end up the SAME ProviderRouter object as `self.router`: the
        handler picks each new connection's initial engine from it (for the
        mic sample rate) and, later, uses it to recover a failed session onto
        the backup engine. A copy or a missing assignment leaves failover
        reading a router nobody ever reports failures against.
        """
        # No pipeline is built here any more. There is no process-wide session
        # to build one around: each device brings its own when it connects.
        self.websocket_handler.openai_service_factory = self.create_service
        self.websocket_handler.router = self.router

    async def run(self) -> None:
        """Run the application."""
        import uvicorn

        await self.initialize()
        self._wire_websocket_handler()

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
