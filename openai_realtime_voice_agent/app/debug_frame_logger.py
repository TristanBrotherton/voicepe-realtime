"""Verbose pipeline frame logger for the DEV channel.

Inserted into the pipeline ONLY when LOG_LEVEL=DEBUG (the dev add-on default;
silent on stable). It logs the turn / audio / interruption lifecycle as frames
pass a single point in the pipeline, so the add-on log alone shows WHEN each thing
happens:
  - user speech start/stop, end-of-turn (server VAD)
  - bot speech start/stop, LLM response start/end, interruptions
  - how much TTS audio (ms) each response produced — useful for spotting a
    burst-complete reply the device only partly played (the "stale audio" heard
    after a 'stop').

It never transforms or drops a frame (pure instrumentation). Frames are matched by
TYPE NAME (string), so it is robust to pipecat frame-class renames across versions
(an unknown name is simply ignored, never a crash).
"""
import logging

from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

logger = logging.getLogger(__name__)

# Lifecycle frames worth a one-line marker.
_LIFECYCLE = {
    "UserStartedSpeakingFrame": "🗣️▶ user speech start",
    "UserStoppedSpeakingFrame": "🗣️⏹ user speech stop (end-of-turn)",
    "BotStartedSpeakingFrame": "🔊▶ bot speech start",
    "BotStoppedSpeakingFrame": "🔊⏹ bot speech stop",
    "LLMFullResponseStartFrame": "🧠▶ response start",
    "LLMFullResponseEndFrame": "🧠⏹ response end",
    "TTSStartedFrame": "🎙️▶ tts start",
    "TTSStoppedFrame": "🎙️⏹ tts stop",
    "StartInterruptionFrame": "✋ interruption start",
    "StopInterruptionFrame": "✋ interruption stop",
    "InterruptionFrame": "✋ interruption",
    "BotInterruptionFrame": "✋ bot interruption",
}
# Audio frames whose bytes we tally to report TTS duration per response.
_AUDIO = {"TTSAudioRawFrame", "OutputAudioRawFrame"}
# Output PCM down to the device is 24 kHz mono 16-bit -> 48000 bytes/sec.
_BYTES_PER_SEC = 24000 * 2


class DebugFrameLogger(FrameProcessor):
    """Passive, DEBUG-only tap that logs the turn / audio / interruption lifecycle."""

    def __init__(self, label: str = "pipe", **kwargs):
        super().__init__(**kwargs)
        self._label = label
        self._audio_bytes = 0
        self._audio_frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        name = type(frame).__name__
        if name in _AUDIO:
            audio = getattr(frame, "audio", b"") or b""
            self._audio_bytes += len(audio)
            self._audio_frames += 1
        else:
            marker = _LIFECYCLE.get(name)
            if marker is not None:
                extra = ""
                # On a response/tts/bot end, report how much audio we just saw —
                # i.e. how long the (possibly burst-complete) reply actually was.
                if (
                    name in ("LLMFullResponseEndFrame", "TTSStoppedFrame", "BotStoppedSpeakingFrame")
                    and self._audio_bytes
                ):
                    ms = int(self._audio_bytes / _BYTES_PER_SEC * 1000)
                    extra = f" — {ms} ms TTS in {self._audio_frames} frames"
                    self._audio_bytes = 0
                    self._audio_frames = 0
                logger.debug("🎚️ [%s] %s (%s)%s", self._label, marker, direction.name, extra)
            elif name == "TranscriptionFrame":
                logger.debug("🎚️ [%s] transcription: %r", self._label, getattr(frame, "text", ""))
        await self.push_frame(frame, direction)
