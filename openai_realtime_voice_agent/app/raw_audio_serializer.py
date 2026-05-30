"""Simple serializer for raw binary PCM audio frames."""
import logging
import os
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
        if not isinstance(message, bytes):
            # Skip non-binary messages (text/JSON)
            return None

        # Validate audio format: 16-bit = 2 bytes per sample
        if len(message) % 2 != 0:
            logger.warning(f"⚠️ Received audio with odd byte count: {len(message)} bytes, skipping")
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

