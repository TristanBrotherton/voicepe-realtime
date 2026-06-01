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
from pipecat.transports.websocket.server import WebsocketServerTransport
from app.mcp_service import HomeAssistantMCPService
from app.disconnect_tool import get_disconnect_tool_definition, create_disconnect_tool_handler
from app.web_search_tool import get_web_search_tool_definition, create_web_search_tool_handler
from app.audio_recording_service import AudioRecordingService
from app.session_manager import SessionManager
from app.websocket_handler import WebSocketHandler

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


class Application:
    """Main application class using Pipecat."""
    
    def __init__(self):
        """Initialize application."""
        self.pipeline: Optional[Pipeline] = None
        self.runner: Optional[PipelineRunner] = None
        self.websocket_handler: Optional[WebSocketHandler] = None
        self.websocket_transport: Optional[WebsocketServerTransport] = None
        self.openai_service: Optional[OpenAIRealtimeLLMService] = None
        self.mcp_service: Optional[HomeAssistantMCPService] = None
        self.audio_recording_service: Optional[AudioRecordingService] = None
        self.session_manager: Optional[SessionManager] = None
        self.current_task: Optional[PipelineTask] = None
        self._pipeline_lock: Optional[asyncio.Lock] = None
        
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
        # (turn 1 answers, turn 2 hangs in "thinking"). See _ensure_openai_service.
        semantic_vad_create_response = os.environ.get("SEMANTIC_VAD_CREATE_RESPONSE", "true").strip().lower() == "true"
        # Expose the `disconnect_client` tool to the model. DEFAULT FALSE: on the
        # Voice PE the device owns its own session lifecycle (wake word starts a
        # turn, the no-speech watchdog / idle phase ends it), so a model-driven
        # disconnect just tears down the persistent WebSocket mid-conversation —
        # it was seen closing the socket DURING the first reply ("conversation_ended").
        # Only enable if your device relies on the backend to hang up.
        enable_disconnect_tool = os.environ.get("ENABLE_DISCONNECT_TOOL", "false").strip().lower() == "true"
        # Pin the input-transcription language (ISO code, e.g. "nl"). Empty = let
        # the model auto-detect. Helps stop the model drifting to another
        # language; pair it with an explicit language lock in `instructions`.
        transcription_language = os.environ.get("TRANSCRIPTION_LANGUAGE", "").strip()
        # Model that transcribes the user's speech to TEXT (the transcript shown
        # in logs + put in the context). NOTE: this is NOT what gpt-realtime-2
        # uses to understand you — the main model hears the audio natively; this
        # only affects the side-channel transcript. Default "gpt-4o-transcribe".
        # Alternatives: "gpt-4o-mini-transcribe", "whisper-1", and the newer
        # streaming "gpt-realtime-whisper" (purpose-built for the Realtime API,
        # faster/cheaper). If the API rejects a value, transcription silently
        # falls back; check the logs.
        transcription_model = os.environ.get("TRANSCRIPTION_MODEL", "gpt-4o-transcribe").strip()

        # Get instructions with default
        instructions = os.environ.get("INSTRUCTIONS", "You are the Home Assistant Voice Agent and can control the Smart Home.")

        # OpenAI Realtime model + voice. Defaults to gpt-realtime-2 (this fork's
        # target); fall back to "gpt-realtime" if the API rejects it.
        openai_model = os.environ.get("OPENAI_MODEL", "gpt-realtime-2")
        openai_voice = os.environ.get("OPENAI_VOICE", "marin")

        # Optional allow-list to trim the (large) ha-mcp tool set exposed to the
        # model. Comma-separated tool names; empty means expose all.
        mcp_tool_allowlist = [t.strip() for t in os.environ.get("MCP_TOOL_ALLOWLIST", "").split(",") if t.strip()]
        
        # Web search: let the assistant look things up online (weather, news,
        # facts). Off by default so the add-on Update doesn't silently change
        # behaviour. When on, a `web_search` function tool calls OpenAI's
        # Responses web_search built-in tool server-side (using OPENAI_API_KEY)
        # and returns a short spoken answer. The model is configurable so a
        # different price/quality — or a renamed model — needs no code change.
        enable_web_search = os.environ.get("ENABLE_WEB_SEARCH", "false").lower() == "true"
        web_search_model = os.environ.get("WEB_SEARCH_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"

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
            follow_up_open_delay_ms = int(os.environ.get("FOLLOW_UP_OPEN_DELAY_MS", "200"))
        except (TypeError, ValueError):
            follow_up_open_delay_ms = 200
        follow_up_open_delay_ms = max(0, min(5000, follow_up_open_delay_ms))
        # Playback jitter buffer (ms): the device holds incoming TTS until this
        # much has accumulated before playing, so a brief network hiccup doesn't
        # dry out the speaker chain mid-word (audible crackle). Sent in `hello`.
        try:
            playback_prebuffer_ms = int(os.environ.get("PLAYBACK_PREBUFFER_MS", "150"))
        except (TypeError, ValueError):
            playback_prebuffer_ms = 150
        playback_prebuffer_ms = max(0, min(2000, playback_prebuffer_ms))

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
        
        # Initialize WebSocket handler
        self.websocket_handler = WebSocketHandler(
            host=websocket_host,
            port=websocket_port,
            session_manager=self.session_manager,
            audio_recording_service=self.audio_recording_service,
            follow_up_ms=follow_up_ms,
            follow_up_open_delay_ms=follow_up_open_delay_ms,
            playback_prebuffer_ms=playback_prebuffer_ms,
        )
        logger.info(
            f"🔁 Follow-up window: {follow_up_listen_seconds}s "
            f"({'enabled' if follow_up_ms > 0 else 'disabled — turn-based'}), "
            f"mic-open delay {follow_up_open_delay_ms}ms, "
            f"playback prebuffer {playback_prebuffer_ms}ms"
        )
        self.websocket_transport = self.websocket_handler.create_transport()
        
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
        self.mcp_tool_allowlist = mcp_tool_allowlist
        self.mcp_client = mcp_client
        self.enable_web_search = enable_web_search
        self.web_search_model = web_search_model

        # Initialize audio recording service (optional)
        self.audio_recording_service = AudioRecordingService(
            enable_recording=enable_recording,
            sample_rate=24000,
            chunk_duration_seconds=30,
            output_dir="recordings"
        )
        
        logger.info("✅ Application initialized - ready to accept WebSocket connections")
    
    def _build_pipeline_for_transport(self, transport: WebsocketServerTransport, client_id: str):
        """
        Build pipeline for a WebSocket transport connection.
        
        Args:
            transport: The WebSocket transport instance
            client_id: Unique identifier for the client device
        """
        # Ensure OpenAI service exists
        if self.openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")
        
        # Use WebSocket handler to build pipeline
        self.pipeline, self.runner, self.current_task = self.websocket_handler.build_pipeline(
            transport=transport,
            openai_service=self.openai_service,
            client_id=client_id,
            activity_callback=self._update_session_activity
        )
    
    def _update_session_activity(self):
        """Update session activity timestamp (called by SessionActivityTracker)."""
        pass
    
    async def _ensure_openai_service(self, client_id: Optional[str] = None):
        """Create a new OpenAI service instance for a client.
        
        Args:
            client_id: Optional client ID for session management
        """
        if self._pipeline_lock is None:
            self._pipeline_lock = asyncio.Lock()
        
        async with self._pipeline_lock:
            if client_id is None:
                logger.warning("⚠️ No client_id provided to _ensure_openai_service")
            
            # Create new session
            if client_id:
                logger.info(f"🆕 Creating new OpenAI Session for Client {client_id}...")
            else:
                logger.info("🆕 Creating new OpenAI Session...")
            
            # Cache context from old service before creating new one
            if client_id and self.openai_service is not None:
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

            session_properties = SessionProperties(
                instructions=self.instructions,
                audio=AudioConfiguration(
                    input=AudioInput(
                        turn_detection=turn_detection,
                        transcription=transcription,
                    ),
                    output=AudioOutput(voice=self.voice)
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
            self.openai_service = SafeRealtimeLLMService(
                api_key=self.openai_api_key,
                model=self.model,
                session_properties=session_properties,
                start_audio_paused=False
            )
            logger.info(f"✅ OpenAI Service created: {type(self.openai_service).__name__}")
            
            # Register disconnect tool handler (only when the tool is exposed)
            if self.enable_disconnect_tool:
                disconnect_tool_handler = create_disconnect_tool_handler(self.websocket_transport)
                self.openai_service.register_function("disconnect_client", disconnect_tool_handler)
                logger.info("✅ Registered disconnect tool handler")

            # Register web search tool handler (only when the tool is exposed)
            if self.enable_web_search:
                self.openai_service.register_function(
                    "web_search",
                    create_web_search_tool_handler(self.openai_api_key, self.web_search_model),
                )
                logger.info(f"✅ Registered web_search tool handler (model={self.web_search_model})")
            
            # Register MCP tool handlers if available
            if self.mcp_client and mcp_tools_schema:
                try:
                    await self.mcp_client.register_tools_schema(mcp_tools_schema, self.openai_service)
                    logger.info(f"✅ Registered {len(mcp_tools_schema.standard_tools)} MCP tool handlers")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to register MCP tool handlers: {e}")
            
            # Register service with session manager
            if client_id:
                self.session_manager.set_current_service(client_id, self.openai_service)
            
            logger.info("✅ New OpenAI Session created")
            return self.openai_service
    
    async def run(self) -> None:
        """Run the application."""
        await self.initialize()
        
        # Create initial OpenAI service (will be replaced per connection)
        await self._ensure_openai_service()
        
        # Build pipeline - based on pipecat-examples, one pipeline handles all connections
        # The transport manages multiple connections internally
        self._build_pipeline_for_transport(self.websocket_transport, "server")

        # Consume pipecat's FIRST-context auto-response ONCE at startup — SILENTLY.
        # WHY: pipecat 0.0.97's OpenAIRealtimeLLMService._handle_context does
        # `if not self._context: ... await self._create_response()` — i.e. the
        # very first context it ever sees triggers a real response. With
        # semantic_vad create_response=True the SERVER also creates a response on
        # every user turn, so the user's first turn would double-create →
        # `conversation_already_has_active_response` (cut turn 1 short, hung
        # turn 2). We previously consumed that path with a throwaway LLMRunFrame
        # kickoff — but an LLMRunFrame runs `_create_response()`, producing a REAL
        # (audible, tool-calling) reply. The old comment assumed it "goes to no
        # device" because nothing is connected at startup; WRONG: when the user
        # updates the add-on the device auto-reconnects within seconds and lands
        # mid-kickoff (and its post-tool follow-up), so the device plays a
        # spontaneous "answer" nobody asked for (observed: "Ik vond geen
        # betrouwbare lamp in de gang" right after a restart).
        #
        # Fix: pre-set `self._context` to an empty LLMContext instead. Now the
        # first REAL user turn hits the ELSE branch of _handle_context (no
        # _create_response), the server creates that turn's response (semantic_vad
        # create_response=True), and there's no double — AND no startup speech.
        # The empty sentinel is harmlessly overwritten by the real context on the
        # first turn (both branches do `self._context = context`).
        if self.turn_detection_type == "semantic_vad" and self.semantic_vad_create_response:
            try:
                from pipecat.processors.aggregators.llm_context import LLMContext
                if self.openai_service is not None and getattr(self.openai_service, "_context", None) is None:
                    self.openai_service._context = LLMContext()
                    # Also mark pipecat's one-time "conversation setup" as already
                    # done. pipecat runs it on the FIRST _create_response: it
                    # re-sends the context's messages as ConversationItemCreate
                    # events, then flips _llm_needs_conversation_setup False. On a
                    # fresh realtime session OpenAI already builds the conversation
                    # from the live audio + tool-call flow, so that one-time setup
                    # re-injects items OpenAI already has — which made the first
                    # post-tool reply come out as a meaningless filler ("Ik ben
                    # klaar om verder te gaan met het gesprek."). Instructions are
                    # sent independently via _update_settings() on session.created,
                    # so clearing this flag is safe and makes the first real turn a
                    # normal reply.
                    if hasattr(self.openai_service, "_llm_needs_conversation_setup"):
                        self.openai_service._llm_needs_conversation_setup = False
                    logger.info("🌱 Pre-seeded empty context + marked conversation setup done (no startup speech, no first-turn filler)")
                else:
                    logger.info("🌱 Startup context already set; skipping pre-seed")
            except Exception as e:
                logger.warning(f"⚠️ Could not pre-seed startup context (turn-1 double may occur): {e}")

        # Setup WebSocket event handlers
        async def on_client_connected(client_id: str):
            """Handle new client connection."""
            await self._ensure_openai_service(client_id=client_id)
            if self.audio_recording_service:
                self.audio_recording_service.start_new_session(client_id)
        
        def on_client_disconnected(client_id: str):
            """Handle client disconnection."""
            if self.session_manager:
                self.session_manager.handle_client_disconnect(client_id, self.openai_service)
            if self.audio_recording_service:
                self.audio_recording_service.stop_recording()
        
        # Function to get OpenAI service for a client
        def get_openai_service_for_client(client_id: str) -> Optional[OpenAIRealtimeLLMService]:
            """Get OpenAI service for a specific client."""
            if self.session_manager:
                return self.session_manager.get_current_service(client_id)
            return self.openai_service
        
        self.websocket_handler.setup_event_handlers(
            transport=self.websocket_transport,
            on_client_connected_callback=on_client_connected,
            on_client_disconnected_callback=on_client_disconnected,
            openai_service_getter=get_openai_service_for_client
        )
        
        try:
            # Start the pipeline runner - this will start the WebSocket server
            # Based on pipecat-examples: PipelineRunner.run() starts the transport server
            logger.info("✅ Starting WebSocket server and pipeline...")
            await self.runner.run(self.current_task)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            raise
        finally:
            await self.cleanup()
    
    async def cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up application...")
        
        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")
        
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
