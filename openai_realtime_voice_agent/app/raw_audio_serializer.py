"""Simple serializer for raw binary PCM audio frames."""
import json
import logging
import os
import time
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, Frame
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType

logger = logging.getLogger(__name__)


class RawAudioSerializer(FrameSerializer):
    """Serializer that treats all binary messages as raw PCM audio.

    Text frames (JSON control messages such as the va_client phase protocol)
    are NOT handled here — they are sent/received directly on the websocket by
    the WebSocketHandler so they go out as TEXT frames, not binary.
    """

    def __init__(self, input_sample_rate: int | None = None):
        # The Home Assistant Voice PE firmware (va_client) streams 16 kHz PCM16
        # mono from the XMOS mic. We tag incoming frames with the device's true
        # rate. NOTE: pipecat 0.0.97's input transport does NOT resample — the
        # InputResampler processor in websocket_handler.py upsamples 16k->24k
        # before the audio reaches OpenAI (which requires 24 kHz pcm16 input).
        if input_sample_rate is None:
            input_sample_rate = int(os.environ.get("DEVICE_INPUT_SAMPLE_RATE", "16000"))
        self._input_sample_rate = input_sample_rate
        # Async callback invoked when the device sends {"type":"interrupt"} (the
        # "stop" wake word). Set by WebSocketHandler.build_pipeline once it has
        # the OpenAI service. We deliberately do NOT emit a pipecat
        # InterruptionFrame for this: pipecat's OWN VAD already emits
        # InterruptionFrame (StartInterruptionFrame) on every user-start-speaking,
        # so reacting to that class would cancel the response on ANY speech.
        self._on_interrupt = None
        # Async callback invoked when the device sends {"type":"start"}. NB the
        # va_client sends this once per WebSocket CONNECTION (on connect), NOT
        # per wake-word session. Used to start every (re)connection with a
        # clean OpenAI input buffer — a reconnect mid-utterance leaves half an
        # utterance behind, which session reuse would replay ahead of the next
        # turn. The per-WAKE stale-buffer case (follow-up window cutting a
        # sentence; observed live 2026-06-12) is covered separately by
        # ConnectionRecovery's mic-resume gap detector in websocket_handler.py.
        self._on_session_start = None
        # Async callback for {"type":"flush"} — the device sends this when a
        # follow-up window times out mid-stream, to drop any uncommitted partial
        # utterance from OpenAI's input buffer AT THE CUT-OFF (so no reactive
        # clear-on-wake is needed). Set by WebSocketHandler.build_pipeline.
        self._on_mic_flush = None
        # Async callback for {"type":"wake"} — sent by va_client on every wake.
        # Resets the dangling-VAD guard's "speech since wake" tracker. Set by
        # WebSocketHandler.build_pipeline.
        self._on_wake = None
        self._speaker_probe = None
        self._enrollment_recorder = None
        self._on_enroll_stopped = None
        self._last_wake_mono = 0.0
        self._on_button_cancel = None
        # True once any reply audio has gone OUT since the last wake. A button
        # press with no reply yet = silencing a false trigger; a press after a
        # reply = the user's normal "I'm done" gesture (must NOT be flagged).
        self._reply_audio_since_wake = False
        # Out-of-band announcements (timer expiry): while playing, inbound mic
        # audio is dropped so the assistant can't hear and answer itself.
        self.suppress_inbound_until = 0.0
        self._last_button_mono = 0.0
        # Set on wake; cleared when we ack the first mic frame back to the
        # device (cancels its no-speech watchdog — audio is flowing).
        self._ack_pending = False
        self._on_first_audio = None

    def set_interrupt_handler(self, handler):
        """Register the async no-arg callback fired on a device 'interrupt'."""
        self._on_interrupt = handler

    def set_session_start_handler(self, handler):
        """Register the async no-arg callback fired on a device 'start'."""
        self._on_session_start = handler

    def set_mic_flush_handler(self, handler):
        """Register the async no-arg callback fired on a device 'flush'."""
        self._on_mic_flush = handler

    def set_wake_handler(self, handler):
        """Register the async no-arg callback fired on a device 'wake'."""
        self._on_wake = handler

    def set_speaker_probe(self, probe):
        """Register a SpeakerProbe: gets start_capture() on wake and feed() for
        every inbound audio frame (cheap append; classification runs off-loop)."""
        self._speaker_probe = probe

    def set_enrollment_recorder(self, recorder):
        """Register an EnrollmentRecorder: fed every inbound audio frame while
        an enrollment session is active (guided voice-training capture)."""
        self._enrollment_recorder = recorder

    def set_button_cancel_handler(self, handler):
        """Async no-arg callback for a button-cancel within 12s of a wake."""
        self._on_button_cancel = handler

    def set_first_audio_handler(self, handler):
        """Async no-arg callback fired on the first mic frame after a wake."""
        self._on_first_audio = handler

    def set_enroll_stopped_handler(self, handler):
        """Register the async no-arg callback fired when the DEVICE ends
        enrollment ({"type":"enroll_stopped"} — button escape or firmware cap)."""
        self._on_enroll_stopped = handler

    @property
    def type(self) -> FrameSerializerType:
        """Get the serialization type - binary for raw audio."""
        return FrameSerializerType.BINARY

    async def deserialize(self, message: bytes) -> InputAudioRawFrame:
        """Deserialize binary message as raw PCM audio frame.

        Args:
            message: Binary PCM audio data (16-bit, mono, device sample rate)

        Returns:
            InputAudioRawFrame with the audio data, or None if invalid
        """
        # Device CONTROL frames arrive as TEXT (str). pipecat 0.0.97's websocket
        # transport has NO on_message event and routes EVERY incoming frame
        # through this serializer, so the device's {"type":"interrupt"} (sent
        # when the user says the "stop" wake word) would be silently dropped and
        # the assistant's reply would never stop. Handle it via the registered
        # interrupt callback (which sends an explicit OpenAI response.cancel) and
        # inject NO frame into the pipeline — emitting a pipecat InterruptionFrame
        # here would be indistinguishable from the VAD's own per-utterance
        # interruptions and would cancel the reply on any speech.
        if isinstance(message, str):
            try:
                data = json.loads(message)
            except (ValueError, TypeError):
                return None
            if isinstance(data, dict) and data.get("type") == "interrupt":
                # During voice enrollment the stop-word model false-fires on the
                # user's repetition batches (observed live: red flash + dropped
                # in-flight audio 10 s into round one). Ignore interrupts while
                # enrolling so the captured batch survives; the device-side mic
                # close is recovered by a silent re-wake.
                if self._enrollment_recorder is not None and self._enrollment_recorder.active:
                    logger.info("🛑 device interrupt IGNORED (enrollment active)")
                    return None
                logger.info("🛑 device interrupt received")
                if self._on_interrupt is not None:
                    try:
                        await self._on_interrupt()
                    except Exception as e:
                        logger.warning(f"⚠️ device interrupt handler failed: {e!r}")
            elif isinstance(data, dict) and data.get("type") == "start":
                # Sent by va_client once per WS connection (on connect). Mic
                # audio only flows after a wake, so clearing the stale OpenAI
                # input buffer here cannot eat new speech.
                logger.info("🎬 device connection start received")
                if self._on_session_start is not None:
                    try:
                        await self._on_session_start()
                    except Exception as e:
                        logger.warning(f"⚠️ device session-start handler failed: {e!r}")
            elif isinstance(data, dict) and data.get("type") == "flush":
                # A follow-up window timed out mid-stream: drop any uncommitted
                # partial utterance at the cut-off so a later wake can't complete
                # it into a stale answer.
                logger.info("🧽 device mic flush received")
                if self._speaker_probe is not None:
                    self._speaker_probe.finalize_partial()
                if self._on_mic_flush is not None:
                    try:
                        await self._on_mic_flush()
                    except Exception as e:
                        logger.warning(f"⚠️ device mic-flush handler failed: {e!r}")
            elif isinstance(data, dict) and data.get("type") == "button_cancel":
                # Center button silenced an active session. Within a short
                # window of the wake this is a human flagging a false trigger.
                self._last_button_mono = time.monotonic()
                dt = time.monotonic() - self._last_wake_mono
                logger.info(
                    f"🔘 button cancel received ({dt:.1f}s after wake, "
                    f"reply_audio={self._reply_audio_since_wake})"
                )
                if dt <= 12.0 and not self._reply_audio_since_wake and self._on_button_cancel is not None:
                    try:
                        await self._on_button_cancel()
                    except Exception as e:
                        logger.warning(f"⚠️ button-cancel handler failed: {e!r}")
            elif isinstance(data, dict) and data.get("type") == "false_flag":
                # Double-press: explicit false-wake flag, no conditions.
                self._last_button_mono = time.monotonic()
                logger.info("🔘🔘 explicit false-wake flag (double-press)")
                if self._on_button_cancel is not None:
                    try:
                        await self._on_button_cancel()
                    except Exception as e:
                        logger.warning(f"⚠️ false-flag handler failed: {e!r}")
            elif isinstance(data, dict) and data.get("type") == "enroll_stopped":
                # Device-side enrollment exit (button / firmware safety cap).
                logger.info("🎓 device ended enrollment")
                if self._on_enroll_stopped is not None:
                    try:
                        await self._on_enroll_stopped()
                    except Exception as e:
                        logger.warning(f"⚠️ enroll-stopped handler failed: {e!r}")
            elif isinstance(data, dict) and data.get("type") == "wake":
                # Sent by va_client on every wake (start_session). Marks a fresh
                # turn boundary for the dangling-VAD guard: until the user
                # actually speaks, any server-VAD end-of-turn is a stale segment
                # from the previous turn closing late (→ garbage response).
                logger.info("👋 device wake received")
                self._last_wake_mono = time.monotonic()
                self._reply_audio_since_wake = False
                self._ack_pending = True
                try:
                    from .ha_sensors import PUBLISHER
                    await PUBLISHER.wake()
                except Exception:
                    pass
                if self._speaker_probe is not None:
                    self._speaker_probe.start_capture()
                if self._on_wake is not None:
                    try:
                        await self._on_wake()
                    except Exception as e:
                        logger.warning(f"⚠️ device wake handler failed: {e!r}")
            # interrupt / ping / start / other control frames: nothing to inject.
            return None

        if not isinstance(message, bytes):
            # Skip anything that isn't bytes or a known text control frame.
            return None

        # Validate audio format: 16-bit = 2 bytes per sample
        if len(message) % 2 != 0:
            logger.warning(f"⚠️ Received audio with odd byte count: {len(message)} bytes, skipping")
            return None

        # First mic frame after a wake: tell the device audio is flowing so
        # it drops its no-speech watchdog (semantic VAD can be slow to commit).
        if self._ack_pending:
            self._ack_pending = False
            if self._on_first_audio is not None:
                try:
                    await self._on_first_audio()
                except Exception as e:
                    logger.warning(f"⚠️ audio-ack failed: {e!r}")

        # Announcement echo-guard: drop inbound audio while an out-of-band
        # announcement is playing (observed: the mic heard "your timer is done",
        # transcribed it, and the model replied to itself).
        if time.monotonic() < self.suppress_inbound_until:
            return None

        # Tee the post-wake capture window to the speaker probe (no-op unless a
        # wake armed it; classification runs in a thread, never blocks here).
        if self._speaker_probe is not None:
            self._speaker_probe.feed(message)

        # Voice enrollment: while a session is active, mic audio goes ONLY to
        # the recorder — OpenAI must not hear it (no VAD commits, no forced
        # responses, no cost). The device is in enrollment mode with its own
        # LED/phase; the pipeline simply sees silence.
        if self._enrollment_recorder is not None and self._enrollment_recorder.active:
            self._enrollment_recorder.feed(message)
            return None

        # Create InputAudioRawFrame at the device's mic rate; the InputResampler
        # processor (right after transport.input()) upsamples it to 24 kHz.
        frame = InputAudioRawFrame(
            audio=message,
            sample_rate=self._input_sample_rate,
            num_channels=1
        )

        return frame
    
    async def serialize(self, frame: Frame) -> bytes:
        if isinstance(frame, OutputAudioRawFrame):
            self._reply_audio_since_wake = True
        """Serialize frame to binary message.
        
        For output audio frames, we just return the raw audio bytes.
        Other frames are not serialized (return empty bytes).
        """
        if isinstance(frame, OutputAudioRawFrame):
            audio_bytes = frame.audio
            logger.debug(f"📤 Serializing OutputAudioRawFrame: {len(audio_bytes)} bytes")
            return audio_bytes
        # For other frame types, return empty bytes (not serialized)
        logger.debug(f"📤 Serializing non-audio frame: {type(frame).__name__}, returning empty bytes")
        return b""

