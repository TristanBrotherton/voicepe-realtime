"""WebSocket handler for managing WebSocket connections and pipelines."""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Callable, Awaitable, Dict

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.websocket.server import WebsocketServerTransport, WebsocketServerParams
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame, EndFrame, ErrorFrame
from pipecat.audio.utils import create_stream_resampler
from pipecat.services.openai.realtime import events as openai_rt_events

from app.raw_audio_serializer import RawAudioSerializer
from app.session_manager import SessionManager
from app.audio_recording_service import AudioRecordingService
from app.phase_emitter import PhaseEmitter
from app.transcript_logger import TranscriptLogger
from app.debug_frame_logger import DebugFrameLogger

logger = logging.getLogger(__name__)

# The OpenAI Realtime API works in 24 kHz PCM16. The Voice PE firmware plays
# 24 kHz back and streams 16 kHz up. IMPORTANT: pipecat 0.0.97's websocket INPUT
# transport does NOT resample (only the OUTPUT transport does), and OpenAI
# Realtime's pcm16 input rate is hard-locked to 24000 (PCMAudioFormat.rate =
# Literal[24000]) — you cannot tell it the audio is 16 kHz. So the device's
# 16 kHz frames would be read 1.5x too fast / pitched up, garbling the whole
# transcript. The InputResampler below upsamples 16k->24k in the pipeline.
PIPELINE_SAMPLE_RATE = 24000


class SessionActivityTracker(FrameProcessor):
    """Processor that tracks session activity by monitoring audio frames."""
    
    def __init__(self, activity_callback, **kwargs):
        super().__init__(**kwargs)
        self.activity_callback = activity_callback
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, StartFrame):
            logger.debug("🎬 SessionActivityTracker: Received StartFrame")
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        elif isinstance(frame, EndFrame):
            logger.debug("🏁 SessionActivityTracker: Received EndFrame")
            await self.push_frame(frame, direction)
            return
        
        # Track activity on any audio frame
        if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame)):
            if self.activity_callback:
                self.activity_callback()
            logger.debug(f"🎵 SessionActivityTracker: Processing {type(frame).__name__} ({len(frame.audio)} bytes)")
        
        # Pass frame through to next processor
        await self.push_frame(frame, direction)


class InputResampler(FrameProcessor):
    """Upsample incoming device mic audio to the OpenAI Realtime input rate.

    The Voice PE streams 16 kHz PCM16. pipecat 0.0.97's websocket input transport
    forwards those frames unchanged, and OpenAI Realtime reads pcm16 input at a
    fixed 24 kHz — so without this the audio is interpreted ~1.5x too fast,
    badly degrading transcription (e.g. first word dropped, words mangled). This
    sits right after transport.input() and resamples each InputAudioRawFrame to
    out_rate. Uses a streaming resampler so there are no per-chunk edge artifacts.
    """

    def __init__(self, out_rate: int = PIPELINE_SAMPLE_RATE, **kwargs):
        super().__init__(**kwargs)
        self._out_rate = out_rate
        self._resampler = create_stream_resampler()
        self._logged = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.sample_rate != self._out_rate:
            if not frame.audio:
                return  # nothing to resample / forward; don't emit empty audio
            try:
                resampled = await self._resampler.resample(
                    frame.audio, frame.sample_rate, self._out_rate
                )
            except Exception as e:
                logger.warning(f"⚠️ input resample {frame.sample_rate}->{self._out_rate} failed: {e!r}")
                return  # drop rather than forward wrong-rate audio
            # The streaming resampler buffers internally and can return empty
            # bytes while priming or on a tiny chunk. OpenAI rejects an
            # input_audio_buffer.append with empty audio ("got empty bytes"), so
            # drop those frames — the samples stay buffered and come out next call.
            if not resampled:
                return
            if not self._logged:
                logger.info(
                    f"🎙️ Resampling device input {frame.sample_rate}Hz -> {self._out_rate}Hz for OpenAI"
                )
                self._logged = True
            frame = InputAudioRawFrame(
                audio=resampled,
                sample_rate=self._out_rate,
                num_channels=frame.num_channels,
            )
        await self.push_frame(frame, direction)


