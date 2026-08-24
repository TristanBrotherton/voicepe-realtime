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
first OutputAudioRawFrame of a speaking segment, or after an idle gap), HOLD the
first LEAD_MS of OpenAI audio, then release it as one burst so the device's ring
buffer fills instantly and carries a lead through downstream jitter. Once the
lead is released, audio passes through untouched — so the only added latency is
~LEAD_MS to the first word, once per reply. OpenAI bursts reply audio faster
than real-time, so holding LEAD_MS of it costs far less than LEAD_MS of
wall-clock.

Mid-reply gaps are NOT re-buffered: Realtime TTS arrives in segments with
sub-second gaps within one reply (inter-sentence, around tool calls — see
phase_emitter.py), so the idle-gap test alone would re-hold LEAD_MS mid-reply on
an already-warm device. Re-arming is therefore gated on bot-speaking state:
while the bot is mid-segment (BotStartedSpeaking seen and audio already passed),
a gap never re-arms the hold.

If audio stalls entirely mid-hold, an asyncio watchdog flushes the held lead at
max_hold_ms — the cap does not depend on another frame arriving.

Barge-in safety: on StartInterruptionFrame the held audio is DROPPED (the user
interrupted — never burst stale reply audio), and an in-flight flush stops at
the next frame boundary (generation guard). The device also flushes its PSRAM
queue authoritatively on 'stop', so anything already sent is discarded there
too. CancelFrame and StartFrame (pipeline teardown / connection recovery) also
drop held audio, so a reply killed by a connection death never leaks its lead
into the next reply. A short reply whose total audio never reaches LEAD_MS is
released on BotStoppedSpeakingFrame / EndFrame so it is never swallowed.

Placed LAST in the pipeline, immediately before transport.output(), so it sees
the final OutputAudioRawFrame stream and the interruption/speaking control
frames that gate it.

