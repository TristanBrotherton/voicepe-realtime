"""Give the device a playout buffer lead at the START of every reply.

Root cause of the reply-start click + mid-reply pauses (measured; ~50% of
turns): the Voice PE's output resampler self-stops when idle between replies and
COLD-STARTS on the next reply. A cold turn begins with a dry speaker AND never
builds a buffer lead, so any WebSocket jitter starves it mid-reply; a warm turn
starts with a lead and rides through the same jitter invisibly. A bigger device
i2s buffer does NOT help — on a cold turn it simply never fills (A/B'd 500->1000
ms, cold-start rate unchanged).

The device-side fix is a firmware `va_client` prime (raised upstream separately).
This is the RELAY-side half, and it needs no firmware change: it copies Alexa's
"complete-utterance" smoothing at the relay. At the start of each reply (the
first OutputAudioRawFrame after an idle gap), HOLD the first LEAD_MS of OpenAI
audio, then release it as one burst so the device's ring buffer fills instantly
and carries a lead through downstream jitter. Once the lead is released, audio
passes through untouched — so the only added latency is ~LEAD_MS to the first
word, once per reply. OpenAI bursts reply audio faster than real-time, so holding
LEAD_MS of it costs far less than LEAD_MS of wall-clock.

Barge-in safety: on StartInterruptionFrame the held audio is DROPPED (the user
interrupted — never burst stale reply audio). The device also flushes its PSRAM
queue authoritatively on 'stop', so anything already sent is discarded there too;
this simply avoids sending bytes the user won't hear. A short reply whose total
audio never reaches LEAD_MS is released on BotStoppedSpeakingFrame / EndFrame so
it is never swallowed.

Placed LAST in the pipeline, immediately before transport.output(), so it sees
the final OutputAudioRawFrame stream and the interruption/speaking control frames
that gate it.

Disable by setting OUTPUT_LEAD_BUFFER_MS=0 (then this is a pure pass-through).
"""

import os
import time
import logging

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    OutputAudioRawFrame,
    StartInterruptionFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"⚠️ {name}={raw!r} is not an int; using default {default}")
        return default


class OutputLeadBuffer(FrameProcessor):
    """Hold a short lead of reply-start audio, then burst it, to prime the
    device's playout buffer against the resampler cold-start starve.

    Args:
        lead_ms: audio to accumulate before the first burst of a cold reply. 0
            disables (pure pass-through). Also read from OUTPUT_LEAD_BUFFER_MS.
        idle_gap_ms: silence since the last output audio that marks the next
            frame as a cold reply-start. Continuous audio (gap < this) passes
            through so mid-reply audio is never re-buffered.
        max_hold_ms: safety cap — flush whatever is held once this long has
            elapsed since buffering began, even if lead_ms bytes never arrived.
    """

    def __init__(
        self,
        lead_ms: int | None = None,
        idle_gap_ms: int | None = None,
        max_hold_ms: int | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._lead_ms = _env_int("OUTPUT_LEAD_BUFFER_MS", lead_ms if lead_ms is not None else 400)
        self._idle_gap_s = (idle_gap_ms if idle_gap_ms is not None else 250) / 1000.0
        # Cap defaults comfortably above the lead so it only trips if audio stalls
        # mid-buffer; never below the lead (that would defeat the buffering).
        cap = max_hold_ms if max_hold_ms is not None else max(self._lead_ms + 400, 1000)
        self._max_hold_s = cap / 1000.0

        self._buffering = False
        self._held: list[OutputAudioRawFrame] = []
        self._held_bytes = 0
        self._buffer_start = 0.0
        self._last_out_ts = 0.0  # 0 => the first reply is treated as cold
        self._logged_disabled = False

    def _lead_bytes(self, frame: OutputAudioRawFrame) -> int:
        # PCM16 => 2 bytes/sample/channel.
        return int(frame.sample_rate * frame.num_channels * 2 * self._lead_ms / 1000)

    async def _flush(self, direction: FrameDirection):
        frames, self._held = self._held, []
        self._held_bytes = 0
        self._buffering = False
        for f in frames:
            await self.push_frame(f, direction)

    def _drop(self):
        self._held = []
        self._held_bytes = 0
        self._buffering = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Disabled => pure pass-through (no state, no held audio).
        if self._lead_ms <= 0:
            if not self._logged_disabled:
                logger.info("🔈 OutputLeadBuffer disabled (OUTPUT_LEAD_BUFFER_MS=0)")
                self._logged_disabled = True
            await self.push_frame(frame, direction)
            return

        # Barge-in: the user interrupted. Drop held (never-heard) audio and let
        # the interruption propagate. The next reply re-buffers naturally.
        if isinstance(frame, StartInterruptionFrame):
            if self._held:
                logger.debug(f"🛑 OutputLeadBuffer dropping {len(self._held)} held frame(s) on interruption")
            self._drop()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, OutputAudioRawFrame):
            now = time.monotonic()
            gap = now - self._last_out_ts
            self._last_out_ts = now

            # Cold reply-start: first audio, or audio after an idle gap.
            if not self._buffering and gap >= self._idle_gap_s:
                self._buffering = True
                self._buffer_start = now

            if self._buffering:
                self._held.append(frame)
                self._held_bytes += len(frame.audio)
                reached_lead = self._held_bytes >= self._lead_bytes(frame)
                hit_cap = (now - self._buffer_start) >= self._max_hold_s
                if reached_lead or hit_cap:
                    if hit_cap and not reached_lead:
                        logger.debug("⏱️ OutputLeadBuffer flushing on max-hold cap (audio stalled mid-lead)")
                    await self._flush(direction)
                return

            # Warm: audio is flowing continuously — pass straight through.
            await self.push_frame(frame, direction)
            return

        # End of a speaking segment (or session): release any held lead so a
        # reply shorter than lead_ms is never swallowed. BotStoppedSpeakingFrame
        # can fire several times per reply; flushing held audio on it is safe
        # (it just releases the lead a touch early).
        if isinstance(frame, (BotStoppedSpeakingFrame, EndFrame)):
            if self._held:
                await self._flush(direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