class ConnectionRecovery(FrameProcessor):
    """Auto-reconnect the OpenAI Realtime session when its WebSocket dies.

    pipecat 0.0.97's OpenAIRealtimeLLMService has NO reconnect logic: when the
    OpenAI WS drops (1011 keepalive ping timeout, 1001 going away on the 60-min
    cap, 1006, or any send/receive failure) it treats the send error as fatal and
    floods ErrorFrame — ~15/s, one per forwarded mic frame — forever. The single
    persistent session is then dead until the add-on restarts, so the device gets
    no answer to any further turn (observed live: a 1011 flood after which the
    next question got silence).

    This processor watches the ErrorFrames as they travel upstream to the task
    source, and on the first connection-death signature it:
      1. emits `idle` to the device so it unsticks (LED + mic reset), and
      2. calls service.reset_conversation() — the one PUBLIC method that does
         _disconnect() + _connect() + re-sends the session config (instructions,
         tools, turn detection) — to bring the session back IN PLACE. No pipeline
         rebuild: the running pipeline keeps the same service object, which is
         exactly the one reset_conversation reconnects.
    A guard + cooldown collapse the error flood into a single reconnect attempt,
    retrying at most every RECONNECT_COOLDOWN_S while the link stays down.
    """

    # Substrings that mark a dead/closed OpenAI websocket (vs an app-level error
    # like a tool failure, which we must NOT reconnect on). These appear on the
    # SEND-side flood ("Error sending client event: …"), so they're paired with
    # the "client event" check below to avoid reacting to a device disconnect.
    _DEATH_MARKERS = (
        "keepalive ping timeout",
        "going away",
        "no close frame",
        "ConnectionClosed",
        "connection is closed",
        "sent 1011",
        "sent 1001",
        "1006",
    )
    # Substrings that UNAMBIGUOUSLY mean OUR OpenAI session is gone and must be
    # reconnected, regardless of how the error surfaced. The 60-minute cap can
    # arrive as a proactive OpenAI *error event* (code='session_expired', "Your
    # session hit the maximum duration of 60 minutes.") with NO "client event"
    # send-flood and NO close-code marker — so the paired check above misses it
    # and the session stays dead until the add-on restarts. These markers force a
    # reconnect on their own. They can only come from OpenAI (not a device close),
    # so no "client event" guard is needed.
    _SESSION_DEAD_MARKERS = (
        "session_expired",
        "maximum duration",
    )
    RECONNECT_COOLDOWN_S = 5.0
    IDLE_UNSTICK_COOLDOWN_S = 2.0

    def __init__(self, openai_service, emit_idle=None, **kwargs):
        super().__init__(**kwargs)
        self._service = openai_service
        self._emit_idle = emit_idle  # async callable(value:str), e.g. broadcast_phase
        self._reconnecting = False
        self._last_attempt = 0.0
        self._last_idle_unstick = 0.0
        # Diagnostics: when the current OpenAI session connected, so we can log its
        # age at a drop (the 60-min cap shows up as ~3600 s) and the reconnect
        # duration (the brief gap the user hears).
        self._connected_at = time.monotonic()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame) and not self._reconnecting:
            msg = str(getattr(frame, "error", "") or "")
            # Two reconnect triggers:
            #  (a) the OpenAI send-side flood ("Error sending client event: …" +
            #      a close-code marker) — OUR WS died mid-send. We require the
            #      "client event" signature so a normal DEVICE-side disconnect
            #      (also 1011/ConnectionClosed, but the device went away) does NOT
            #      trigger an OpenAI reconnect.
            #  (b) an unambiguous OpenAI session-dead error event (session_expired
            #      / "maximum duration") — this is the 60-min cap surfacing as a
            #      proactive error event with NO send-flood, so (a) misses it.
            #      It can only come from OpenAI, so it needs no "client event" guard.
            send_flood = "client event" in msg and any(m in msg for m in self._DEATH_MARKERS)
            session_dead = any(m in msg for m in self._SESSION_DEAD_MARKERS)
            if send_flood or session_dead:
                now = time.monotonic()
                if now - self._last_attempt >= self.RECONNECT_COOLDOWN_S:
                    self._reconnecting = True
                    self._last_attempt = now
                    asyncio.create_task(self._recover(msg))
            else:
                # Non-connection-death error that ENDS a turn without a reply:
                # most importantly an OpenAI rate-limit ("Rate limit reached …"),
                # but also any other transient response.create failure. No bot
                # speech was produced, so PhaseEmitter never fires
                # BotStopped→idle; the device is left stuck in `thinking`
                # (LED keeps blinking) with no device-side watchdog to recover.
                # Emit one `idle` to unstick it so the user can just try again.
                # Guarded by a short cooldown so a rare flood collapses to one.
                now = time.monotonic()
                if now - self._last_idle_unstick >= self.IDLE_UNSTICK_COOLDOWN_S:
                    self._last_idle_unstick = now
                    asyncio.create_task(self._unstick_idle(msg))
        await self.push_frame(frame, direction)

    async def _recover(self, reason: str):
        t0 = time.monotonic()
        age_s = t0 - self._connected_at
        try:
            logger.warning(
                f"🔌 OpenAI Realtime connection lost after {age_s:.0f}s "
                f"({reason[:90]}) — reconnecting…"
            )
            # Unstick the device first, regardless of how the reconnect goes.
            if self._emit_idle is not None:
                try:
                    await self._emit_idle("idle")
                except Exception as e:
                    logger.warning(f"⚠️ could not emit idle during recovery: {e!r}")
            reset = getattr(self._service, "reset_conversation", None)
            if reset is None:
                logger.error("❌ service has no reset_conversation(); cannot reconnect in place")
                return
            await reset()
            self._connected_at = time.monotonic()
            logger.info(
                f"✅ OpenAI Realtime session reconnected in {self._connected_at - t0:.1f}s "
                f"(gap the user may have heard)"
            )
        except Exception as e:
            logger.error(f"❌ OpenAI reconnect attempt failed: {e!r}")
        finally:
            self._reconnecting = False

    async def _unstick_idle(self, reason: str):
        """Emit `idle` to the device after a turn-ending error (e.g. rate limit).

        The session is still alive (no reconnect needed) — we just nudge the
        device out of its stuck `thinking` blink so the user can retry.
        """
        try:
            logger.warning(f"⚠️ turn ended on error, emitting idle to unstick device ({reason[:90]})")
            if self._emit_idle is not None:
                await self._emit_idle("idle")
        except Exception as e:
            logger.warning(f"⚠️ could not emit idle after turn-ending error: {e!r}")


