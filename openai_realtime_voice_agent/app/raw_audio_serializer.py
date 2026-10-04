"""Serializer for the Voice PE link: binary PCM audio plus JSON control frames."""
import asyncio
import base64
import inspect
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, Frame
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType

logger = logging.getLogger(__name__)

# Fields the firmware may attach to {"type":"wake"} (protocol v2). Values are
# validated and size-limited before they reach logs, sensors or filenames.
WAKE_META_STRINGS = ("turn", "src", "model", "model_sha", "tier", "fw")
WAKE_META_NUMBERS = ("cutoff", "cutoff_uint8", "window", "fire_age_ms", "v")
MAX_TRIGGER_AUDIO_BYTES = 3 * 16000 * 2  # 3 s of 16 kHz mono PCM16
MAX_TRIGGER_CHUNKS = 64


def parse_wake_meta(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the well-formed wake metadata fields; drop everything else."""
    meta: Dict[str, Any] = {}
    for key in WAKE_META_STRINGS:
        value = data.get(key)
        if isinstance(value, str) and value:
            cleaned = "".join(c for c in value if c.isalnum() or c in "-_.:")[:40]
            if cleaned:
                meta[key] = cleaned
    for key in WAKE_META_NUMBERS:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and -1 < value < 1_000_000:
            meta[key] = value
    # Firmware sends the cutoff as the quantized uint8 micro_wake_word uses.
    if "cutoff_uint8" in meta and "cutoff" not in meta:
        meta["cutoff"] = round(float(meta["cutoff_uint8"]) / 255.0, 3)
    return meta


class RawAudioSerializer(FrameSerializer):
    """Treats binary messages as raw PCM and routes text frames to handlers.

    pipecat 0.0.97's websocket transport routes EVERY inbound frame through
    this serializer, so the device's JSON control messages (wake, interrupt,
    ping, flush, flags, metrics) are dispatched here. None of them injects a
    pipeline frame, and none may block: the receive loop is also the path
    that delivers the user's audio.
    """

    def __init__(self, device_id: str, input_sample_rate: int | None = None):
        self._device_id = device_id
        # The Voice PE firmware streams 16 kHz PCM16 mono; the InputResampler
        # processor upsamples to the 24 kHz OpenAI requires.
        if input_sample_rate is None:
            input_sample_rate = int(os.environ.get("DEVICE_INPUT_SAMPLE_RATE", "16000"))
        self._input_sample_rate = input_sample_rate
        self._handlers: Dict[str, Optional[Callable[..., Awaitable[None]]]] = {}
        self._speaker_probe = None
        self._enrollment_recorder = None
        self._audio_taps = []
        self._last_wake_mono = 0.0
        # True once any reply audio has gone OUT since the last wake. A button
        # press with no reply yet = silencing a false trigger; a press after a
        # reply = the user's normal "I'm done" gesture (must NOT be flagged).
        self._reply_audio_since_wake = False
        # Out-of-band announcements (timer expiry, acknowledgements): while
        # playing, inbound mic audio is dropped so the assistant can't hear
        # and answer itself.
        self.suppress_inbound_until = 0.0
        self._last_button_mono = 0.0
        # Set on wake; cleared when we ack the first mic frame back to the
        # device (cancels its no-speech watchdog — audio is flowing).
        self._ack_pending = False
        self._on_activity = None
        self._on_output_audio = None
        self.current_turn_id = ""
        self.wake_meta: Dict[str, Any] = {}
        self._trigger_chunks: Dict[str, list] = {}
        # Out-of-band prompts (acknowledgements, spoken errors) are written
        # straight to the socket. While one plays, reply audio waits here so
        # the two streams never interleave in the device's playback ring.
        self._oob_done: Optional[asyncio.Event] = None

    # -- handler registration (kept as explicit setters for readability) ----
    def _set(self, name, handler):
        self._handlers[name] = handler

    def set_interrupt_handler(self, handler):
        """Async no-arg callback fired on a device 'interrupt' ("stop")."""
        self._set("interrupt", handler)

    def set_session_start_handler(self, handler):
        """Async no-arg callback fired on 'start' (once per WS connection)."""
        self._set("start", handler)

    def set_mic_flush_handler(self, handler):
        """Async no-arg callback fired on 'flush' (follow-up window timed out)."""
        self._set("flush", handler)

    def set_wake_handler(self, handler):
        """Async callback(meta: dict) fired on every 'wake'."""
        self._set("wake", handler)

    def set_button_cancel_handler(self, handler):
        """Async callback(method: str, turn_id: str, age_ms: int) for a false-wake flag."""
        self._set("false_flag", handler)

    def set_first_audio_handler(self, handler):
        """Async no-arg callback fired on the first mic frame after a wake."""
        self._set("first_audio", handler)

    def set_enroll_stopped_handler(self, handler):
        """Async no-arg callback fired when the DEVICE ends enrollment."""
        self._set("enroll_stopped", handler)

    def set_ping_handler(self, handler):
        """Async no-arg callback answering the device's keepalive ping."""
        self._set("ping", handler)

    def set_turn_metrics_handler(self, handler):
        """Async callback(turn_id: str, metrics: dict) for device turn timings."""
        self._set("turn_metrics", handler)

    def set_trigger_audio_handler(self, handler):
        """Async callback(turn_id: str, pcm: bytes, rate: int) for the opt-in pre-wake snippet."""
        self._set("trigger_audio", handler)

    def set_activity_handler(self, handler):
        """Sync no-arg callback marking this device as the one in use."""
        self._on_activity = handler

    def set_output_audio_handler(self, handler):
        """Sync no-arg callback invoked for every reply audio frame sent."""
        self._on_output_audio = handler

    def set_speaker_probe(self, probe):
        """SpeakerProbe: start_capture() on wake, feed() for every inbound frame."""
        self._speaker_probe = probe

    def add_audio_tap(self, tap):
        """Object with feed(pcm: bytes) receiving every forwarded inbound frame."""
        self._audio_taps.append(tap)

    def set_enrollment_recorder(self, recorder):
        """EnrollmentRecorder: receives mic audio while an enrollment session is active."""
        self._enrollment_recorder = recorder

    async def _call(self, name, *args):
        handler = self._handlers.get(name)
        if handler is None:
            return
        try:
            if args and len(inspect.signature(handler).parameters) == 0:
                await handler()
            else:
                await handler(*args)
        except Exception as e:
            logger.warning(f"⚠️ device {name} handler failed: {e!r}")

    @property
    def type(self) -> FrameSerializerType:
        return FrameSerializerType.BINARY

    def _enrolling(self) -> bool:
        return (
            self._enrollment_recorder is not None
            and self._enrollment_recorder.active
            and self._enrollment_recorder.device_id == self._device_id
        )

    async def _handle_control(self, data: Dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "interrupt":
            # During voice enrollment the stop-word model false-fires on the
            # user's repetition batches; ignore so the captured batch survives.
            if self._enrolling():
                logger.info("🛑 device interrupt IGNORED (enrollment active)")
                return
            logger.info("🛑 device interrupt received")
            await self._call("interrupt")
        elif kind == "start":
            logger.info("🎬 device connection start received")
            await self._call("start")
        elif kind == "flush":
            logger.info("🧽 device mic flush received")
            if self._speaker_probe is not None:
                self._speaker_probe.finalize_partial()
            for tap in self._audio_taps:
                finalize = getattr(tap, "finalize", None)
                if finalize is not None:
                    finalize()
            await self._call("flush")
        elif kind == "button_cancel":
            # Center button silenced an active session. Within a short window
            # of the wake, with no reply heard yet, this is a human flagging a
            # false trigger.
            self._last_button_mono = time.monotonic()
            dt = time.monotonic() - self._last_wake_mono
            logger.info(
                f"🔘 button cancel received ({dt:.1f}s after wake, "
                f"reply_audio={self._reply_audio_since_wake})"
            )
            if dt <= 12.0 and not self._reply_audio_since_wake:
                await self._call("false_flag", "button", self._turn_from(data), 0)
        elif kind == "false_flag":
            # Double-press: explicit false-wake flag. Scoped to this device and
            # bounded in time by the WakeEventStore; firmware v2 names the turn
            # and, for flags queued while offline, how old the press is.
            self._last_button_mono = time.monotonic()
            age_ms = data.get("age_ms") if isinstance(data.get("age_ms"), int) else 0
            logger.info(f"🔘🔘 explicit false-wake flag (double-press, age {age_ms} ms)")
            await self._call("false_flag", "double_press", self._turn_from(data), max(0, age_ms))
        elif kind == "ping":
            await self._call("ping")
        elif kind == "enroll_stopped":
            logger.info("🎓 device ended enrollment")
            await self._call("enroll_stopped")
        elif kind == "wake":
            await self._handle_wake(data)
        elif kind == "turn_metrics":
            turn_id = self._turn_from(data)
            if turn_id:
                await self._call("turn_metrics", turn_id, data)
        elif kind == "trigger_audio":
            await self._handle_trigger_chunk(data)

    def _turn_from(self, data: Dict[str, Any]) -> str:
        meta = parse_wake_meta({"turn": data.get("turn")})
        return meta.get("turn", "")

    async def _handle_wake(self, data: Dict[str, Any]) -> None:
        # Sent by va_client on every wake. Marks a fresh turn boundary for the
        # dangling-VAD guard and anchors this turn's latency timeline.
        self._last_wake_mono = time.monotonic()
        self._reply_audio_since_wake = False
        self._ack_pending = True
        self.wake_meta = parse_wake_meta(data)
        self.current_turn_id = self.wake_meta.get("turn", "")
        logger.info(
            "👋 device wake received"
            + (f" turn={self.current_turn_id}" if self.current_turn_id else "")
            + (f" model={self.wake_meta.get('model')} cutoff={self.wake_meta.get('cutoff')}"
               f" window={self.wake_meta.get('window')}" if self.wake_meta.get("model") else "")
        )
        # A wake is the strongest signal that THIS device is the one in use.
        if self._on_activity is not None:
            self._on_activity()
        if self._speaker_probe is not None:
            self._speaker_probe.start_capture()
        # The wake handler only schedules work (sensor publishing, liveness
        # probe, capture bookkeeping); it never awaits the network.
        await self._call("wake", self.wake_meta)

    async def _handle_trigger_chunk(self, data: Dict[str, Any]) -> None:
        turn_id = self._turn_from(data)
        b64 = data.get("b64")
        if not turn_id or not isinstance(b64, str):
            return
        chunks = self._trigger_chunks.setdefault(turn_id, [])
        if len(chunks) >= MAX_TRIGGER_CHUNKS:
            return
        try:
            chunks.append(base64.b64decode(b64, validate=True))
        except (ValueError, TypeError):
            self._trigger_chunks.pop(turn_id, None)
            return
        if sum(len(c) for c in chunks) > MAX_TRIGGER_AUDIO_BYTES:
            logger.warning("⚠️ trigger audio exceeded size limit — discarded")
            self._trigger_chunks.pop(turn_id, None)
            return
        if data.get("last"):
            pcm = b"".join(self._trigger_chunks.pop(turn_id, []))
            rate = data.get("rate") if data.get("rate") in (16000,) else 16000
            if pcm and len(pcm) % 2 == 0:
                await self._call("trigger_audio", turn_id, pcm, rate)
        # Never keep chunks from abandoned turns around.
        for stale in [t for t in self._trigger_chunks if t != turn_id]:
            self._trigger_chunks.pop(stale, None)

    async def deserialize(self, message) -> Optional[InputAudioRawFrame]:
        """Binary -> InputAudioRawFrame; text -> control dispatch (no frame)."""
        if isinstance(message, str):
            try:
                data = json.loads(message)
            except (ValueError, TypeError):
                return None
            if isinstance(data, dict):
                await self._handle_control(data)
            return None

        if not isinstance(message, bytes):
            return None

        # Validate audio format: 16-bit = 2 bytes per sample
        if len(message) % 2 != 0:
            logger.warning(f"⚠️ Received audio with odd byte count: {len(message)} bytes, skipping")
            return None

        # First mic frame after a wake: tell the device audio is flowing so it
        # drops its no-speech watchdog (semantic VAD can be slow to commit).
        if self._ack_pending:
            self._ack_pending = False
            await self._call("first_audio")

        # Announcement echo-guard: drop inbound audio while an out-of-band
        # announcement is playing (observed: the mic heard "your timer is
        # done", transcribed it, and the model replied to itself).
        if time.monotonic() < self.suppress_inbound_until:
            return None

        if self._speaker_probe is not None:
            self._speaker_probe.feed(message)

        # Voice enrollment: while active, mic audio goes ONLY to the recorder —
        # OpenAI must not hear it (no VAD commits, no responses, no cost).
        if self._enrolling():
            self._enrollment_recorder.feed(message)
            return None

        for tap in self._audio_taps:
            tap.feed(message)

        return InputAudioRawFrame(
            audio=message,
            sample_rate=self._input_sample_rate,
            num_channels=1,
        )

    def begin_out_of_band(self) -> None:
        """An out-of-band prompt starts: hold reply audio until it ends."""
        if self._oob_done is None:
            self._oob_done = asyncio.Event()
        self._oob_done.clear()

    def end_out_of_band(self) -> None:
        if self._oob_done is not None:
            self._oob_done.set()

    async def serialize(self, frame: Frame) -> bytes:
        """Reply audio frames go out as raw PCM; nothing else is serialized."""
        if isinstance(frame, OutputAudioRawFrame):
            if self._oob_done is not None and not self._oob_done.is_set():
                try:
                    await asyncio.wait_for(self._oob_done.wait(), 3.0)
                except asyncio.TimeoutError:
                    pass
            self._reply_audio_since_wake = True
            if self._on_output_audio is not None:
                try:
                    self._on_output_audio()
                except Exception:
                    pass
            return frame.audio
        return b""
