"""WebSocket handler for managing WebSocket connections and pipelines."""
import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional, Callable, Awaitable, Dict

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    MetricsFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.audio.utils import create_stream_resampler

from app.device_registry import DeviceConnection, DeviceRegistry, device_id_from_websocket
from app.multi_client_transport import MixedFastAPIWebsocketTransport
from app.raw_audio_serializer import RawAudioSerializer
from app.session_manager import SessionManager
from app.audio_recording_service import AudioRecordingService
from app.phase_emitter import PhaseEmitter
from app.output_lead_buffer import OutputLeadBuffer
from app.transcript_logger import TranscriptLogger
from app.turn_timeline import TurnTimeline
from app.input_replay import InputReplayBuffer
from app.wake_events import WakeAudioCapture, WakeEventStore
from app.device_auth import DeviceAuth
from app.ha_sensors import PUBLISHER
from app.session_controls import controls_for

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


class TimelineMetricsTap(FrameProcessor):
    """Copy pipecat's TTFB metric into the turn timeline (cross-check only)."""

    def __init__(self, timeline_getter, **kwargs):
        super().__init__(**kwargs)
        self._timeline_getter = timeline_getter

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            timeline = self._timeline_getter()
            turn = getattr(timeline, "current", None)
            for data in getattr(frame, "data", None) or []:
                if type(data).__name__ == "TTFBMetricsData" and turn is not None:
                    value = getattr(data, "value", None)
                    if isinstance(value, (int, float)) and value > 0:
                        turn.meta["pipecat_ttfb_ms"] = int(round(value * 1000))
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
    # Proactive refresh: reconnect BEFORE OpenAI's 60-min session cap, but only
    # while the house is genuinely quiet, so the cap practically never lands
    # mid-conversation (where it costs the user a turn).
    REFRESH_AGE_S = 55 * 60   # refresh once the session is this old
    REFRESH_QUIET_S = 60.0    # ... and no mic audio flowed for this long
    REFRESH_CHECK_S = 60.0    # poll cadence of the background check

    def __init__(self, openai_service, emit_idle=None, phase_emitter=None,
                 replay=None, should_replay=None, on_turn_error=None, **kwargs):
        super().__init__(**kwargs)
        self._service = openai_service
        self._controls = controls_for(openai_service)
        # Reconnects hold mic frames and, when the request in progress was not
        # answered yet, replay it into the fresh session (input_replay.py).
        self._replay = replay
        self._should_replay = should_replay or (lambda: False)
        # Called with the error text when a turn ends on a non-fatal error
        # (rate limit, failed response) so the user can be told out loud.
        self._on_turn_error = on_turn_error
        self._emit_idle = emit_idle  # async callable(value:str), this device's send_phase
        # Preferred idle route: PhaseEmitter.force_idle() keeps the emitter's
        # phase state consistent AND suppresses the racing `thinking` from VAD
        # stop events still in flight (observed: a raw broadcast idle was
        # overridden 400 ms later and the device sat in `thinking` with an
        # open mic for 44 s). emit_idle stays as fallback wiring.
        self._phase_emitter = phase_emitter
        self._reconnecting = False
        self._last_attempt = 0.0
        self._last_idle_unstick = 0.0
        # Diagnostics: when the current OpenAI session connected, so we can log its
        # age at a drop (the 60-min cap shows up as ~3600 s) and the reconnect
        # duration (the brief gap the user hears).
        self._connected_at = time.monotonic()
        # Proactive-refresh state. This processor sits right behind
        # transport.input(), so every mic frame passes through it — the cheapest
        # possible "is anyone interacting?" signal (the device only streams the
        # mic during an active turn or the follow-up window).
        self._last_input_audio = time.monotonic()
        self._refresh_task = None
        self._recover_task = None
        self._closed = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._closed:
            # Torn down (e.g. displaced by the same device reconnecting): frames
            # still pass, but a late frame must never restart the refresh loop
            # or act on an error — that resurrected a dead connection's session.
            await self.push_frame(frame, direction)
            return
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._proactive_refresh_loop())
        if isinstance(frame, InputAudioRawFrame):
            # Only kept for the proactive-refresh "is anyone interacting?" check.
            # (Stale-audio clearing is now done at the cut-off source — the device
            # sends {"type":"flush"} when a follow-up window times out — not
            # reactively on mic-resume, which disturbed the VAD and caused garbage.)
            self._last_input_audio = time.monotonic()
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
            # (c) the OpenAI READ side died or ended (network drop / silent
            #     server close). pipecat produces no ErrorFrame for these at
            #     all — SafeRealtimeLLMService (and the GPT-Live service)
            #     wrap the receive loop and report them with this message. Without it the session sat
            #     deaf for hours until the next utterance hit the dead socket.
            reader_dead = "receive loop" in msg
            if send_flood or session_dead or reader_dead:
                now = time.monotonic()
                if now - self._last_attempt >= self.RECONNECT_COOLDOWN_S:
                    self._reconnecting = True
                    self._last_attempt = now
                    # A request still in flight is replayed into the new
                    # session, so keep the device listening instead of idling it.
                    self._recover_task = asyncio.create_task(
                        self._recover(msg, unstick=not self._should_replay())
                    )
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

    @property
    def reconnecting(self) -> bool:
        return self._reconnecting

    async def probe_liveness(self, timeout_s: float = 1.5) -> bool:
        """Positive liveness check: a WebSocket ping must get its pong back.

        Has no effect on the conversation (unlike clearing the input buffer),
        and a half-open socket — the failure mode that produced NO error at
        all — cannot answer it.
        """
        socket = getattr(self._service, "_websocket", None)
        if socket is None:
            return False
        try:
            waiter = await asyncio.wait_for(socket.ping(), timeout_s)
            await asyncio.wait_for(waiter, timeout_s)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"🔌 OpenAI liveness ping failed: {e!r}")
            return False

    async def force_reconnect(self, reason: str, unstick: bool = True) -> None:
        """Positive-liveness reconnect: for wedged (half-open) sockets that
        produce NO ErrorFrames at all — audio streams out, nothing comes back
        (observed live 2026-07-16: wake + speech after an idle gap → zero
        server events, no error, request lost)."""
        now = time.monotonic()
        if self._closed or self._reconnecting or now - self._last_attempt < self.RECONNECT_COOLDOWN_S:
            return
        self._reconnecting = True
        self._last_attempt = now
        self._recover_task = asyncio.create_task(self._recover(reason, unstick=unstick))
        await self._recover_task

    async def _recover(self, reason: str, unstick: bool = True):
        if self._closed:
            self._reconnecting = False
            return
        t0 = time.monotonic()
        age_s = t0 - self._connected_at
        replay = self._replay
        if replay is not None:
            replay.begin_hold()
        released = False
        try:
            logger.warning(
                f"🔌 OpenAI Realtime connection lost after {age_s:.0f}s "
                f"({reason[:90]}) — reconnecting…"
            )
            # Unstick the device first, regardless of how the reconnect goes —
            # unless the request in progress will be replayed, in which case
            # the device must keep its mic open and the user keeps talking.
            if unstick:
                try:
                    await self._go_idle(f"reconnect: {reason[:60]}")
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
            if replay is not None:
                do_replay = bool(self._should_replay())
                pushed = await replay.release(replay=do_replay)
                released = True
                if do_replay:
                    logger.info(f"🔁 replayed the unanswered request into the new session ({pushed} frames)")
        except Exception as e:
            logger.error(f"❌ OpenAI reconnect attempt failed: {e!r}")
        finally:
            if replay is not None and not released:
                try:
                    await replay.release(replay=False)
                except Exception as e:
                    logger.debug(f"replay release after failed reconnect: {e!r}")
            self._reconnecting = False

    async def _proactive_refresh_loop(self):
        """Refresh the OpenAI session BEFORE the 60-min cap, during real idle.

        The cap reconnect is recoverable (~3 s), but when it lands
        mid-conversation that turn hiccups. Refreshing proactively while
        nothing is happening means users practically never meet the cap.
        "Quiet" is double-checked: no assistant response in flight AND no mic
        audio for REFRESH_QUIET_S — so it can never fire during a turn, a
        reply, or an open follow-up window.
        """
        while not self._closed:
            try:
                await asyncio.sleep(self.REFRESH_CHECK_S)
                if self._closed:
                    return
                if self._reconnecting:
                    continue
                now = time.monotonic()
                age = now - self._connected_at
                quiet = now - self._last_input_audio
                busy = self._controls.response_active
                if (age >= self.REFRESH_AGE_S and quiet >= self.REFRESH_QUIET_S
                        and not busy and now - self._last_attempt >= self.RECONNECT_COOLDOWN_S):
                    self._reconnecting = True
                    self._last_attempt = now
                    logger.info(
                        f"🔄 proactive session refresh (session {age/60:.0f} min old, "
                        f"quiet for {quiet:.0f}s) — staying ahead of the 60-min cap"
                    )
                    await self._recover("proactive refresh before the 60-min session cap")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ proactive refresh loop error: {e!r}")

    async def close(self) -> None:
        """Stop background work owned by this pipeline processor."""
        self._closed = True
        tasks = (self._refresh_task, self._recover_task)
        self._refresh_task = None
        self._recover_task = None
        for task in tasks:
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"connection recovery task shutdown: {e!r}")

    async def _go_idle(self, reason: str) -> None:
        """Put the device in idle for a dead turn — via PhaseEmitter when wired."""
        if self._phase_emitter is not None:
            await self._phase_emitter.force_idle(reason)
        elif self._emit_idle is not None:
            await self._emit_idle("idle")

    async def _unstick_idle(self, reason: str):
        """Emit `idle` to the device after a turn-ending error (e.g. rate limit).

        The session is still alive (no reconnect needed) — we just nudge the
        device out of its stuck `thinking` blink so the user can retry.
        """
        if self._closed:
            return
        try:
            logger.warning(f"⚠️ turn ended on error, emitting idle to unstick device ({reason[:90]})")
            await self._go_idle(f"turn ended on error: {reason[:60]}")
        except Exception as e:
            logger.warning(f"⚠️ could not emit idle after turn-ending error: {e!r}")
        if self._on_turn_error is not None:
            try:
                await self._on_turn_error(reason)
            except Exception as e:
                logger.debug(f"turn-error observer failed: {e!r}")


