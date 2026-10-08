"""Local turn detection: the add-on decides where a user turn ends.

Shared by every engine whose own turn detection fails this device -- Gemini
since 0.22.5, xAI since 0.25.3. The device signals only the wake; nothing
marks the END of speech, so the Silero model pipecat already ships, run by
sherpa-onnx (installed for voice prints), decides it here. No new dependency.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# Quiet for this long = "preend" (Gemini with bana 0). LOCAL_PRE_END_MS=0 turns it off.
PRE_END_MS = int(os.environ.get("LOCAL_PRE_END_MS", "500"))


class LocalTurns:
    """Feed it mic audio; it says "start" and "end" of speech.

    Google's automatic activity detection did not open turns for this
    device (live 2026-10-02 13:42-13:45: "Var är klockan?" at -26 dBFS, then
    60 s of nothing), and xAI's server_vad ended them 5-13 s late in a room
    with music (live 2026-10-02 20:29:42, "Vad är det för väder i helgen?").
    """

    # Silero goes deaf after ~20 s of non-speech unless its state is reset;
    # pipecat's own SileroVADAnalyzer resets every 5 s for the same reason.
    # Measured on the 13:42 recording: without it the command at 144 s was
    # never detected.
    RESET_AFTER_QUIET_S = 5.0

    def __init__(self, vad, sample_rate: int = 16000, pre_vad=None):
        self._vad = vad
        self._pre = pre_vad  # a second detector with a shorter silence: "preend"
        self._pre_speaking = False
        self._rate = sample_rate
        self._speaking = False
        self._quiet_samples = 0

    @classmethod
    def create(cls, silence_ms: int, sample_rate: int = 16000, pre_ms: int = 0) -> Optional["LocalTurns"]:
        """Build one, or None (logged) when the model or library is missing.

        `pre_ms` (0 = off, else at least 200 ms under `silence_ms`) adds the event
        "preend": the speech has been quiet for pre_ms, but the turn is not over yet.
        Bana 0 starts its speech-to-text then, with the last stretch of the wait
        still to go (raawr US-032).
        """
        try:
            import pipecat
            import sherpa_onnx

            def detector(ms):
                config = sherpa_onnx.VadModelConfig()
                config.silero_vad.model = os.path.join(
                    os.path.dirname(pipecat.__file__), "audio", "vad", "data", "silero_vad.onnx"
                )
                config.silero_vad.threshold = 0.5
                config.silero_vad.min_speech_duration = 0.25
                config.silero_vad.min_silence_duration = max(0.2, ms / 1000)
                config.sample_rate = sample_rate
                return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)

            vad = detector(silence_ms)
            pre = detector(pre_ms) if pre_ms and silence_ms - pre_ms >= 200 else None
        except Exception as e:
            logger.error(f"❌ no local turn detection ({e!r}) — the engine keeps its own")
            return None
        return cls(vad, sample_rate, pre)

    def feed(self, pcm16: bytes, sample_rate: Optional[int] = None) -> Optional[str]:
        """Feed mic audio (PCM16 mono). Returns "start", "preend", "end" or None.

        `sample_rate` is the audio's own rate; Silero only takes 8/16 kHz, so
        anything else (xAI's 24 kHz input) is brought down to the VAD's rate.
        """
        import numpy as np

        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate and sample_rate != self._rate and len(samples):
            n = int(len(samples) * self._rate / sample_rate)
            samples = np.interp(
                np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples
            ).astype(np.float32)
        self._vad.accept_waveform(samples)
        while not self._vad.empty():
            self._vad.pop()  # only the state is used, never the segments
        speaking = bool(self._vad.is_speech_detected())
        event = None
        if speaking != self._speaking:
            event = "start" if speaking else "end"
            self._speaking = speaking
        if self._pre is not None:
            self._pre.accept_waveform(samples)
            while not self._pre.empty():
                self._pre.pop()
            pre_speaking = bool(self._pre.is_speech_detected())
            if self._pre_speaking and not pre_speaking and self._speaking and event is None:
                event = "preend"  # quiet for pre_ms; the turn itself ends later
            self._pre_speaking = pre_speaking
        self._quiet_samples = 0 if speaking else self._quiet_samples + len(samples)
        if self._quiet_samples >= self.RESET_AFTER_QUIET_S * self._rate:
            self._vad.reset()
            if self._pre is not None:
                self._pre.reset()
                self._pre_speaking = False
            self._quiet_samples = 0
        return event

    def reset(self) -> None:
        self._vad.reset()
        if self._pre is not None:
            self._pre.reset()
            self._pre_speaking = False
        self._speaking = False
        self._quiet_samples = 0


class LocalTurnsMixin:
    """The pre-roll every engine with local turns needs.

    Audio from before the local VAD said "speech" is held, not sent: Silero
    needs ~0.3 s to be sure, and the first syllable must still reach the
    model when the turn opens.
    """

    # Set by the engine's build() when local turn detection is on; None means
    # the engine's own turn detection.
    _turns: Optional[LocalTurns] = None
    PREROLL_S = 0.5

    def _keep_preroll(self, frame) -> None:
        preroll = getattr(self, "_preroll", bytearray())
        preroll += frame.audio
        keep = int(self.PREROLL_S * frame.sample_rate) * 2
        self._preroll = preroll[-keep:]

    def _take_preroll(self) -> bytes:
        preroll = bytes(getattr(self, "_preroll", b""))
        self._preroll = bytearray()
        return preroll