Disable by setting output_lead_buffer_ms / OUTPUT_LEAD_BUFFER_MS to 0 (then this
is a pure pass-through). Default is 0 (opt-in) until runtime-validated.
"""

from __future__ import annotations

import asyncio
import os
import time
import logging

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    OutputAudioRawFrame,
    StartInterruptionFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    StartFrame,
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
            disables (pure pass-through). Falls back to OUTPUT_LEAD_BUFFER_MS,
            then 0 (opt-in).
        idle_gap_ms: silence since the last output audio that marks the next
            frame as a cold reply-start — only consulted when bot-speaking
            state says we are NOT mid-segment.
        max_hold_ms: safety cap — an asyncio watchdog flushes whatever is held
            once this long has elapsed since buffering began, even if no
            further frame ever arrives.
    """

    def __init__(
        self,
        lead_ms: int | None = None,
        idle_gap_ms: int | None = None,
        max_hold_ms: int | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._lead_ms = _env_int("OUTPUT_LEAD_BUFFER_MS", lead_ms if lead_ms is not None else 0)
        self._idle_gap_s = (idle_gap_ms if idle_gap_ms is not None else 250) / 1000.0
        # Cap defaults comfortably above the lead so it only trips if audio stalls
        # mid-buffer; never below the lead (that would defeat the buffering).
        cap = max_hold_ms if max_hold_ms is not None else max(self._lead_ms + 400, 1000)
        self._max_hold_s = cap / 1000.0

        self._buffering = False
        self._held: list[OutputAudioRawFrame] = []
        self._held_bytes = 0
        # None => no audio yet: the first reply is always cold. (A 0.0 sentinel
        # is wrong here — time.monotonic()'s epoch is arbitrary and can sit
        # near zero at process start, making the first gap look tiny.)
        self._last_out_ts: float | None = None
        self._logged_disabled = False

        # Bot-speaking segment state (gates mid-reply re-arm).
        self._bot_speaking = False
        self._audio_this_segment = False

        # Generation guard: bumped on every drop and on every new hold. An
        # in-flight _flush() or a stale cap watchdog compares its generation
        # and stops/no-ops if the world moved on beneath it.
        self._gen = 0
        self._cap_task: asyncio.Task | None = None

    def _lead_bytes(self, frame: OutputAudioRawFrame) -> int:
        # PCM16 => 2 bytes/sample/channel.
        return int(frame.sample_rate * frame.num_channels * 2 * self._lead_ms / 1000)

    def _cancel_cap_timer(self):
        if self._cap_task is not None:
            self._cap_task.cancel()
            self._cap_task = None

    def _arm_cap_timer(self, direction: FrameDirection):
        """Flush-on-stall watchdog. hit-cap flushing must NOT depend on the
        next audio frame arriving: if the stream stalls entirely mid-hold,
        this timer is the only thing that releases the held lead."""
        self._cancel_cap_timer()
        gen = self._gen

        async def _watchdog():
            try:
                await asyncio.sleep(self._max_hold_s)
            except asyncio.CancelledError:
                return
            if self._buffering and self._gen == gen and self._held:
                logger.debug("⏱️ OutputLeadBuffer flushing on max-hold cap (audio stalled mid-lead)")
                await self._flush(direction)

        self._cap_task = asyncio.create_task(_watchdog())

    async def _flush(self, direction: FrameDirection):
        """Release held frames in order. Iterates the live list under a
        generation guard so a concurrent _drop() (interruption delivered
        out-of-band while we are suspended on push_frame) stops the release
        at the next frame boundary instead of pushing stale audio."""
        self._buffering = False
        self._cancel_cap_timer()
        gen = self._gen
        while self._held and self._gen == gen:
            f = self._held.pop(0)
            self._held_bytes -= len(f.audio)
            await self.push_frame(f, direction)
        if self._gen == gen:
            self._held_bytes = 0

    def _drop(self):
        self._gen += 1  # halts any in-flight _flush at its next iteration
        self._cancel_cap_timer()
        self._held = []
        self._held_bytes = 0
        self._buffering = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Disabled => pure pass-through (no state, no held audio).
        if self._lead_ms <= 0:
            if not self._logged_disabled:
                logger.info("🔈 OutputLeadBuffer disabled (lead_ms=0)")
                self._logged_disabled = True
            await self.push_frame(frame, direction)
            return

        # Barge-in: the user interrupted. Drop held (never-heard) audio and let
        # the interruption propagate. The next reply re-buffers naturally.
        if isinstance(frame, StartInterruptionFrame):
            if self._held:
                logger.debug(f"🛑 OutputLeadBuffer dropping {len(self._held)} held frame(s) on interruption")
            self._drop()
            self._bot_speaking = False
            self._audio_this_segment = False
            self._last_out_ts = None  # next reply is cold by definition
            await self.push_frame(frame, direction)
            return

        # Pipeline teardown / restart (connection death mid-reply lands here as
        # CancelFrame, and recovery re-runs StartFrame): a reply that died
        # mid-hold must never leak its lead into the next reply.
        if isinstance(frame, (CancelFrame, StartFrame)):
            if self._held:
                logger.debug(f"🛑 OutputLeadBuffer dropping {len(self._held)} held frame(s) on {type(frame).__name__}")
            self._drop()
            self._bot_speaking = False
            self._audio_this_segment = False
            self._last_out_ts = None  # next reply is cold by definition
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._audio_this_segment = False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, OutputAudioRawFrame):
            now = time.monotonic()
            gap_is_cold = (self._last_out_ts is None
                           or (now - self._last_out_ts) >= self._idle_gap_s)
            self._last_out_ts = now

            # Cold reply-start: first audio, or audio after an idle gap — but a
            # gap does NOT count while the bot is mid-segment (Realtime TTS has
            # legitimate sub-second gaps inside one reply; re-holding there
            # would add LEAD_MS of latency to a warm device for nothing).
            mid_segment = self._bot_speaking and self._audio_this_segment
            if not self._buffering and gap_is_cold and not mid_segment:
                self._gen += 1
                self._buffering = True
                self._arm_cap_timer(direction)
            self._audio_this_segment = True

            if self._buffering:
                self._held.append(frame)
                self._held_bytes += len(frame.audio)
                if self._held_bytes >= self._lead_bytes(frame):
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
            self._bot_speaking = False
            self._audio_this_segment = False
            if self._held:
                await self._flush(direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