class WebSocketHandler:
    """Handles WebSocket transport initialization, pipeline building, and event management."""
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        session_manager: Optional[SessionManager] = None,
        audio_recording_service: Optional[AudioRecordingService] = None,
        follow_up_ms: int = 0,
        follow_up_open_delay_ms: int = 1500,
        playback_prebuffer_ms: int = 0,
    ):
        """
        Initialize WebSocket handler.

        Args:
            host: Host address to bind to
            port: Port to listen on
            session_manager: Session manager instance
            audio_recording_service: Audio recording service instance
            follow_up_ms: How long (ms) the device should keep the mic open
                after a reply so the user can answer without a wake word. Sent to
                the device in the `hello` handshake. 0 = turn-based (no window).
            follow_up_open_delay_ms: How long (ms) the device waits after a reply
                finishes before opening that follow-up mic (bridges the speaker
                hardware tail). Sent in the `hello` handshake.
        """
        self.host = host
        self.port = port
        self.session_manager = session_manager
        self.audio_recording_service = audio_recording_service
        self.follow_up_ms = max(0, int(follow_up_ms))
        self.follow_up_open_delay_ms = max(0, int(follow_up_open_delay_ms))
        self.playback_prebuffer_ms = max(0, int(playback_prebuffer_ms))

        self.transport: Optional[WebsocketServerTransport] = None
        self.pipeline: Optional[Pipeline] = None
        self.runner: Optional[PipelineRunner] = None
        self.current_task: Optional[PipelineTask] = None
        # The serializer instance the transport reads through. Kept so
        # build_pipeline can wire its device-interrupt callback to the OpenAI
        # service.
        self._serializer: Optional[RawAudioSerializer] = None
        # Connected device websockets, used to push va_client control/phase
        # messages as TEXT frames (the audio path uses the binary serializer).
        self._websockets: set = set()
    
    def create_transport(self) -> WebsocketServerTransport:
        """
        Create and initialize WebSocket transport.
        
        Returns:
            WebsocketServerTransport instance
        """
        logger.info("Initializing WebSocket transport...")
        
        # Use RawAudioSerializer for binary PCM audio. It tags incoming frames
        # with the device mic rate (16 kHz for Voice PE); the transport
        # resamples in/out to the 24 kHz pipeline rate below.
        serializer = RawAudioSerializer()
        self._serializer = serializer

        # Create WebsocketServerTransport with WebsocketServerParams
        # The transport will start its own server automatically
        self.transport = WebsocketServerTransport(
            host=self.host,
            port=self.port,
            params=WebsocketServerParams(
                serializer=serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
                audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
            )
        )
        
        logger.info(f"✅ WebSocket transport created - will listen on ws://{self.host}:{self.port}/")
        return self.transport
    
    def build_pipeline(
        self,
        transport: WebsocketServerTransport,
        openai_service: OpenAIRealtimeLLMService,
        client_id: str,
        activity_callback: Optional[Callable[[], None]] = None
    ) -> tuple[Pipeline, PipelineRunner, PipelineTask]:
        """
        Build pipeline for a WebSocket transport connection.
        
        Args:
            transport: The WebSocket transport instance
            openai_service: The OpenAI service instance
            client_id: Unique identifier for the client device
            activity_callback: Optional callback for session activity tracking
            
        Returns:
            Tuple of (Pipeline, PipelineRunner, PipelineTask)
        """
        logger.info(f"🔗 Building pipeline for client: {client_id}")
        
        if openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")
        
        logger.info(f"🔗 Building pipeline with WebSocket transport and OpenAI service: {type(openai_service).__name__}")
        
        # Create activity trackers
        input_activity_tracker = SessionActivityTracker(
            activity_callback=activity_callback or (lambda: None)
        )
        output_activity_tracker = SessionActivityTracker(
            activity_callback=activity_callback or (lambda: None)
        )
        
        # Create context aggregator with cached context if available
        context_aggregator = None
        context_initializer = None
        if self.session_manager:
            context_aggregator = self.session_manager.create_context_aggregator(client_id)
            context_initializer = self.session_manager.create_context_initializer(client_id, context_aggregator)
        
        # Build pipeline components. InputResampler runs FIRST (right after the
        # transport) so every later stage — VAD, context aggregator, OpenAI
        # service — sees correctly-rated 24 kHz audio instead of the device's
        # raw 16 kHz (which OpenAI would otherwise read 1.5x too fast).
        pipeline_components = [
            transport.input(),
            # Watch for OpenAI connection-death ErrorFrames (they travel upstream
            # to the task source, so place this upstream of the service) and
            # reconnect in place. Without it a 1011/1001 drop bricks the session.
            ConnectionRecovery(openai_service=openai_service, emit_idle=self.broadcast_phase),
            InputResampler(out_rate=PIPELINE_SAMPLE_RATE),
            input_activity_tracker,
        ]
        
        # Add input audio recorder to capture ONLY InputAudioRawFrame
        input_recorder = self.audio_recording_service.get_input_recorder() if self.audio_recording_service else None
        if input_recorder:
            pipeline_components.append(input_recorder)
        
        # Continue with rest of pipeline, with transcript-logging taps. The
        # assistant reply text (TTSTextFrame) flows DOWNSTREAM out of the LLM
        # while the user's TranscriptionFrame is pushed UPSTREAM (so the user
        # aggregator can consume it) — opposite directions, so they need taps on
        # opposite sides of the service (see transcript_logger.py): "user" before
        # the LLM, "assistant" after it.
        # DEV diagnostics: a passive frame logger that surfaces the turn / audio /
        # interruption lifecycle. Only active when LOG_LEVEL=DEBUG (the dev add-on
        # default; silent on stable). Placed right after the LLM so it sees the
        # response / TTS / interruption frames plus the VAD speech frames passing
        # through.
        _debug_taps = logging.getLogger().isEnabledFor(logging.DEBUG)
        if context_aggregator:
            pipeline_components.extend([
                context_aggregator.user(),
                TranscriptLogger(capture="user"),
                openai_service,
            ])
            if _debug_taps:
                pipeline_components.append(DebugFrameLogger(label="llm"))
            pipeline_components.extend([
                TranscriptLogger(capture="assistant"),
                context_aggregator.assistant(),
            ])
        else:
            pipeline_components.extend([
                TranscriptLogger(capture="user"),
                openai_service,
            ])
            if _debug_taps:
                pipeline_components.append(DebugFrameLogger(label="llm"))
            pipeline_components.append(TranscriptLogger(capture="assistant"))

        pipeline_components.append(output_activity_tracker)

        # Emit va_client phase messages (listening/thinking/replying/idle) to
        # the device, derived from Pipecat speaking frames as they pass
        # downstream. Placed before transport.output() so it sees both the
        # user (UserStarted/Stopped) and bot (BotStarted/Stopped) frames.
        pipeline_components.append(PhaseEmitter(send_phase=self.broadcast_phase))

        # Add output audio recorder to capture ONLY OutputAudioRawFrame
        output_recorder = self.audio_recording_service.get_output_recorder() if self.audio_recording_service else None
        if output_recorder:
            pipeline_components.append(output_recorder)

        pipeline_components.append(transport.output())
        
        # Add context initializer if we have cached messages
        if context_initializer:
            pipeline_components.append(context_initializer)
        
        pipeline = Pipeline(pipeline_components)
        logger.info("✅ Pipeline created for WebSocket connection")
        
        # Audio recording is handled by AudioFrameRecorder processors in the pipeline
        if self.audio_recording_service:
            logger.info("🎙️ Audio recording enabled - will record input and output audio")
        
        # Create pipeline runner and task
        # Disable idle timeout - server should always stay ready for connections
        runner = PipelineRunner()
        task = PipelineTask(pipeline, idle_timeout_secs=None, cancel_on_idle_timeout=False)
        
        # Start pipeline in background
        asyncio.create_task(runner.run(task))
        logger.info("✅ Pipeline started for WebSocket connection")
        logger.info("✅ Pipeline initialized successfully")

        # Wire the device "stop" interrupt. The serializer calls this when it
        # sees {"type":"interrupt"} from the device.
        #
        # The DEVICE stops playback AUTHORITATIVELY: on "stop" its firmware
        # flushes the PSRAM queue and drops all further incoming TTS
        # (suppress_incoming_audio_) until the next turn boundary. So the backend
        # does NOT need to clear its own output here — the user already hears
        # silence. The backend's only job is to stop OpenAI generating MORE
        # tokens: a plain response.cancel, and ONLY while a response is actually
        # active (avoids the noisy response_cancel_not_active in the common
        # already-burst-finished case).
        #
        # We deliberately do NOT queue an InterruptionTaskFrame anymore. It made
        # pipecat run _handle_interruption → _truncate_current_audio_response(),
        # which tells OpenAI to truncate the assistant audio at the *playback*
        # position. But OpenAI bursts the reply faster than real-time, so that
        # position overshoots the audio that actually exists and OpenAI rejects
        # the truncate with invalid_request_error ("Audio content of N ms is
        # already shorter than M ms"). That error left the realtime session in a
        # broken state where the user's VERY NEXT turn got NO response — the
        # recurring "say stop, then immediately ask again → silence" bug. Since
        # the device already silenced playback, dropping the truncate costs us
        # nothing and keeps the next turn alive. (The backend still drains its
        # already-buffered output to the device, which the device discards —
        # minor wasted bandwidth, tracked as roadmap #3; no extra tokens because
        # response.cancel stops further generation.)
        async def _on_device_interrupt():
            try:
                if getattr(openai_service, "_current_assistant_response", None) is not None:
                    await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                    logger.info("🛑 device interrupt → response.cancel sent (response was still active)")
                else:
                    logger.info("🛑 device interrupt → no active response to cancel (device already silenced)")
            except Exception as e:
                logger.info(f"🛑 device interrupt → response.cancel no-op ({e!r})")

        if self._serializer is not None:
            self._serializer.set_interrupt_handler(_on_device_interrupt)

        return pipeline, runner, task
    
    def extract_client_id(self, websocket) -> str:
        """
        Extract client ID from websocket connection.
        
        Args:
            websocket: WebSocket connection object
            
        Returns:
            Client ID string
        """
        client_ip = None
        if hasattr(websocket, 'client') and websocket.client:
            client_ip = websocket.client.host
        elif hasattr(websocket, 'remote_address'):
            client_ip = str(websocket.remote_address[0]) if websocket.remote_address else None
        
        if not client_ip:
            client_ip = f"unknown_{uuid.uuid4().hex[:8]}"
            logger.warning("⚠️ Could not extract client IP, using generated ID")

        return client_ip

    async def _send_json(self, websocket, obj: dict) -> None:
        """Send a JSON object to one device as a TEXT websocket frame.

        IMPORTANT: use COMPACT separators (no space after ':' or ','). The Voice
        PE va_client does a literal substring match on `"value":"<phase>"`
        (va_client.cpp handle_text_), so the default json.dumps output
        `"value": "listening"` (with a space) would NOT match and the device
        would silently ignore every phase. Compact output `"value":"listening"`
        matches. This is what made listening/thinking/replying never reach the
        device (LED stuck idle, no-speech watchdog never cancelled).
        """
        try:
            await websocket.send(json.dumps(obj, separators=(",", ":")))
        except Exception as e:
            logger.warning(f"⚠️ Could not send {obj.get('type')} to device: {e!r}")

    async def broadcast_json(self, obj: dict) -> None:
        """Send a JSON object to every connected device as a TEXT frame."""
        for ws in list(self._websockets):
            await self._send_json(ws, obj)

    async def broadcast_phase(self, value: str) -> None:
        """Send a va_client phase message to every connected device."""
        # TEMP instrumentation: log the broadcast + how many device sockets we
        # think are connected (was debug).
        logger.info(f"➡️ broadcast phase '{value}' to {len(self._websockets)} device(s)")
        await self.broadcast_json({"type": "phase", "value": value})
    
    def setup_event_handlers(
        self,
        transport: WebsocketServerTransport,
        on_client_connected_callback: Callable[[str], Awaitable[None]],
        on_client_disconnected_callback: Optional[Callable[[str], None]] = None,
        openai_service_getter: Optional[Callable[[str], Optional[OpenAIRealtimeLLMService]]] = None
    ):
        """
        Setup WebSocket event handlers.
        
        Args:
            transport: The WebSocket transport instance
            on_client_connected_callback: Async callback function(client_id) called when client connects
            on_client_disconnected_callback: Optional callback function(client_id) called when client disconnects
            openai_service_getter: Optional function(client_id) -> OpenAIRealtimeLLMService to get service for interrupt
        """
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport: WebsocketServerTransport, websocket):
            """Handle new WebSocket client connection."""
            client_id = self.extract_client_id(websocket)
            logger.info(f"🔗 New WebSocket connection from IP: {client_id}")
            # Track the raw connection so we can push phase/control TEXT frames.
            self._websockets.add(websocket)
            # Handshake ack expected by the va_client protocol (server -> device
            # "hello"). The Voice PE firmware tolerates its absence, but sending
            # it keeps both sides in lockstep with the documented protocol.
            # follow_up_ms tells the device how long to keep the mic open after a
            # reply (post-reply follow-up window); 0/absent = turn-based. Sent on
            # every connect so an add-on config change takes effect on reconnect.
            await self._send_json(
                websocket,
                {
                    "type": "hello",
                    "audio_out": "pcm",
                    "follow_up_ms": self.follow_up_ms,
                    "follow_up_open_delay_ms": self.follow_up_open_delay_ms,
                    "playback_prebuffer_ms": self.playback_prebuffer_ms,
                },
            )
            await on_client_connected_callback(client_id)

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport: WebsocketServerTransport, websocket, *args, **kwargs):
            """Handle client disconnection."""
            self._websockets.discard(websocket)
            client_id = self.extract_client_id(websocket)
            if client_id:
                logger.info(f"🔌 Client {client_id} disconnected")
                if on_client_disconnected_callback:
                    on_client_disconnected_callback(client_id)
        
        # Handle text messages from client (e.g., interrupt messages)
        @transport.event_handler("on_client_message")
        async def on_client_message(transport: WebsocketServerTransport, websocket, message):
            """Handle text messages from WebSocket client."""
            try:
                client_id = self.extract_client_id(websocket)
                
                # Try to parse as JSON
                if isinstance(message, bytes):
                    message = message.decode('utf-8')
                
                try:
                    data = json.loads(message)
                    message_type = data.get("type")
                    
                    if message_type == "interrupt":
                        logger.info(f"🛑 Interrupt received from client {client_id}")
                        
                        # Get OpenAI service for this client
                        openai_service = None
                        if openai_service_getter:
                            openai_service = openai_service_getter(client_id)
                        
                        if openai_service:
                            # Send interrupt event to OpenAI Realtime API
                            # The interrupt event tells OpenAI to stop speaking and listen for user input
                            try:
                                # Try to send interrupt event directly to the service
                                # OpenAI Realtime API expects: {"type": "response.interrupt"}
                                if hasattr(openai_service, 'send_interrupt'):
                                    await openai_service.send_interrupt()
                                    logger.info(f"✅ Interrupt sent to OpenAI service for client {client_id}")
                                elif hasattr(openai_service, 'push_event'):
                                    # Send interrupt event via push_event
                                    await openai_service.push_event({"type": "response.interrupt"})
                                    logger.info(f"✅ Interrupt event sent to OpenAI service for client {client_id}")
                                elif hasattr(openai_service, '_send_event'):
                                    # Try private method if available
                                    await openai_service._send_event({"type": "response.interrupt"})
                                    logger.info(f"✅ Interrupt sent via _send_event to OpenAI service for client {client_id}")
                                else:
                                    # Fallback: log warning
                                    logger.warning(f"⚠️ Could not find method to send interrupt to OpenAI service. Available methods: {[m for m in dir(openai_service) if not m.startswith('__')]}")
                            except Exception as e:
                                logger.error(f"❌ Error sending interrupt to OpenAI service: {e}", exc_info=True)
                        else:
                            logger.warning(f"⚠️ No OpenAI service found for client {client_id}, cannot send interrupt")
                    elif message_type == "start":
                        # va_client sends {"type":"start"} on connect. The
                        # pipeline already streams continuously with server VAD,
                        # so there's nothing to start here — just acknowledge.
                        logger.debug(f"▶️ start from client {client_id}")
                    elif message_type == "ping":
                        # Keepalive. Reply with pong on the same connection.
                        await self._send_json(websocket, {"type": "pong"})
                    else:
                        logger.debug(f"📨 Received message from client {client_id}: {message_type}")
                        
                except json.JSONDecodeError:
                    logger.debug(f"📨 Received non-JSON message from client {client_id}: {message[:100]}")
                    
            except Exception as e:
                logger.error(f"❌ Error handling client message: {e}", exc_info=True)
    
    async def cleanup(self):
        """Cleanup WebSocket handler resources."""
        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")
        
        if self.transport:
            try:
                if hasattr(self.transport, 'stop'):
                    await self.transport.stop()
            except Exception as e:
                logger.warning(f"⚠️ Error stopping transport: {e}")