class WebSocketHandler:
    """Handles WebSocket transport initialization, pipeline building, and event management."""

    WEDGE_TIMEOUT_S = 12.0
    # A wake opens a liveness check that runs during the chime + mic-open
    # delay, so a dead socket is replaced before the user starts talking.
    WAKE_PROBE_TIMEOUT_S = 1.5
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        session_manager: Optional[SessionManager] = None,
        audio_recording_service: Optional[AudioRecordingService] = None,
        follow_up_ms: int = 0,
        follow_up_open_delay_ms: int = 700,
        wake_open_delay_ms: int = 700,
        playback_prebuffer_ms: int = 0,
        output_lead_buffer_ms: int = 0,
        wake_events: Optional[WakeEventStore] = None,
        device_auth: Optional[DeviceAuth] = None,
        trigger_capture: bool = False,
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
            wake_open_delay_ms: How long (ms) the device waits after the wake
                chime before opening the mic, so the chime's hardware tail can't
                leak into the fresh mic as a ghost turn. Sent in `hello`.
        """
        self.host = host
        self.port = port
        self.session_manager = session_manager
        self.audio_recording_service = audio_recording_service
        self.follow_up_ms = max(0, int(follow_up_ms))
        self.follow_up_open_delay_ms = max(0, int(follow_up_open_delay_ms))
        self.wake_open_delay_ms = max(0, int(wake_open_delay_ms))
        self.playback_prebuffer_ms = max(0, int(playback_prebuffer_ms))
        self.output_lead_buffer_ms = max(0, int(output_lead_buffer_ms))
        # Wake metadata, false-wake labels and (opt-in) captures, shared by
        # every connection of this add-on instance.
        self.wake_events = wake_events or WakeEventStore()
        self.device_auth = device_auth or DeviceAuth()
        # Ask firmware for the pre-wake trigger snippet only when audio
        # capture is on AND the operator opted in to trigger capture.
        self.trigger_capture = bool(trigger_capture)
        # Called with (connection, kind) to speak a short error to the user
        # (set by main.py; see SpokenPrompts).
        self.turn_error_speaker = None
        self._background: set = set()

        # Per-device connections. Everything that used to be a singleton here —
        # transport, serializer, OpenAI session, pipeline, task — now lives on a
        # DeviceConnection, because sharing one of each is precisely what forced
        # a second device to displace the first.
        self.devices = DeviceRegistry()
        # Builds a fresh OpenAI service for a connection. Set by main.py, which
        # owns the model/tool configuration. Takes the DeviceConnection so
        # per-device tools (e.g. disconnect_client) can bind to that device's
        # transport rather than to a process-wide one.
        self.openai_service_factory: Optional[Callable[[DeviceConnection], Awaitable[Any]]] = None
        # Which device's audio the (single-file) recorder is following.
        self._recording_owner: Optional[str] = None
        # Voice enrollment remains single-user, but is explicitly targeted to
        # the connection that starts it.
        self.enrollment_recorder = None
        self.enrollment_conductor = None
    
    def create_transport(
        self, websocket, serializer: RawAudioSerializer
    ) -> MixedFastAPIWebsocketTransport:
        """Create a transport for one accepted connection.

        Args:
            websocket: The accepted FastAPI/starlette WebSocket.
            serializer: This connection's serializer.

        Returns:
            A transport bound to that one connection.
        """
        return MixedFastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                serializer=serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
                audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
            ),
        )


    def build_pipeline(
        self,
        connection: DeviceConnection,
        activity_callback: Optional[Callable[[], None]] = None
    ) -> tuple[Pipeline, PipelineRunner, PipelineTask]:
        """Build the pipeline from one connection's resources.

        Args:
            connection: The device's connection, with its transport,
                serializer and OpenAI service already set.
            activity_callback: Optional callback for session activity tracking

        Returns:
            Tuple of (Pipeline, PipelineRunner, PipelineTask)
        """
        transport = connection.transport
        openai_service = connection.openai_service
        client_id = connection.device_id
        serializer = connection.serializer
        # Phases go to this device only. They used to be broadcast, so one
        # room's listening/thinking/replying drove every device's LEDs.
        send_phase = connection.send_phase
        logger.info(f"🔗 Building pipeline for client: {client_id}")
        
        if openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")
        # Provider-neutral view of the model session (app/session_controls.py):
        # the device-driven pokes below (stop, connect, flush, verdicts) never
        # touch a wire protocol directly.
        controls = controls_for(openai_service)
        
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
        # Built early so ConnectionRecovery can route its unstick/reconnect
        # idle through PhaseEmitter.force_idle() (consistent phase state +
        # racing-`thinking` suppression); it is APPENDED near the end of the
        # pipeline below, before transport.output().
        phase_emitter = PhaseEmitter(
            send_phase=send_phase, liveness=connection.turn_liveness
        )
        connection.phase_emitter = phase_emitter

        timeline = connection.turn_timeline
        connection.replay = InputReplayBuffer()

        def _should_replay() -> bool:
            # Replay only a request that is still in progress and unanswered.
            # Once a tool has started, its side effect may have happened even
            # if its result never reached the model. Replaying the utterance in
            # a replacement session could execute it twice.
            if getattr(openai_service, "unsafe_to_replay_input", False):
                return False
            turn = getattr(timeline, "current", None)
            return bool(turn and not turn.done and "first_model_audio" not in turn.stamps)

        async def _on_turn_error(reason: str) -> None:
            speaker = self.turn_error_speaker
            if speaker is not None:
                await speaker(connection, reason)

        connection.recovery = ConnectionRecovery(
            openai_service=openai_service, emit_idle=send_phase,
            phase_emitter=connection.phase_emitter,
            replay=connection.replay, should_replay=_should_replay,
            on_turn_error=_on_turn_error,
        )
        pipeline_components = [
            transport.input(),
            # Watch for OpenAI connection-death ErrorFrames (they travel upstream
            # to the task source, so place this upstream of the service) and
            # reconnect in place. Without it a 1011/1001 drop bricks the session.
            connection.recovery,
            InputResampler(out_rate=PIPELINE_SAMPLE_RATE),
            # Keeps the request in progress so a reconnect can replay it.
            connection.replay,
            input_activity_tracker,
        ]
        
        # Add input audio recorder to capture ONLY InputAudioRawFrame.
        # AudioRecordingService writes ONE session file, so letting several
        # devices share it would interleave their audio into unusable
        # recordings. Exactly one connection owns it at a time — see
        # _claim_recording.
        connection.records_audio = self._claim_recording(client_id)
        input_recorder = (
            self.audio_recording_service.get_input_recorder()
            if (self.audio_recording_service and connection.records_audio) else None
        )
        if input_recorder:
            pipeline_components.append(input_recorder)
        
        # Continue with rest of pipeline, with transcript-logging taps. The
        # assistant reply text (TTSTextFrame) flows DOWNSTREAM out of the LLM
        # while the user's TranscriptionFrame is pushed UPSTREAM (so the user
        # aggregator can consume it) — opposite directions, so they need taps on
        # opposite sides of the service (see transcript_logger.py): "user" before
        # the LLM, "assistant" after it.
        if context_aggregator:
            pipeline_components.extend([
                context_aggregator.user(),
                TranscriptLogger(capture="user"),
                openai_service,
                TranscriptLogger(capture="assistant"),
                context_aggregator.assistant(),
            ])
        else:
            pipeline_components.extend([
                TranscriptLogger(capture="user"),
                openai_service,
                TranscriptLogger(capture="assistant"),
            ])

        pipeline_components.append(output_activity_tracker)
        pipeline_components.append(TimelineMetricsTap(lambda: connection.turn_timeline))

        # Emit va_client phase messages (listening/thinking/replying/idle) to
        # the device, derived from Pipecat speaking frames as they pass
        # downstream. Placed before transport.output() so it sees both the
        # user (UserStarted/Stopped) and bot (BotStarted/Stopped) frames.
        # (Constructed above, before ConnectionRecovery.)
        pipeline_components.append(connection.phase_emitter)

        # Add output audio recorder to capture ONLY OutputAudioRawFrame
        output_recorder = (
            self.audio_recording_service.get_output_recorder()
            if (self.audio_recording_service and connection.records_audio) else None
        )
        if output_recorder:
            pipeline_components.append(output_recorder)

        # Prime the device's playout buffer against the resampler cold-start
        # starve: hold the first LEAD_MS of each reply's audio and burst it so the
        # device gets a buffer lead even on a cold turn. Placed LAST, right before
        # transport.output(), so it acts on the final audio stream the device
        # receives (the recorder above still captures the true, un-delayed frames).
        # Pass-through when OUTPUT_LEAD_BUFFER_MS=0.
        pipeline_components.append(OutputLeadBuffer(lead_ms=self.output_lead_buffer_ms))

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
        # handle_sigint=False is REQUIRED now that there is a runner per
        # connection: PipelineRunner installs a process-wide SIGINT handler by
        # default, so each new device would clobber the previous one's and a
        # disconnect would tear down shutdown handling for the whole add-on.
        # The process owns its own signal handling in main().
        runner = PipelineRunner(handle_sigint=False)
        # Metrics on: pipecat's TTFB/processing metrics feed the turn timeline
        # (TimelineMetricsTap) as a cross-check of our own stage stamps.
        task = PipelineTask(
            pipeline,
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
            idle_timeout_secs=None,
            cancel_on_idle_timeout=False,
        )

        logger.info(f"✅ Pipeline built for {client_id}")

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
        # FOLLOW-UP-WINDOW STOP (the "stop heard as a question" bug). During the
        # post-reply follow-up window the device mic is OPEN and streaming, so by
        # the time the device's local wake-word detects "stop" and sends us the
        # interrupt, the stop word's audio is ALREADY in OpenAI's input buffer.
        # Left alone, the server VAD commits it as a user turn and — with
        # create_response=true — the model literally ANSWERS the word "stop"
        # ("Ik hou me stil…"). The device's local detection must therefore be
        # authoritative on the cloud side too, in two layers:
        #   1) input_audio_buffer.clear discards the not-yet-committed stop-word
        #      audio (the device closed its own mic gate in the same instant),
        #      so in the common case no turn is created at all;
        #   2) if the server VAD committed BEFORE our clear landed (tight race),
        #      OpenAI creates a response moments later anyway — so any assistant
        #      conversation item that appears within INTERRUPT_KILL_WINDOW_S of
        #      a device interrupt is cancelled on arrival (handler below). A
        #      legitimate next turn cannot fall inside that window: after a stop
        #      the mic is closed, and a fresh wake-word turn needs the chime +
        #      speech + VAD end-of-turn (> 2 s) before a response is created.
        _interrupt_kill_until = {"t": 0.0}
        INTERRUPT_KILL_WINDOW_S = 1.5
        # A device "stop" must cancel the NEXT assistant response too, not only
        # the one currently playing. After a stop, the only responses OpenAI can
        # still produce before the user speaks again are unwanted:
        #   - the cancelled reply's already-generated tail;
        #   - a slow tool's answer (web search ~2-4 s) the user stopped mid-run,
        #     created on the tool result OUTSIDE the 1.5 s time-window;
        #   - most common: OpenAI's STT hearing the user's spoken "stop" as a
        #     turn and the model REPLYING to it ("Okay, I'll stop"), which lands
        #     ~1.8 s later — just outside 1.5 s (observed 2026-06-14 22:51: the
        #     device flashed red but a fresh "I'll be quiet" reply played, so the
        #     user had to say stop twice).
        # The time-window alone misses the >1.5 s cases. This flag, armed on
        # EVERY device interrupt, makes _kill_racing_response cancel that one
        # next response regardless of timing. It is consumed when used and
        # cleared at the next genuine turn boundary (real speech via
        # on_real_speech, and {"type":"wake"}) — and a legitimate next turn needs
        # the user to actually speak — so it can never cancel a real turn.
        _kill_next_response = {"v": False}

        async def _on_device_interrupt():
            turn = timeline.current if timeline is not None else None
            if turn is not None and not turn.done:
                if "bot_started" in turn.stamps or "first_audio_sent" in turn.stamps:
                    timeline.finish("interrupted")
                elif "speech_started" not in turn.stamps:
                    timeline.finish("no_speech")
                else:
                    timeline.finish("cancelled")
            _interrupt_kill_until["t"] = time.monotonic() + INTERRUPT_KILL_WINDOW_S
            # Arm the next-response kill on EVERY stop (see the flag comment):
            # the 1.5 s time-window alone misses responses that land later —
            # OpenAI replying to the spoken "stop", or a slow tool's answer.
            _kill_next_response["v"] = True
            try:
                if await controls.discard_pending_input("device interrupt"):
                    logger.info("🛑 device interrupt → input_audio_buffer.clear sent (drop in-flight user audio)")
                else:
                    logger.info("🛑 device interrupt → no input buffer to clear on this runtime")
            except Exception as e:
                logger.info(f"🛑 device interrupt → input_audio_buffer.clear no-op ({e!r})")
            try:
                if await controls.cancel_active_response("device interrupt"):
                    logger.info("🛑 device interrupt → response.cancel sent (response was still active)")
                else:
                    logger.info("🛑 device interrupt → no active response to cancel (device already silenced)")
            except Exception as e:
                logger.info(f"🛑 device interrupt → response.cancel no-op ({e!r})")

        async def _kill_racing_response():
            # Runs whenever a NEW assistant response starts; only one right
            # after a device interrupt is the racing response to the stop
            # word the user just cancelled.
            within_window = time.monotonic() < _interrupt_kill_until["t"]
            kill_armed = _kill_next_response["v"]
            if not within_window and not kill_armed:
                return
            # Consume the flag: this assistant item is the unwanted response the
            # user's stop pre-empted — a stop-acknowledgement ("Okay, I'll stop"),
            # a stopped tool's answer, or the cancelled reply's tail.
            _kill_next_response["v"] = False
            try:
                await controls.cancel_active_response("post-stop racing response", force=True)
                logger.info(
                    "🛑 response raced in right after a device interrupt → "
                    "response.cancel (post-stop)"
                )
            except Exception as e:
                logger.info(f"🛑 post-interrupt racing-response cancel no-op ({e!r})")

        controls.on_assistant_response_started(_kill_racing_response)

        async def _on_device_session_start():
            # va_client sends {"type":"start"} once per WebSocket CONNECTION
            # (on connect) — NOT per wake. A reconnect mid-utterance (wifi
            # blip, backend restart with session reuse) can leave half an
            # utterance in OpenAI's input buffer; start every (re)connection
            # with a clean one. The per-WAKE/follow-up stale-buffer case is
            # covered by the device's {"type":"flush"} on follow-up timeout.
            try:
                if await controls.discard_pending_input("device (re)connected"):
                    logger.info("🎬 device (re)connected → input_audio_buffer.clear (clean start)")
            except Exception as e:
                logger.debug(f"🎬 connect-time input clear no-op ({e!r})")

        async def _on_device_mic_flush():
            # The device sends {"type":"flush"} when a follow-up window times out
            # mid-stream. Drop any uncommitted partial utterance NOW, at the
            # cut-off, so a later wake can't "complete" it into a stale answer.
            # This replaced the reactive clear-on-mic-resume, which fired on
            # every wake and disturbed the server VAD → spurious garbage commits.
            # Also a turn boundary for the dangling-VAD guard: the follow-up
            # closed without speech, so any later server-VAD stop is dangling.
            phase_emitter.note_wake()
            try:
                if await controls.discard_pending_input("follow-up cut-off"):
                    logger.info("🧽 follow-up cut-off → input_audio_buffer.clear (drop partial utterance)")
            except Exception as e:
                logger.debug(f"🧽 mic-flush input clear no-op ({e!r})")

        async def _on_device_wake(meta=None):
            meta = dict(meta or {})
            source = meta.get("src") or "wake_word"
            turn = timeline.begin(turn_id=meta.get("turn"), source=source, meta=meta)
            connection.replay.clear()
            event = self.wake_events.record_wake(
                connection.device_id,
                turn.turn_id,
                source=source,
                model=str(meta.get("model", "")),
                model_sha=str(meta.get("model_sha", "")),
                cutoff=meta.get("cutoff"),
                window=meta.get("window"),
                tier=str(meta.get("tier", "")),
            )
            if connection.wake_capture is not None:
                connection.wake_capture.start(event)
            # Sensor publishing is fire-and-forget: the receive loop that
            # delivered this wake also delivers the user's next audio frames.
            await PUBLISHER.wake(connection.device_id)
            if meta.get("model"):
                PUBLISHER.wake_model(connection.device_id, meta)
            self._spawn(self._wake_liveness(connection))
            self._spawn(self._wedge_check(connection, phase_emitter, time.monotonic()))
            # va_client sends {"type":"wake"} on every wake (start_session). Mark
            # the turn boundary for the dangling-VAD guard (A): until the user
            # actually speaks, a server-VAD end-of-turn is a stale pre-wake
            # segment closing late → suppress its thinking + cancel its garbage
            # response (handled in PhaseEmitter via the kill-window callbacks).
            phase_emitter.note_wake()
            # New turn boundary: drop any pending post-tool kill so it can't
            # leak onto this fresh turn's response.
            _kill_next_response["v"] = False

        # Wire the dangling-VAD guard's kill-window into the PhaseEmitter. It
        # reuses the SAME _interrupt_kill_until + _kill_racing_response machinery
        # as the device stop: on a dangling stop, arm it so the auto-created
        # garbage response is cancelled; on a real UserStartedSpeaking, clear it
        # so a genuine new turn's response is never cancelled.
        def _clear_kill_window():
            # Real user speech = a genuine new turn — disarm BOTH the time
            # window and the post-tool flag so neither can cancel it.
            _interrupt_kill_until["t"] = 0.0
            _kill_next_response["v"] = False

        phase_emitter.set_kill_window_handlers(
            on_dangling=lambda: _interrupt_kill_until.__setitem__(
                "t", time.monotonic() + INTERRUPT_KILL_WINDOW_S),
            on_real_speech=_clear_kill_window,
        )

        def _on_turn_end(outcome: str) -> None:
            turn = timeline.current
            if turn is None or turn.done:
                return
            timeline.finish(outcome)
            self.wake_events.set_outcome(connection.device_id, turn.turn_id.split(".f")[0], outcome)

        phase_emitter.on_turn_end = _on_turn_end
        phase_emitter.on_bot_started = lambda: timeline.mark("bot_started")
        phase_emitter.on_bot_stopped = lambda: timeline.mark("bot_stopped", overwrite=True)

        def _on_silent_response() -> None:
            turn = timeline.current
            if turn is not None and not turn.done:
                self.wake_events.mark_candidate(
                    connection.device_id, turn.turn_id.split(".f")[0], "admission_silence"
                )
            phase_emitter.end_silent_turn()

        if hasattr(openai_service, "turn_timeline"):
            openai_service.turn_timeline = timeline
        openai_service.on_silent_response = _on_silent_response
        # The request was answered: a later reconnect must not replay it.
        openai_service.on_response_audio = connection.replay.clear

        if serializer is not None:
            serializer.set_interrupt_handler(_on_device_interrupt)
            serializer.set_session_start_handler(_on_device_session_start)
            serializer.set_mic_flush_handler(_on_device_mic_flush)
            serializer.set_wake_handler(_on_device_wake)

            # Speaker context v1 (fork): per-wake voice-type verdict → injected
            # as a system conversation item. Out-of-band w.r.t. the audio path;
            # it lands ~2.5 s after the wake, so the FIRST reply of a turn may
            # not have it yet — follow-ups and later turns do. Gating of
            # speaker-restricted tools does NOT depend on this injection (see
            # SafeRealtimeLLMService.register_function in main.py).
            if connection.speaker_probe is not None and connection.speaker_probe.enabled:
                from .speaker_context import verdict_text

                async def _on_speaker_verdict(label, name, f0):
                    try:
                        await controls.inject_context(
                            verdict_text(connection.speaker_probe, label, name, f0)
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ speaker verdict injection failed: {e!r}")

                connection.speaker_probe.on_verdict = _on_speaker_verdict
                serializer.set_speaker_probe(connection.speaker_probe)

            if self.enrollment_recorder is not None:
                serializer.set_enrollment_recorder(self.enrollment_recorder)
            # False-wake feedback (fast button cancel, double press, or a
            # queued flag sent after a reconnect) labels exactly one wake on
            # THIS device: the named turn, or the newest wake within the
            # bounded flag window. Never another room's capture.
            async def _on_false_flag(method, turn_id="", age_ms=0):
                event = self.wake_events.flag_false_wake(
                    connection.device_id,
                    method,
                    turn_id=turn_id or None,
                    age_s=(age_ms or 0) / 1000.0,
                    reply_audio_before_flag=serializer._reply_audio_since_wake,
                )
                if event is None:
                    return
                timeline.set_outcome(event.turn_id, "false_wake")
                await PUBLISHER.false_wake(connection.device_id)
            serializer.set_button_cancel_handler(_on_false_flag)

            async def _on_first_audio():
                timeline.mark("first_audio_frame")
                await connection.send_json({"type": "ack"})
            serializer.set_first_audio_handler(_on_first_audio)

            async def _on_turn_metrics(turn_id, metrics):
                timeline.merge_device_metrics(turn_id, metrics)
            serializer.set_turn_metrics_handler(_on_turn_metrics)

            async def _on_trigger_audio(turn_id, pcm, rate):
                event = self.wake_events.find(connection.device_id, turn_id)
                if event is None:
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.wake_events.save_trigger, event, pcm, rate)
            serializer.set_trigger_audio_handler(_on_trigger_audio)

            async def _on_shadow_detection(model):
                self.wake_events.record_shadow(connection.device_id, model)
            serializer.set_shadow_detection_handler(_on_shadow_detection)

            serializer.set_output_audio_handler(lambda: timeline.mark("first_audio_sent"))

            if self.enrollment_conductor is not None:
                async def _on_device_enroll_stopped():
                    if self.enrollment_conductor.device_id == connection.device_id:
                        await self.enrollment_conductor.stop()
                serializer.set_enroll_stopped_handler(_on_device_enroll_stopped)

        return pipeline, runner, task

    def _spawn(self, coro) -> None:
        """Run a background coroutine, keeping a reference until it finishes."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _wake_liveness(self, connection: DeviceConnection) -> None:
        """Check the OpenAI socket while the wake chime plays.

        A half-open socket used to be discovered only 12 s after the wake,
        with the user's request already lost. Pinging during the chime and the
        mic-open delay replaces a dead socket before speech arrives, and the
        replay buffer covers any speech that still raced the reconnect.
        """
        recovery = connection.recovery
        if recovery is None or recovery.reconnecting:
            return
        if await recovery.probe_liveness(self.WAKE_PROBE_TIMEOUT_S):
            return
        if connection.recovery is not recovery:
            return
        logger.warning("🔌 OpenAI socket did not answer a ping at wake — reconnecting before the request")
        await recovery.force_reconnect("liveness: no pong at wake", unstick=False)

    async def _wedge_check(
        self, connection: DeviceConnection, phase_emitter: PhaseEmitter, wake_mono: float
    ) -> None:
        """Close a wake that heard no speech; reconnect only a socket that is dead.

        No server VAD activity WEDGE_TIMEOUT_S after a wake used to always mean
        a reconnect — so every silent or false wake made the next turn cold.
        Now a ping decides: a live socket just ends the turn (the device mic
        closes, nothing is reconnected); a dead one is replaced.
        """
        await asyncio.sleep(self.WEDGE_TIMEOUT_S)
        if getattr(phase_emitter, "last_vad_mono", 0.0) >= wake_mono:
            return
        recovery = connection.recovery
        if recovery is None:
            return
        if await recovery.probe_liveness():
            if connection.recovery is not recovery:
                return
            logger.info(
                "🤫 no speech %.0fs after wake; OpenAI socket is alive — closing the turn "
                "without reconnecting", self.WEDGE_TIMEOUT_S
            )
            timeline = connection.turn_timeline
            if timeline is not None and timeline.current is not None and not timeline.current.done:
                timeline.finish("no_speech")
            await phase_emitter.force_idle("no speech after wake")
            return
        logger.warning(
            "🧟 no server VAD activity %.0fs after wake and no pong — "
            "half-open OpenAI socket, reconnecting", self.WEDGE_TIMEOUT_S
        )
        await recovery.force_reconnect("wedge: silent after wake")
    
    # ------------------------------------------------------------------
    # Device addressing
    #
    # Announce, timers and enrollment all speak "to the device". With more
    # than one connected that question has to be answered explicitly, so
    # these take an optional device id and fall back to the most recently
    # active device.
    # ------------------------------------------------------------------

    def resolve_device(self, device_id: Optional[str] = None) -> Optional[DeviceConnection]:
        """Pick the device a single-device feature should act on.

        Args:
            device_id: An explicit target, or None for the most recently
                active device.

        Returns:
            The connection, or None if there is no match.
        """
        return self.devices.resolve(device_id)

    async def send_json_to(self, obj: dict, device_id: Optional[str] = None) -> bool:
        """Send one JSON control frame to a single device.

        Args:
            obj: The object to serialize.
            device_id: Target device, or None for the most recently active.

        Returns:
            True if a device took it.
        """
        connection = self.resolve_device(device_id)
        if connection is None:
            logger.warning(f"⚠️ no device to send {obj.get('type')} to (target={device_id or 'last-active'})")
            return False
        return await connection.send_json(obj)

    async def send_bytes_to(self, data: bytes, device_id: Optional[str] = None) -> bool:
        """Send raw 24 kHz mono PCM16 to a single device.

        The device treats every BINARY frame as reply audio, so this pushes
        sound to one speaker outside any OpenAI response — used by the
        enrollment conductor's guidance prompts and by announcements.

        Args:
            data: PCM16 audio.
            device_id: Target device, or None for the most recently active.

        Returns:
            True if a device took it.
        """
        connection = self.resolve_device(device_id)
        if connection is None:
            logger.warning("⚠️ no device to send audio to")
            return False
        transport = connection.transport
        client = getattr(transport, "client", None) if transport else None
        if client is None:
            return False
        try:
            await client.send(data)
            return True
        except Exception as e:
            logger.warning(f"⚠️ send_bytes_to {connection.device_id} failed: {e!r}")
            return False

    async def broadcast_json(self, obj: dict) -> int:
        """Send a JSON object to every connected device.

        Args:
            obj: The object to serialize.

        Returns:
            How many devices took it.
        """
        return await self.devices.broadcast_json(obj)

    def serializer_for(self, device_id: Optional[str] = None) -> Optional[RawAudioSerializer]:
        """The serializer of one device, for inbound-audio suppression.

        Args:
            device_id: Target device, or None for the most recently active.

        Returns:
            That device's serializer, or None.
        """
        connection = self.resolve_device(device_id)
        return connection.serializer if connection else None

    # ------------------------------------------------------------------
    # Audio recording ownership
    #
    # AudioRecordingService writes a single session file, so only one
    # connection may feed it or the recordings interleave into nonsense.
    # ------------------------------------------------------------------

    def _claim_recording(self, device_id: str) -> bool:
        """Try to become the device whose audio is recorded.

        Args:
            device_id: The connecting device.

        Returns:
            True if this device now owns recording.
        """
        if not self.audio_recording_service:
            return False
        if self._recording_owner in (None, device_id):
            newly_claimed = self._recording_owner is None
            self._recording_owner = device_id
            if newly_claimed:
                self.audio_recording_service.start_new_session(device_id)
            return True
        logger.info(
            f"🎙️ audio recording is already following {self._recording_owner}; "
            f"not recording {device_id}"
        )
        return False

    def _release_recording(self, device_id: str) -> None:
        """Give up recording ownership when that device disconnects.

        Args:
            device_id: The departing device.
        """
        if self._recording_owner == device_id:
            self.audio_recording_service.stop_recording()
            self._recording_owner = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def hello_payload(self) -> dict:
        """The handshake the device expects immediately after connecting.

        follow_up_ms tells the device how long to hold the mic open after a
        reply; the delays cover the speaker's hardware tail so it cannot leak
        back into a freshly opened mic. Sent on every connect so an add-on
        config change takes effect on reconnect.

        Returns:
            The `hello` object.
        """
        return {
            "type": "hello",
            "proto": 2,
            "audio_out": "pcm",
            "follow_up_ms": self.follow_up_ms,
            "follow_up_open_delay_ms": self.follow_up_open_delay_ms,
            "wake_open_delay_ms": self.wake_open_delay_ms,
            "playback_prebuffer_ms": self.playback_prebuffer_ms,
            # 1 = the operator opted in to storing the pre-wake trigger snippet
            # for false-wake review; firmware also requires its own switch.
            "trigger_capture": 1 if (self.trigger_capture and self.wake_events.storing_audio()) else 0,
        }

    async def serve_connection(
        self,
        websocket,
        on_client_connected: Optional[Callable[[str], Awaitable[None]]] = None,
        on_client_disconnected: Optional[Callable[[DeviceConnection], None]] = None,
        activity_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """Own one device connection from accept to close.

        Builds this device its own serializer, transport, OpenAI session and
        pipeline, then runs that pipeline until the socket closes. Several of
        these run concurrently — one per device — which is the whole point:
        the old single transport closed the incumbent socket on every new
        connection, so two devices could only take turns.

        Args:
            websocket: The FastAPI/starlette WebSocket, not yet accepted.
            on_client_connected: Optional async callback(device_id).
            on_client_disconnected: Optional callback(connection).
            activity_callback: Optional session-activity callback.
        """
        decision = self.device_auth.check(websocket)
        if not decision.allowed:
            logger.warning(f"⛔ device connection rejected: {decision.reason}")
            # Closing before accept() makes the server refuse the handshake.
            await websocket.close(code=1008)
            return
        await websocket.accept()
        device_id = device_id_from_websocket(websocket)
        logger.info(
            f"🔗 device {device_id} connected ({len(self.devices) + 1} total)"
            + ("" if decision.authenticated else f" [{decision.reason}]")
        )

        serializer = RawAudioSerializer(device_id)
        connection = DeviceConnection(
            device_id=device_id, websocket=websocket, serializer=serializer
        )
        connection.transport = self.create_transport(websocket, serializer)
        connection.turn_timeline = TurnTimeline(device_id, publish=PUBLISHER.latency)
        connection.wake_capture = WakeAudioCapture(self.wake_events)
        serializer.add_audio_tap(connection.wake_capture)

        # Keepalive. The device sends {"type":"ping"} and waits for a pong;
        # the previous implementation registered this on an event pipecat
        # never fires, so no pong was ever sent.
        async def _on_ping():
            await connection.send_json({"type": "pong"})

        serializer.set_ping_handler(_on_ping)

        # Any wake marks this device as the one in use, so announcements and
        # timers land in the room the user is actually talking to.
        serializer.set_activity_handler(connection.touch)

        displaced = None
        registered = False
        try:
            if self.openai_service_factory is None:
                raise RuntimeError("openai_service_factory must be set before serving connections")
            connection.openai_service = await self.openai_service_factory(connection)

            pipeline, runner, task = self.build_pipeline(connection, activity_callback)
            connection.pipeline = pipeline
            connection.runner = runner
            connection.task = task

            @connection.transport.event_handler("on_client_disconnected")
            async def _stop_disconnected_task(_transport, _websocket):
                # Pipecat's FastAPI input transport signals this event but does
                # not stop PipelineRunner itself. Cancel only this device's task
                # so serve_connection reaches its per-device cleanup.
                #
                # Pipecat invokes handlers as handler(emitter, *event_args), so
                # this takes the transport as well as the websocket. Getting the
                # arity wrong raises before the cancel, leaving the old pipeline
                # running: its recorder then pushes into torn-down processors
                # ("no attribute _FrameProcessor__input_queue") until
                # push_error_frame recurses past the stack limit.
                await task.cancel()

            displaced = await self.devices.add(connection)
            registered = True
            await connection.send_json(self.hello_payload())

            if displaced is not None:
                await self._teardown(displaced)
                displaced = None

            if on_client_connected:
                await on_client_connected(device_id)

            # Blocks until the device disconnects (or the pipeline ends).
            await runner.run(task)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ connection for {device_id} failed: {e!r}", exc_info=True)
        finally:
            removed = await self.devices.remove(connection)
            if not removed and connection.openai_service is not None and self.session_manager:
                self.session_manager.handle_client_disconnect(
                    connection.device_id, connection.openai_service
                )
            if removed:
                self._release_recording(device_id)
                if (
                    self.enrollment_conductor is not None
                    and self.enrollment_conductor.device_id == device_id
                ):
                    await self.enrollment_conductor.stop()
            elif not registered and connection.records_audio:
                self._release_recording(device_id)
            if removed and on_client_disconnected:
                try:
                    on_client_disconnected(connection)
                except Exception as e:
                    logger.warning(f"⚠️ disconnect callback for {device_id} failed: {e!r}")
            await self._teardown(connection)
            logger.info(f"🔌 device {device_id} disconnected ({len(self.devices)} remaining)")

    async def _teardown(self, connection: DeviceConnection) -> None:
        """Release one connection's pipeline and OpenAI session.

        Args:
            connection: The connection to tear down.
        """
        # Stop this connection's own background work FIRST. Cancelling the
        # pipeline can block for pipecat's whole cancel timeout (20 s) when a
        # CancelFrame cannot reach its end; meanwhile a live recovery or phase
        # emitter would still act on the dead connection (refresh its OpenAI
        # session, send phases to its closed socket).
        recovery = connection.recovery
        if recovery is not None:
            await recovery.close()
        phase_emitter = connection.phase_emitter
        if phase_emitter is not None:
            await phase_emitter.close()
        if connection.task is not None:
            try:
                await connection.task.cancel()
            except Exception as e:
                logger.debug(f"task cancel for {connection.device_id}: {e!r}")
        timeline = connection.turn_timeline
        if timeline is not None:
            if timeline.current is not None and not timeline.current.done:
                timeline.finish("disconnected")
            timeline.close()
        if connection.wake_capture is not None:
            connection.wake_capture.finalize()
        service = connection.openai_service
        if service is not None:
            for method in ("disconnect", "_disconnect", "cleanup"):
                closer = getattr(service, method, None)
                if closer is None:
                    continue
                try:
                    await closer()
                    break
                except Exception as e:
                    logger.debug(f"{method}() for {connection.device_id}: {e!r}")
        connection.task = None
        connection.runner = None
        connection.pipeline = None
        connection.openai_service = None
        connection.recovery = None
        connection.phase_emitter = None

    async def cleanup(self):
        """Tear down every connection at shutdown."""
        for connection in self.devices:
            await self._teardown(connection)
