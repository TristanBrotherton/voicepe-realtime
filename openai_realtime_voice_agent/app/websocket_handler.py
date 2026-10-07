"""WebSocket handler for managing WebSocket connections and pipelines."""
import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional, Callable, Awaitable, Dict

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame, EndFrame, ErrorFrame,
)
from pipecat.audio.utils import create_stream_resampler
from pipecat.services.openai.realtime import events as openai_rt_events

from app.device_registry import DeviceConnection, DeviceRegistry, device_id_from_websocket
from app.multi_client_transport import MixedFastAPIWebsocketTransport
from app.providers import (
    OPENAI,
    bana0_hit,
    bana0_miss,
    drop_pending_input_audio,
    input_sample_rate,
    supports_client_events,
)
from app import bana0

# US-018: well under EARLY_ACK_SILENCE_MS (1500 default, 2000 on core), counted
# from the same moment, so the honest line wins the slot over "Ett ögonblick."
OFFLINE_PROBE_S = 1.0
# After a hit the model confirms in its own words; past this, the cached "Klart.".
OK_VANTA_S = 2.5
from app.raw_audio_serializer import RawAudioSerializer
from app.session_manager import SessionManager
from app.audio_recording_service import AudioRecordingService
from app.phase_emitter import PhaseEmitter
from app.output_lead_buffer import OutputLeadBuffer
from app.transcript_logger import TranscriptLogger

logger = logging.getLogger(__name__)

# The OpenAI Realtime API works in 24 kHz PCM16. The Voice PE firmware plays
# 24 kHz back and streams 16 kHz up. IMPORTANT: pipecat 0.0.97's websocket INPUT
# transport does NOT resample (only the OUTPUT transport does), and OpenAI
# Realtime's pcm16 input rate is hard-locked to 24000 (PCMAudioFormat.rate =
# Literal[24000]) — you cannot tell it the audio is 16 kHz. So the device's
# 16 kHz frames would be read 1.5x too fast / pitched up, garbling the whole
# transcript. The InputResampler below upsamples 16k->24k in the pipeline.
#
# Gemini Live has no such lock -- it takes the device's native 16 kHz, so its
# declared input rate is app.providers.input_sample_rate("gemini") instead of
# this constant. PIPELINE_SAMPLE_RATE remains the OUTPUT rate for both
# engines (what the device already plays at) and OpenAI's own input rate.
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
    """Nurse whichever engine this connection is running, the way IT needs.

    pipecat 0.0.97's OpenAIRealtimeLLMService has NO reconnect logic: when the
    OpenAI WS drops (1011 keepalive ping timeout, 1001 going away on the 60-min
    cap, 1006, or any send/receive failure) it treats the send error as fatal and
    floods ErrorFrame — ~15/s, one per forwarded mic frame — forever. The single
    persistent session is then dead until the add-on restarts, so the device gets
    no answer to any further turn (observed live: a 1011 flood after which the
    next question got silence). Gemini's service is the opposite: it has its own
    `_reconnect`, `_handle_connection_error` and session resumption, so calling
    `reset_conversation` on it would just be a second hand fighting the first.

    This processor watches the ErrorFrames as they travel upstream to the task
    source and hands every one of them to `handle_error`, which asks two
    SEPARATE questions (see its docstring): does the router need to hear about
    this (`app.provider_failures.classify`, `app.provider_router.ProviderRouter`
    — this is what lets a money/auth failure fail the connection over even
    though nothing here can repair it), and is OUR connection specifically
    dead enough that repairing it is worth doing. Only the second question can
    lead to:
      3. repairing this engine's session in place — emit `idle` to unstick the
         device (LED + mic reset) and call service.reset_conversation() — the
         one PUBLIC method that does _disconnect() + _connect() + re-sends the
         session config (instructions, tools, turn detection). No pipeline
         rebuild: the running pipeline keeps the same service object, which is
         exactly the one reset_conversation reconnects.
    Answering "no" to both (our own tool's fault, an engine that heals itself,
    a rate limit, anything else that isn't a dead socket) means doing nothing
    but the plain idle-unstick below. A guard + cooldown collapse an error
    flood into a single report/reconnect attempt, retrying at most every
    RECONNECT_COOLDOWN_S while the link stays down.
    """

    # Substrings that mark a dead/closed OpenAI websocket (vs. a rate limit or
    # an app-level error, neither of which we must reconnect on). These appear
    # on the SEND-side flood ("Error sending client event: …" — pipecat's own
    # OpenAIRealtimeLLMService._send_client_event is the only place that
    # phrase is ever produced, so it always means OUR send to OpenAI failed).
    # Several of these strings are ALSO in app.provider_failures._TRANSIENT
    # (they answer classify()'s different question — "should the router hear
    # about this at all" — not this one), so the two lists are related but
    # deliberately not merged; see the note beside _TRANSIENT there.
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
    # session hit the maximum duration of 60 minutes.") with NO send-flood and
    # NO close-code marker — so _DEATH_MARKERS above misses it and the session
    # stays dead until the add-on restarts. These markers force a reconnect on
    # their own; they can only come from OpenAI, not a device close.
    _SESSION_DEAD_MARKERS = (
        "session_expired",
        "maximum duration",
    )
    # The third signature this gate matches: the OpenAI READ side died or
    # ended (network drop / silent server close). pipecat produces no
    # ErrorFrame for these at all — SafeRealtimeLLMService wraps the receive
    # loop and reports them as "realtime receive loop …" (checked directly in
    # _is_dead_socket, not stored as a constant since it is a single phrase).
    RECONNECT_COOLDOWN_S = 5.0
    IDLE_UNSTICK_COOLDOWN_S = 2.0
    # A rate limit inside this long after our own retry is the retry failing.
    RATE_LIMIT_WINDOW_S = 30.0
    # Proactive refresh: reconnect BEFORE OpenAI's 60-min session cap, but only
    # while the house is genuinely quiet, so the cap practically never lands
    # mid-conversation (where it costs the user a turn).
    REFRESH_AGE_S = 55 * 60   # refresh once the session is this old
    REFRESH_QUIET_S = 60.0    # ... and no mic audio flowed for this long
    REFRESH_CHECK_S = 60.0    # poll cadence of the background check

    def __init__(self, openai_service, emit_idle=None, phase_emitter=None,
                 provider="openai", router=None, on_failover=None, **kwargs):
        super().__init__(**kwargs)
        self._service = openai_service
        self._emit_idle = emit_idle  # async callable(value:str), this device's send_phase
        # Preferred idle route: PhaseEmitter.force_idle() keeps the emitter's
        # phase state consistent AND suppresses the racing `thinking` from VAD
        # stop events still in flight (observed: a raw broadcast idle was
        # overridden 400 ms later and the device sat in `thinking` with an
        # open mic for 44 s). emit_idle stays as fallback wiring.
        self._phase_emitter = phase_emitter
        # The engine THIS connection's service actually is (resolved once, in
        # serve_connection, before the transport even existed) — the only
        # thing that lets handle_error tell an OpenAI dead-socket apart from a
        # Gemini one that heals itself. Defaults to "openai" so every existing
        # direct construction (tests included) keeps today's behaviour.
        self._provider = provider
        # The same ProviderRouter the application holds, or None in any test
        # (and any caller) that hasn't wired one in — handle_error then just
        # repairs in place and never fails over, which is today's behaviour.
        self._router = router
        # Async callable(), no arguments — rebuilds this connection on the
        # engine the router just switched to (see make_failover). Wired to
        # that in serve_connection's real pipeline; still defaults to None so
        # any test or caller that constructs ConnectionRecovery directly
        # without one just logs a failover decision without carrying it out.
        self._on_failover = on_failover
        self._reconnecting = False
        # Stamped ONLY when a repair (reset_conversation) is actually
        # attempted — by handle_error's dead-socket branch, force_reconnect,
        # or the proactive refresh loop. Deliberately NOT stamped by every
        # error handle_error sees: a rate limit or any other non-repairing
        # failure must not delay force_reconnect's wedge detector, which
        # gates on this same field, by up to RECONNECT_COOLDOWN_S for no
        # reason.
        self._last_attempt = 0.0
        # The text of the last error handle_error actually processed (not
        # deduped), paired with _last_reported_at below, so a flood of
        # identical messages (a dead socket delivers the same one ~15x/s)
        # collapses into a single router report/reconnect per
        # RECONNECT_COOLDOWN_S instead of one per frame. Kept separate from
        # _last_attempt above precisely so a message that never repairs still
        # gets deduped without touching the repair-cooldown clock.
        self._last_error_message = None
        self._last_reported_at = 0.0
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
        self._sov_task = None
        self._vakna_task = None
        # Last wake of this device (providers/sovlage.py): the sleep timer counts from it too.
        self._last_wake = time.monotonic()
        self._closed = False
        # Rate-limit ladder for the current turn: 0 nothing yet, 1 answer
        # retried, 2 fallback said. Reset by a finished turn or by time.
        self._rate_limit_stage = 0
        self._rate_limit_at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._proactive_refresh_loop())
        if self._sov_task is None and getattr(self._service, "sover", None) is not None:
            self._sov_task = asyncio.create_task(self._sov_loop())
        if isinstance(frame, StartFrame) and self.vakna_vid_start:
            self.vakna_vid_start = False
            self._vakna_task = asyncio.create_task(self._vakna_efter_start())
        if isinstance(frame, InputAudioRawFrame):
            # Only kept for the proactive-refresh "is anyone interacting?" check.
            # (Stale-audio clearing is now done at the cut-off source — the device
            # sends {"type":"flush"} when a follow-up window times out — not
            # reactively on mic-resume, which disturbed the VAD and caused garbage.)
            self._last_input_audio = time.monotonic()
        if isinstance(frame, ErrorFrame) and getattr(self._service, "sover", False):
            # Asleep (providers/sovlage.py): the socket was closed on purpose.
            # Nothing to report to the router and nothing to reconnect.
            logger.debug(f"💤 error while asleep ignored: {str(getattr(frame, 'error', ''))[:80]}")
        elif isinstance(frame, ErrorFrame) and not self._reconnecting:
            msg = str(getattr(frame, "error", "") or "")
            # EVERY error goes through handle_error now — including the ones
            # that used to fall straight to the plain idle-unstick below (a
            # rate limit, an out-of-money account, our own tool's failure).
            # Out-of-money in particular arrives as an ordinary error with
            # none of the close-socket signatures this comment block used to
            # gate on, so a check that only reconnected on those signatures
            # would never even ask the router about the very failure the
            # router exists to fail over. handle_error is what now tells a
            # dead OpenAI socket, an engine that heals itself (Gemini), a
            # permanent failure and our own bug apart — see its docstring.
            self._recover_task = asyncio.create_task(self._route_error(msg))
        await self.push_frame(frame, direction)

    def _is_dead_socket(self, message: str) -> bool:
        """Whether this message is one of the three signatures that
        specifically mean OUR OpenAI connection is closed.

        classify() below answers a different, broader question ("should the
        router hear about this") and returns TRANSIENT for a rate limit and
        for anything it does not recognise, not only for a dead socket — so
        it cannot double as this gate. Only a genuine connection-death
        signature is worth calling reset_conversation() over. Three SEPARATE
        conditions, restored to their exact pre-Task-7 shape (see
        `git show c54eb29:app/websocket_handler.py`):

          (a) the OpenAI send-side flood ("Error sending client event: …" + a
              close-code marker) — OUR WS died mid-send. The "client event"
              pairing is required: without it, some unrelated error that
              merely happens to mention a close code (e.g. a normal
              DEVICE-side disconnect, also possibly 1011/ConnectionClosed)
              could tear down a perfectly working OpenAI session.
          (b) an unambiguous OpenAI session-dead error event (session_expired
              / "maximum duration") — the 60-min cap surfacing as a proactive
              error event with NO send-flood, so (a) misses it. It can only
              come from OpenAI, not a device close, so it needs no pairing.
          (c) the OpenAI READ side died or ended ("realtime receive loop …",
              from SafeRealtimeLLMService) — pipecat produces no ErrorFrame
              for this at all otherwise. Also OpenAI-only, no pairing needed.
        """
        send_flood = "client event" in message and any(
            marker in message for marker in self._DEATH_MARKERS
        )
        session_dead = any(marker in message for marker in self._SESSION_DEAD_MARKERS)
        reader_dead = "realtime receive loop" in message
        return send_flood or session_dead or reader_dead

    async def handle_error(self, message: str) -> bool:
        """Decide what one error means and do that.

        Two SEPARATE questions, not one:
          - Does the router need to hear about this? Yes, always (unless it
            is our own bug) — this is what lets a money/auth failure fail the
            connection over even though nothing here is capable of repairing
            it.
          - Is OUR connection actually dead, so repairing it is worth doing?
            Only when `_is_dead_socket` says so. classify() returns TRANSIENT
            for a rate limit and for anything it does not recognise too —
            neither means the socket is closed, and reset_conversation() on a
            merely-rate-limited (but otherwise fine) session would rebuild a
            working connection for nothing.

        Returns:
            True if a repair or a failover was attempted, OR if this is a
            connection-death message arriving while a repair already made
            within RECONNECT_COOLDOWN_S covers it -- a duplicate of one, or
            the same socket death worded differently (the caller need do
            nothing more in any of those cases). False if this
            error needed no action from us — our own tool, a terminal
            failure with nowhere to go, an engine that heals its own
            connection, a reported-but-not-dead-socket failure (a rate
            limit, say), a failover decision with no callback yet to carry
            it out, or a duplicate of a non-death message — in which case
            the caller still owes the device an idle nudge so a stuck turn
            does not sit forever.
        """
        from app.provider_failures import Failure, classify
        from app.providers import self_heals

        failure = classify(message)
        if failure is Failure.APP:
            return False
        if failure is Failure.RATE_LIMIT:
            # Before the flood dedup: the retry's own failure may read the
            # same, and it must still reach the fallback. Never reported to
            # the router: tokens-per-minute is not a broken engine.
            return await self._answer_after_rate_limit(message)

        now = time.monotonic()
        # A dead/flooding socket delivers the SAME message ~15x/s. Without
        # this, every one of those calls re-reports to the router: strikes
        # accrue as if each were an independent failure (switching engines
        # after the first *detected* drop, not the second *real* one), and
        # once the retry budget is exhausted a fresh WARNING logs every
        # ~66ms. One report per RECONNECT_COOLDOWN_S — the window a reconnect
        # attempt already waits out, not a second number — is enough; the
        # same message recurring after the window elapses is still reported.
        # Keyed by _last_reported_at, NOT _last_attempt: _last_attempt only
        # moves when a repair is actually attempted (see __init__), so a
        # duplicate that never repairs (a repeated rate limit, say) would
        # otherwise never be recognised as a duplicate at all.
        duplicate = (
            message == self._last_error_message
            and now - self._last_reported_at < self.RECONNECT_COOLDOWN_S
        )
        if duplicate:
            # A duplicate DEAD-SOCKET message must report back "handled"
            # (True), not "nothing to do" (False): False would make
            # _route_error call the plain idle-unstick, which routes through
            # PhaseEmitter.force_idle() and sets _suppress_thinking — and if
            # the earlier repair already succeeded and the user has since
            # started a fresh turn, this straggler frame would clobber that
            # turn's phase (the exact race documented at
            # app/phase_emitter.py:35-48). A duplicate NON-death message (a
            # repeated rate limit) still needs its idle nudge, so it still
            # returns False, gated by IDLE_UNSTICK_COOLDOWN_S as before.
            return self._is_dead_socket(message)
        self._last_error_message = message
        self._last_reported_at = now

        # A dead socket on an engine we repair ourselves is reported only if
        # the repair fails. OpenAI closes healthy idle sessions server-side
        # ("realtime receive loop died: ConnectionClosedError(None, None,
        # None)") and they reconnect in ~1.5 s; on 2026-10-02 two of those 23
        # minutes apart spent the retry budget and moved the house to a deaf
        # backup. Same rule ResilientGeminiLiveService applies to Gemini's
        # idle hang-ups.
        repair_first = (
            failure is Failure.TRANSIENT
            and not self_heals(self._provider)
            and self._is_dead_socket(message)
        )
        if not repair_first and await self._report(message):
            return True

        if failure in (Failure.MONEY, Failure.AUTH):
            # Nothing here can be repaired, and there is nowhere to go.
            return False

        if self_heals(self._provider):
            # This engine reconnects on its own; a second hand on the wheel
            # only fights it.
            logger.debug(f"{self._provider} repairs its own connection — standing back")
            return False

        if not self._is_dead_socket(message):
            # Reported above (so money/auth/transient-budget accounting still
            # happened); not a connection-death signature, so there is
            # nothing here worth reconnecting over.
            return False

        # RESTORED (final review): the repair cooldown this path had before
        # the provider work, and which the class docstring above never
        # stopped promising. It is NOT the same guard as the message-dedup
        # further up, and both are needed:
        #   - the dedup collapses the SAME string repeating ~15x/s (one
        #     router report per window),
        #   - this collapses DIFFERENT death messages arriving back to back
        #     into ONE repair.
        # One socket death produces both: the reader dies ("realtime receive
        # loop died: …") -> repair #1, and when that finishes the queued
        # sends surface the same event as a different string ("Error sending
        # client event: sent 1011 …"). The dedup cannot see those two as
        # duplicates, so without this cooldown the second one repairs again
        # immediately -- and every repair now also re-seeds the conversation.
        if now - self._last_attempt < self.RECONNECT_COOLDOWN_S:
            # Report "handled" (True), not "nothing to do": a repair just
            # happened, so the caller must NOT also nudge idle -- that
            # straggler would clobber the phase of a turn the user has since
            # started (the race documented at app/phase_emitter.py:35-48,
            # and the same reason the duplicate branch above returns True
            # for a dead socket).
            logger.debug(
                f"repair already attempted <{self.RECONNECT_COOLDOWN_S:.0f}s ago — "
                f"not repairing again for: {message[:90]}"
            )
            return True

        self._reconnecting = True
        self._last_attempt = now  # only stamped here, where a repair is actually attempted
        if not await self._recover(message):
            # The engine did not come back: THAT is a strike.
            await self._report(f"reconnect failed after: {message}")
        return True

    async def _report(self, message: str) -> bool:
        """Tell the router; carry out a switch if it decides one.

        Returns True only when a failover was actually carried out.
        """
        if self._router is None:
            return False
        after = await self._router.report_failure(self._provider, message)
        if after == self._provider:
            return False
        logger.warning(f"🔀 failing over {self._provider} → {after}")
        if self._on_failover is None:
            # No callback wired (a test-built ConnectionRecovery, say):
            # claiming "handled" would suppress the idle nudge while nothing
            # rebuilds the connection.
            return False
        await self._on_failover()
        return True

    async def _answer_after_rate_limit(self, message: str) -> bool:
        """Wait what OpenAI asks, ask for the answer once more, else say so.

        2026-10-02 12:57: HassTurnOff ran, the reply after it was rate
        limited, and the user heard nothing. The tool result is already in
        the server conversation, so a fresh response.create is the answer.

        Returns True when something audible is on its way (retry or
        fallback), False when the caller should just nudge the device idle.
        """
        from app.provider_failures import retry_after_s

        retry = getattr(self._service, "retry_response", None)
        if retry is None:
            return False  # an engine without a retry hook: idle nudge as before
        now = time.monotonic()
        if now - self._rate_limit_at > self.RATE_LIMIT_WINDOW_S:
            self._rate_limit_stage = 0
        self._rate_limit_at = now
        if self._rate_limit_stage == 0:
            self._rate_limit_stage = 1
            wait = retry_after_s(message)
            logger.warning(f"⏳ rate limited — asking for the answer again in {wait:.1f}s")
            await asyncio.sleep(wait)
            try:
                await retry()
                return True
            except Exception as e:
                logger.warning(f"⚠️ rate-limit retry could not be sent: {e!r}")
        if self._rate_limit_stage < 2:
            self._rate_limit_stage = 2
            say = getattr(self._service, "say_rate_limited", None)
            logger.warning(
                "🙊 still rate limited after the retry — "
                + ("saying so" if say else "no audio path, going idle")
            )
            if say is not None:
                try:
                    await say()
                    return True
                except Exception as e:
                    logger.warning(f"⚠️ rate-limit fallback could not be sent: {e!r}")
        return False

    def note_turn_success(self) -> None:
        """The assistant just finished a reply cleanly.

        Wired from PhaseEmitter's debounced real "idle" (a genuinely
        completed turn, never the turn-death `force_idle` path) rather than
        read off a frame here directly: BotStoppedSpeakingFrame is produced
        by the engine's service, which sits DOWNSTREAM of this processor in
        the pipeline, so it never flows back through here on its own — unlike
        an ErrorFrame, which services push upstream on purpose. The engine
        being healthy enough to finish a reply is exactly "a turn just
        finished well": forget any earlier hiccup charged against it. Without
        this, one hiccup today and an unrelated one next month would count as
        two-in-a-row and switch engines for nothing — the one-retry budget
        must reset on real success, not just with the passage of time.
        """
        self._rate_limit_stage = 0
        if self._router is not None:
            self._router.note_success(self._provider)

    async def _route_error(self, msg: str) -> None:
        """Run handle_error, then fall back to the plain idle-unstick if it
        decided there was nothing FOR IT to do.

        A stuck device (LED blinking `thinking`, mic still open) is the
        failure mode this exists to prevent — handle_error only ever repairs
        or fails over, it never itself just nudges the UI, so whenever it
        stands back this is the only thing left that will.
        """
        acted = await self.handle_error(msg)
        if acted:
            return
        # Non-connection-death error that ENDS a turn without a reply:
        # most importantly an out-of-money/auth failure or a rate-limit, but
        # also our own tool's failure or an engine standing back to heal
        # itself. No bot speech was produced, so PhaseEmitter never fires
        # BotStopped→idle; the device is left stuck in `thinking`
        # (LED keeps blinking) with no device-side watchdog to recover.
        # Emit one `idle` to unstick it so the user can just try again.
        # Guarded by a short cooldown so a rare flood collapses to one.
        now = time.monotonic()
        if now - self._last_idle_unstick >= self.IDLE_UNSTICK_COOLDOWN_S:
            self._last_idle_unstick = now
            await self._unstick_idle(msg)

    async def force_reconnect(self, reason: str) -> None:
        """Positive-liveness reconnect: for wedged (half-open) sockets that
        produce NO ErrorFrames at all — audio streams out, nothing comes back
        (observed live 2026-07-16: wake + speech after an idle gap → zero
        server events, no error, request lost).

        Only for an engine that cannot repair itself. handle_error already
        stands back for a self-healing one; this path did not, and it is the
        path the wedge detector uses — so on Gemini every quiet wake ran the
        whole repair: `_go_idle()` first (which is a real, audible phase
        reset on the device: the chime and the LED, mid-conversation), then
        `❌ service has no reset_conversation()` and a give-up, leaving a
        session pipecat was already reconnecting on its own. Observed live
        2026-09-09, three times in ten minutes. The engine table has said
        `_SELF_HEALS[GEMINI] = True` since the provider work landed; this is
        the second place that finally reads it.
        """
        from app.providers import self_heals

        if self_heals(self._provider):
            logger.debug(
                f"🧟 wedge repair skipped — {self._provider} reconnects its own socket "
                f"({reason[:60]})"
            )
            return
        now = time.monotonic()
        if self._closed or self._reconnecting or now - self._last_attempt < self.RECONNECT_COOLDOWN_S:
            return
        if getattr(self._service, "sover", False):
            return  # asleep on purpose: the next wake connects
        self._reconnecting = True
        self._last_attempt = now
        self._recover_task = asyncio.create_task(self._recover(reason))
        if await self._recover_task and self._router is not None:
            # This repair never went through handle_error/report_failure (a
            # wedge is detected positively, not from an ErrorFrame), so there
            # is no strike count here to protect from being reset too early —
            # unlike the reactive path below, crediting it immediately is safe.
            self._router.note_success(self._provider)

    async def _recover(self, reason: str) -> bool:
        """Reconnect the service in place. Returns whether it worked.

        Deliberately NOT the thing that resets the router's retry-strike
        budget when called from handle_error's reactive path: that path just
        reported THIS failure to the router (accruing a strike precisely so a
        second one soon after is recognised as "still broken", not a fresh
        first strike) — crediting the repair back immediately, in the same
        breath, would erase that strike before a second occurrence of the
        SAME problem ever had a chance to be counted, and the budget would
        never do its job. The two callers that are NOT reporting a failure at
        all (force_reconnect's wedge repair, and the proactive refresh below)
        credit a success themselves once this returns True — a repair with no
        strike to protect is safe to credit right away.
        """
        t0 = time.monotonic()
        age_s = t0 - self._connected_at
        try:
            logger.warning(
                f"🔌 OpenAI Realtime connection lost after {age_s:.0f}s "
                f"({reason[:90]}) — reconnecting…"
            )
            # Unstick the device first, regardless of how the reconnect goes.
            try:
                await self._go_idle(f"reconnect: {reason[:60]}")
            except Exception as e:
                logger.warning(f"⚠️ could not emit idle during recovery: {e!r}")
            reset = getattr(self._service, "reset_conversation", None)
            if reset is None:
                logger.error("❌ service has no reset_conversation(); cannot reconnect in place")
                return False
            await reset()
            if getattr(self._service, "_websocket", True) is None:
                # pipecat's _connect swallows a failed connect and leaves this
                # None; its "Error connecting" frame is dropped while we repair.
                logger.error("❌ OpenAI reconnect did not open a socket")
                return False
            self._connected_at = time.monotonic()
            logger.info(
                f"✅ OpenAI Realtime session reconnected in {self._connected_at - t0:.1f}s "
                f"(gap the user may have heard)"
            )
            return True
        except Exception as e:
            logger.error(f"❌ OpenAI reconnect attempt failed: {e!r}")
            return False
        finally:
            self._reconnecting = False

    async def _proactive_refresh_loop(self):
        """Refresh the OpenAI session BEFORE the 60-min cap, during real idle.

        The cap reconnect is recoverable (~3 s), but when it lands
        mid-conversation that turn hiccups. Refreshing proactively while
        nothing is happening means users practically never meet the cap.
        "Quiet" is double-checked: no assistant response in flight AND no mic
        audio for REFRESH_QUIET_S — so it can never fire during a turn, a
        reply, or an open follow-up window.

        OpenAI only. The 60-minute cap is OpenAI Realtime's; Gemini Live has
        session resumption and reconnects itself, and `_recover` cannot repair
        it anyway (no `reset_conversation`) — it would only emit a real idle
        phase at the device and then give up. Same rule as force_reconnect
        and handle_error: a self-healing engine gets no second hand on the
        wheel.
        """
        from app.providers import self_heals

        if self_heals(self._provider):
            logger.debug(
                f"⏭️ proactive session refresh skipped — {self._provider} has no "
                f"session cap to get ahead of"
            )
            return
        while True:
            try:
                await asyncio.sleep(self.REFRESH_CHECK_S)
                await self._maybe_proactive_refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ proactive refresh loop error: {e!r}")

    async def _maybe_proactive_refresh(self) -> None:
        """Refresh if the session is old, quiet, and not already mid-repair.

        Split out from the sleep loop above so this decision + success-credit
        path is directly testable without waiting on REFRESH_CHECK_S/
        REFRESH_AGE_S/REFRESH_QUIET_S — behaviour is unchanged either way.
        """
        if self._reconnecting or getattr(self._service, "sover", False):
            return
        now = time.monotonic()
        age = now - self._connected_at
        quiet = now - self._last_input_audio
        busy = getattr(self._service, "_current_assistant_response", None) is not None
        if not (age >= self.REFRESH_AGE_S and quiet >= self.REFRESH_QUIET_S
                and not busy and now - self._last_attempt >= self.RECONNECT_COOLDOWN_S):
            return
        self._reconnecting = True
        self._last_attempt = now
        logger.info(
            f"🔄 proactive session refresh (session {age/60:.0f} min old, "
            f"quiet for {quiet:.0f}s) — staying ahead of the 60-min cap"
        )
        refreshed = await self._recover("proactive refresh before the 60-min session cap")
        if refreshed and self._router is not None:
            # Not a failure report either (see _recover's docstring) — a
            # clean scheduled refresh is as good a proof of health as a
            # finished turn.
            self._router.note_success(self._provider)

    SOV_CHECK_S = 5.0

    VAKNA_TIMEOUT_S = 5.0
    # Set by serve_connection when this connection replaces one whose engine
    # was awake: the device reconnected mid-conversation, so wake at start.
    vakna_vid_start = False

    def note_wake(self) -> None:
        self._last_wake = time.monotonic()

    async def vakna(self) -> None:
        """The device woke: connect the engine if it sleeps (providers/sovlage.py).

        Bounded: the serializer waits on this, so a hung connect must not
        hold the device's frames (and its ping/pong) for more than a moment.
        """
        self.note_wake()
        vakna = getattr(self._service, "vakna", None)
        if vakna is None:
            return
        try:
            if await asyncio.wait_for(vakna(), self.VAKNA_TIMEOUT_S):
                self._connected_at = time.monotonic()
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ cloud connect on wake took over {self.VAKNA_TIMEOUT_S:.0f}s — going on")

    def _tal_sedan_vakning(self) -> bool:
        """A real utterance since this wake, not the open mic.

        The device streams the mic as soon as it wakes, silence included, so
        _last_input_audio moves without anyone speaking. PhaseEmitter.note_wake
        clears _speech_since_wake; UserStartedSpeaking sets it.
        """
        emitter = self._phase_emitter
        if emitter is not None and hasattr(emitter, "_speech_since_wake"):
            return bool(emitter._speech_since_wake)
        return self._last_input_audio > self._last_wake

    def _vakning_utan_tal(self, now: float) -> bool:
        """Wake nobody answered within VAKNA_TIMEOUT_S, whatever phase stuck."""
        return (
            now - self._last_wake >= self.VAKNA_TIMEOUT_S
            and not self._tal_sedan_vakning()
        )

    def _tyst_nog(self, now: float) -> bool:
        """Quiet for SOV_EFTER_S: no mic audio, no wake, device idle, no reply under way."""
        from app.providers.sovlage import sov_efter_s

        after = sov_efter_s()
        phase = getattr(self._phase_emitter, "phase", None) if self._phase_emitter is not None else None
        busy = getattr(self._service, "_current_assistant_response", None) is not None
        return (
            now - self._last_input_audio >= after
            and now - self._last_wake >= after
            and phase in (None, "idle")
            and not busy
            and not self._reconnecting
        )

    async def _vakna_efter_start(self):
        await asyncio.sleep(0.5)  # let the engine's own start() run first
        await self.vakna()

    async def _sov_loop(self):
        """Put the engine to sleep once the conversation has been quiet long enough."""
        while True:
            try:
                await asyncio.sleep(self.SOV_CHECK_S)
                if self._closed:
                    return
                if getattr(self._service, "sover", True):
                    continue
                from app.providers.sovlage import sov_efter_s
                if self._service.over_budget():
                    logger.warning("💸 cloud budget for today used — disconnecting now")
                    await self._service.sova("daily cloud budget used")
                elif self._service.over_maxtid():
                    # Even mid-sentence: the next wake word connects again.
                    logger.warning("⏱️ cloud session reached its maximum length — disconnecting")
                    await self._service.sova("maximum session length")
                elif self._vakning_utan_tal(time.monotonic()):
                    # Stuck in listening or replying must not keep the engine
                    # awake: nobody has spoken since this wake.
                    await self._service.sova(f"wake without speech for {self.VAKNA_TIMEOUT_S:.0f}s")
                elif self._tyst_nog(time.monotonic()):
                    await self._service.sova(f"quiet for {sov_efter_s():.0f}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ sleep loop error: {e!r}")

    async def close(self) -> None:
        """Stop background work owned by this pipeline processor."""
        self._closed = True
        tasks = (self._refresh_task, self._recover_task, self._sov_task, self._vakna_task)
        self._refresh_task = None
        self._recover_task = None
        self._sov_task = None
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
        try:
            logger.warning(f"⚠️ turn ended on error, emitting idle to unstick device ({reason[:90]})")
            await self._go_idle(f"turn ended on error: {reason[:60]}")
        except Exception as e:
            logger.warning(f"⚠️ could not emit idle after turn-ending error: {e!r}")


def make_failover(connection):
    """Build the action that moves one device to the other engine.

    A pipecat pipeline owns its service object; swapping that object while the
    pipeline runs is not something pipecat supports. So the pipeline is torn
    down instead. The device notices the closed socket and calls back within
    about a second -- the same path every add-on update already takes -- and
    the new session is built with whatever the router now says, with the
    conversation restored from the session cache.

    Tearing down means `connection.task.cancel()` -- exactly what
    `serve_connection`'s own `on_client_disconnected` handler already does.
    That cancellation is what makes `runner.run(task)` return, which runs
    `serve_connection`'s `finally` block: `_teardown()` (closes the recovery
    processor and phase emitter, disconnects the service, clears the
    connection's fields) and the registry/recording/callback bookkeeping. None
    of that is repeated here -- this function only pulls the one lever that
    starts it.

    Args:
        connection: The DeviceConnection to move.

    Returns:
        An async callable taking no arguments. Calling it more than once is
        harmless: several error frames arrive for the same death.
    """
    done = {"fired": False}

    async def failover():
        if done["fired"]:
            return
        done["fired"] = True
        # Unstick the device first, so the LED does not blink through the gap
        # and leave the user thinking it is still listening.
        try:
            await connection.send_phase("idle")
        except Exception as e:
            logger.warning(f"⚠️ could not emit idle before failover: {e!r}")
        task = getattr(connection, "task", None)
        if task is None:
            logger.warning("⚠️ no pipeline task to tear down for failover")
            return
        logger.warning(
            f"🔀 tearing down {connection.device_id}'s pipeline so it reconnects "
            f"on the other engine"
        )
        try:
            await task.cancel()
        except Exception as e:
            logger.error(f"❌ failover teardown failed: {e!r}")

    return failover


_GEMINI_TURN_CLOSE_FLAG = "_needs_turn_complete_message"


async def _send_gemini_note_silently(service, text: str) -> bool:
    """Append a system note to a Gemini Live session without an immediate reply.

    Returns True if the note was actually queued for the session, False if it
    was skipped (no session yet, or nothing to send after conversion).

    FIX ROUND 3: round 2 concluded no silent channel existed. Re-review found
    the bridge that makes one work, already built and already running:
    `_create_initial_response` (gemini_live/llm.py:1355-1384) calls

        await self._session.send_client_content(
            turns=messages, turn_complete=self._inference_on_context_initialization
        )
        ...
        if not self._inference_on_context_initialization:
            self._needs_turn_complete_message = True

    and `_handle_user_stopped_speaking` (gemini_live/llm.py:874-882) -- already
    wired into GeminiLiveLLMService.process_frame's UserStoppedSpeakingFrame
    branch, so it runs automatically on every REAL turn boundary the live
    pipeline produces -- checks that exact flag and silently closes the turn:

        if self._needs_turn_complete_message:
            self._needs_turn_complete_message = False
            # NOTE: without this, the model ignores the context it's been
            # seeded with before the user started speaking
            await self._session.send_client_content(turn_complete=True)

    Round 2's mistake was treating "no supported way to hook
    UserStoppedSpeakingFrame ourselves" as blocking -- it isn't, because we
    don't need to hook it: the service already hooks its own
    UserStoppedSpeakingFrame handling on every real turn. Setting
    `_needs_turn_complete_message = True` is a plain instance-attribute write
    (grep of the whole file: `_needs_turn_complete_message` appears at exactly
    these three sites -- init, this setter, that one checker/resetter -- so
    there is nothing else to coordinate with or race against). No subclassing,
    no monkeypatching, no new dispatch logic: the existing, live-running code
    does the closing call for us the next time the user's real speech ends.

    THE ONE REAL COST (say it plainly, not leave it to be discovered): the
    note is not spoken about the instant a voice is recognised -- it is
    folded into the model's awareness only when the CURRENT (or, if the probe
    fires after the user already stopped talking this turn, the NEXT) user
    turn completes and the model responds to it. That is the same
    "may not land until a later turn" characteristic this feature has always
    had on OpenAI (see the wiring comment below) -- just one turn-boundary
    later on Gemini, and never audible on its own.

    Structural guard: if a future pipecat renames or removes
    `_needs_turn_complete_message`, silently doing nothing here would be
    exactly the failure mode this whole task exists to end. So existence is
    checked explicitly and logged as an ERROR (not folded into the general
    try/except below) if missing, and the injection is skipped rather than
    guessed at.
    """
    if not hasattr(service, _GEMINI_TURN_CLOSE_FLAG):
        logger.error(
            f"⚠️ {type(service).__name__} has no `{_GEMINI_TURN_CLOSE_FLAG}` "
            f"attribute -- the silent turn-close bridge this code depends on "
            f"to tell Gemini who is speaking is gone (renamed or removed "
            f"upstream). Skipping the model-facing note rather than guessing "
            f"at a replacement."
        )
        return False

    session = getattr(service, "_session", None)
    if session is None:
        return False

    from pipecat.processors.aggregators.llm_context import LLMContext

    context = LLMContext(messages=[{"role": "system", "content": text}])
    adapter = service.get_llm_adapter()
    turns = adapter.get_llm_invocation_params(context).get("messages", [])
    if not turns:
        return False

    # ⛔ MEASURED IN THE HOUSE 2026-09-09, and it cost an afternoon. Sending
    # the note this way makes Gemini answer the user's next question in TEXT
    # ONLY: the reply is generated -- the transcript shows it, using the
    # person's name, so the note plainly arrived -- but no audio ever follows,
    # the device sits in `thinking`, and the 15 s watchdog forces it idle. The
    # assistant is mute. Turning the speaker probe off made the voice come
    # back immediately, on the very next utterance.
    #
    # The mechanism is the turn bookkeeping: the note opens a turn that the
    # user's own utterance then finishes, and the model treats the merged turn
    # as text. Reasoning about it offline said this was safe; the room said
    # otherwise, and the room wins.
    #
    # So Gemini does not get the note. The name still reaches the male-only
    # tool gate and the Home Assistant sensor -- neither goes through the
    # model -- so only the model's ability to address the person by name is
    # lost, and only while Gemini is the engine. OpenAI is untouched.
    logger.info(
        "🗣️ gemini: not telling the model who is speaking — sending the note "
        "leaves the reply text-only and the device mute (measured). The name "
        "still reaches the tool gate and the sensor."
    )
    return False


def make_speaker_note(connection, openai_service, probe=None):
    """Build the callback that tells the model who is speaking.

    FIX ROUND 1 found that pushing a frame through `context_aggregator.user()`
    never reaches either engine (push_frame forwards to `self._next`, past the
    aggregator's own dispatch; neither service's process_frame acts on
    LLMMessagesUpdateFrame anyway). OpenAI got its original direct
    `send_client_event` back; Gemini was routed through `LLMMessagesAppendFrame`
    -> `GeminiLiveLLMService._create_single_response`, which works -- but that
    method hardcodes `turn_complete=True`, so Gemini spoke an unprompted reply
    every single time a voice was recognised. Worse than the silent loss this
    task exists to fix.

    FIX ROUND 2 concluded no silent channel existed for Gemini and shipped a
    warn-and-skip instead. FIX ROUND 3 found that conclusion was wrong -- see
    `_send_gemini_note_silently`'s docstring for the bridge that makes
    `send_client_content(turn_complete=False)` actually reach the model
    without an unprompted reply.

    `probe` is the connection's SpeakerProbe; it is only read by verdict_text
    for the "not confidently matched" fallback message (to name the household
    members it might be), so tests exercising just the confident-match branch
    can omit it.
    """

    async def note(label, name, f0):
        # verdict_text carries every case this feature has been tuned for:
        # an unmatched voice ("likely a guest" -> stay neutral, no names), a
        # confident match (address the person by name), and an ambiguous
        # match (stay neutral but name the household candidates). None of
        # those cases are conditioned on `name` being truthy here -- the
        # guest and ambiguous branches deliberately fire with name=None, and
        # skipping the injection then would silently drop the very guidance
        # those branches exist to give the model.
        from .speaker_context import verdict_text

        text = verdict_text(probe, label, name, f0)

        if not supports_client_events(connection.provider):
            # Gemini: append silently via send_client_content(turn_complete=
            # False) + the service's own pending-turn-close bridge -- see
            # _send_gemini_note_silently's docstring. Its own structural
            # "does the bridge still exist" check logs an ERROR every time it
            # fails, deliberately not deduplicated -- a vanished bridge is a
            # library-compatibility break, not an expected steady-state
            # condition, and is exactly the kind of thing this task exists to
            # stop hiding after the first sighting. "No session yet" is not
            # an error (mirrors _create_single_response's own quiet `if not
            # self._session: return`) and is left unlogged for that reason.
            try:
                await _send_gemini_note_silently(openai_service, text)
            except Exception as e:
                logger.warning(f"⚠️ speaker verdict injection failed: {e!r}")
            return

        try:
            await openai_service.send_client_event(
                openai_rt_events.ConversationItemCreateEvent(
                    item=openai_rt_events.ConversationItem(
                        type="message",
                        role="system",
                        content=[openai_rt_events.ItemContent(
                            type="input_text",
                            text=text,
                        )],
                    )
                )
            )
        except Exception as e:
            logger.warning(f"⚠️ speaker verdict injection failed: {e!r}")

    return note


class WebSocketHandler:
    """Handles WebSocket transport initialization, pipeline building, and event management."""

    WEDGE_TIMEOUT_S = 12.0
    
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
        # Application.hamta_verktyg_vid_vakning. Called on the device wake,
        # before the cloud engine connects. None in tests that build a handler
        # by hand.
        self.hamta_verktyg = None
        # Set by main.py, alongside openai_service_factory, to the same
        # ProviderRouter the application holds. Used to pick the engine for a
        # brand-new connection's transport (before a session exists to ask),
        # and by connection recovery to fail a session over to the backup
        # engine. None (its default here, and in every test that builds a
        # WebSocketHandler directly) means "openai, no failover" -- unchanged
        # behaviour for anyone who hasn't wired a router in.
        self.router = None
        # Which device's audio the (single-file) recorder is following.
        self._recording_owner: Optional[str] = None
        # Voice enrollment remains single-user, but is explicitly targeted to
        # the connection that starts it.
        self.enrollment_recorder = None
        self.enrollment_conductor = None
        # Bana 0 (raawr US-016), set by main.py: the local STT's (host, port),
        # None = off; its timeouts in seconds (stt, comms); and the guarded
        # announcer `say(text, device_id)` that speaks HA's confirmation.
        self.bana0_stt: Optional[tuple[str, int]] = None
        self.bana0_timeouts: tuple[float, float] = (0.6, 4.0)
        # "Does this engine's API answer at all?" (main.probe_engine). Async.
        # Set by main; None = no offline line (US-018).
        self.engine_probe: Optional[Callable[[str], bool]] = None
        self._offline_tasks: set = set()
        # main._ack_clip: a phrase in the engine's own voice, cached on disk.
        self.ack_clip = None
        self.say = None
    
    def create_transport(
        self, websocket, serializer: RawAudioSerializer, provider: str = OPENAI
    ) -> MixedFastAPIWebsocketTransport:
        """Create a transport for one accepted connection.

        Args:
            websocket: The accepted FastAPI/starlette WebSocket.
            serializer: This connection's serializer.
            provider: The engine this connection's session will use. Sets the
                declared input rate to what that engine wants; the output rate
                stays PIPELINE_SAMPLE_RATE for both engines.

        Returns:
            A transport bound to that one connection.
        """
        return MixedFastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                serializer=serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=input_sample_rate(provider),
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
            context_initializer = self.session_manager.create_context_initializer(
                client_id, context_aggregator, openai_service, connection.provider or OPENAI
            )
        
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

        async def _request_follow_up():
            # The device's own message, documented in va_client.cpp: wait for
            # the reply's audio to drain, chime, open the mic. Sent only when
            # the model asked for it (see app/follow_up_tool.py).
            await connection.send_json({"type": "request_follow_up"})

        phase_emitter.set_follow_up_sender(_request_follow_up)

        # Direct end-of-turn wire, for the engine whose frame does not survive
        # the aggregator between it and PhaseEmitter. Optional by design: an
        # engine without this method keeps the frame-only path.
        set_turn_complete = getattr(openai_service, "set_turn_complete_handler", None)
        if set_turn_complete is not None:
            set_turn_complete(phase_emitter.note_engine_turn_complete)

        connection.phase_emitter = phase_emitter

        connection.recovery = ConnectionRecovery(
            openai_service=openai_service, emit_idle=send_phase,
            phase_emitter=connection.phase_emitter,
            # provider names the engine THIS connection's service actually is
            # (see the comment below) -- the only thing that lets handle_error
            # tell an OpenAI dead-socket apart from a Gemini one that heals
            # itself. router is the same ProviderRouter self.router points at,
            # so a failure reported here can move the NEXT connection's
            # current() -- or None in any test that builds a WebSocketHandler
            # directly, which keeps repair-in-place-only behaviour.
            # on_failover tears this connection's pipeline down so the device
            # reconnects on the engine the router just switched to -- see
            # make_failover's docstring for why a teardown+reconnect and not
            # an in-place service swap.
            provider=connection.provider or OPENAI, router=self.router,
            on_failover=make_failover(connection),
        )
        # connection.provider was decided once in serve_connection, before
        # the transport was even built, and create_service (which built
        # `openai_service` above) only ever reads that same value back --
        # never re-decides it -- so this is guaranteed to name the engine
        # `openai_service` actually is. For Gemini that's 16000 -- exactly
        # what the device already streams, so the resampler below sees
        # frame.sample_rate == out_rate and becomes a pass-through (see
        # InputResampler.process_frame).
        in_rate = input_sample_rate(connection.provider or OPENAI)
        pipeline_components = [
            transport.input(),
            # Watch for OpenAI connection-death ErrorFrames (they travel upstream
            # to the task source, so place this upstream of the service) and
            # reconnect in place. Without it a 1011/1001 drop bricks the session.
            connection.recovery,
            InputResampler(out_rate=in_rate),
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
        task = PipelineTask(pipeline, idle_timeout_secs=None, cancel_on_idle_timeout=False)

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
            _interrupt_kill_until["t"] = time.monotonic() + INTERRUPT_KILL_WINDOW_S
            # Arm the next-response kill on EVERY stop (see the flag comment):
            # the 1.5 s time-window alone misses responses that land later —
            # OpenAI replying to the spoken "stop", or a slow tool's answer.
            _kill_next_response["v"] = True
            try:
                sent = await drop_pending_input_audio(connection.provider, openai_service)
                logger.info(f"🛑 device interrupt → {sent} sent (drop in-flight user audio)")
            except Exception as e:
                logger.info(f"🛑 device interrupt → dropping input audio no-op ({e!r})")
            if supports_client_events(connection.provider):
                try:
                    if getattr(openai_service, "_current_assistant_response", None) is not None:
                        await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                        logger.info("🛑 device interrupt → response.cancel sent (response was still active)")
                    else:
                        logger.info("🛑 device interrupt → no active response to cancel (device already silenced)")
                except Exception as e:
                    logger.info(f"🛑 device interrupt → response.cancel no-op ({e!r})")
            else:
                logger.debug(
                    f"{connection.provider} takes no raw client events — "
                    f"leaving the interrupt to pipecat's own handling"
                )

        @openai_service.event_handler("on_conversation_item_created")
        async def _kill_racing_response(service, item_id, item):
            # Pipecat fires this for every conversation.item.added; only an
            # ASSISTANT item right after a device interrupt is the racing
            # response to the stop word the user just cancelled.
            if getattr(item, "role", None) != "assistant":
                return
            within_window = time.monotonic() < _interrupt_kill_until["t"]
            kill_armed = _kill_next_response["v"]
            if not within_window and not kill_armed:
                return
            # Consume the flag: this assistant item is the unwanted response the
            # user's stop pre-empted — a stop-acknowledgement ("Okay, I'll stop"),
            # a stopped tool's answer, or the cancelled reply's tail.
            _kill_next_response["v"] = False
            if supports_client_events(connection.provider):
                try:
                    await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                    logger.info(
                        "🛑 response raced in right after a device interrupt → "
                        "response.cancel (post-stop)"
                    )
                except Exception as e:
                    logger.info(f"🛑 post-interrupt racing-response cancel no-op ({e!r})")
            else:
                logger.debug(
                    f"{connection.provider} takes no raw client events — "
                    f"leaving the interrupt to pipecat's own handling"
                )

        async def _on_device_session_start():
            # va_client sends {"type":"start"} once per WebSocket CONNECTION
            # (on connect) — NOT per wake. A reconnect mid-utterance (wifi
            # blip, backend restart with session reuse) can leave half an
            # utterance in OpenAI's input buffer; start every (re)connection
            # with a clean one. The per-WAKE/follow-up stale-buffer case is
            # covered by the device's {"type":"flush"} on follow-up timeout.
            if supports_client_events(connection.provider):
                try:
                    await openai_service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
                    logger.info("🎬 device (re)connected → input_audio_buffer.clear (clean start)")
                except Exception as e:
                    logger.debug(f"🎬 connect-time input clear no-op ({e!r})")
            else:
                logger.debug(
                    f"{connection.provider} takes no raw client events — "
                    f"leaving the interrupt to pipecat's own handling"
                )

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
                sent = await drop_pending_input_audio(
                    connection.provider, openai_service, keep_speech=True
                )
                logger.info(f"🧽 follow-up cut-off → {sent} (drop partial utterance)")
            except Exception as e:
                logger.debug(f"🧽 mic-flush input drop no-op ({e!r})")

        async def _on_device_wake():
            # Tools first, then the cloud. A fetch that failed or came back
            # short is tried again here, on the service the wake is about to
            # connect. The serializer holds the mic audio until this returns
            # (providers/sovlage.py, raawr INKAST 2026-10-04).
            if self.hamta_verktyg is not None:
                try:
                    await self.hamta_verktyg(connection)
                except Exception as e:
                    logger.warning(f"⚠️ HA tools on wake failed: {e!r}")
            await connection.recovery.vakna()
            asyncio.create_task(
                self._wedge_check(connection, phase_emitter, time.monotonic())
            )
            # va_client sends {"type":"wake"} on every wake (start_session). Mark
            # the turn boundary for the dangling-VAD guard (A): until the user
            # actually speaks, a server-VAD end-of-turn is a stale pre-wake
            # segment closing late → suppress its thinking + cancel its garbage
            # response (handled in PhaseEmitter via the kill-window callbacks).
            phase_emitter.note_wake()
            # Only here, never on the mic flush: the early ack tells a woken
            # turn from a follow-up by this (TurnLiveness.from_wake).
            if connection.turn_liveness is not None:
                connection.turn_liveness.woke()
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
        # A cleanly finished reply is "a turn just finished well" for the
        # provider router's retry budget — see ConnectionRecovery.note_turn_success
        # and PhaseEmitter.set_turn_success_handler for why this has to be a
        # callback rather than a frame ConnectionRecovery sees directly.
        phase_emitter.set_turn_success_handler(connection.recovery.note_turn_success)

        # Bana 0 (raawr US-016), on either engine: at the end of every user
        # turn, try HA's own agent first; the model answers only on a miss.
        # OpenAI: main.py turned the server's create_response off for this.
        # Gemini: the service holds the turn's audio until bana 0 decides.
        # Up to 0.22.5 this was OpenAI only, so on Gemini "släck kontoret"
        # always went the slow way through the model (2026-10-02).
        provider = connection.provider or OPENAI
        if (
            self.bana0_stt is not None
            and serializer is not None
            and hasattr(openai_service, "on_user_turn_end")
        ):
            host, port = self.bana0_stt
            timeout_stt, timeout_comms = self.bana0_timeouts

            async def _say(text):
                """Bana 0's own lines, in the engine's voice from the disk cache (never the
                conductor's dry voice - the owner 2026-10-03: "hellre tyst än den torra")."""
                if self.say is None or self.ack_clip is None:
                    raise RuntimeError("no announcer wired")
                pcm = await self.ack_clip(provider, text)
                await self.say(text, client_id, pace=False, pcm=pcm)

            def _efter_miss():
                liveness = getattr(connection, "turn_liveness", None)
                if self.engine_probe is None or liveness is None:
                    return
                router = self.router
                engines = tuple(router.chain) if router is not None else (provider,)
                asked = time.monotonic()
                task = asyncio.get_running_loop().create_task(bana0.vakta_natet(
                    natet_nere=lambda: bana0.natet_nere(self.engine_probe, engines, OFFLINE_PROBE_S),
                    claim=lambda: liveness.claim_silence_ack(asked),
                    say=_say,
                ))
                # The loop keeps tasks weakly; hold it until it is done.
                self._offline_tasks.add(task)
                task.add_done_callback(self._offline_tasks.discard)

            def _efter_traff():
                liveness = getattr(connection, "turn_liveness", None)
                if liveness is None:
                    return
                asked = time.monotonic()

                # Gemini never hears a hit, so no model confirmation is coming.
                vanta = OK_VANTA_S if supports_client_events(provider) else 0.0
                task = asyncio.get_running_loop().create_task(bana0.vakta_bekraftelse(
                    vanta_s=vanta, claim=lambda: bana0.ingen_bekraftelse_an(liveness, asked),
                    say_ok=lambda: _say(bana0.OK_FALLBACK),
                ))
                self._offline_tasks.add(task)
                task.add_done_callback(self._offline_tasks.discard)

            async def _on_user_turn_end():
                bana = await bana0.tur(
                    serializer.take_turn_audio(),
                    stt=lambda pcm, t: bana0.transkribera(pcm, host, port, t),
                    timeout_stt=timeout_stt,
                    timeout_comms=timeout_comms,
                    skicka_svar_till_modellen=lambda text: bana0_hit(provider, openai_service, text),
                    efter_traff=_efter_traff,
                    skapa_svar=lambda: bana0_miss(provider, openai_service),
                    efter_miss=_efter_miss,
                )
                if bana == "bana0":
                    await phase_emitter.force_idle("bana0")

            openai_service.on_user_turn_end = _on_user_turn_end
            openai_service.on_user_turn_start = serializer.start_turn_audio
            serializer.is_replying = lambda: phase_emitter.phase == "replying"

        # Google dropped the socket with a turn in flight (1011, 2026-10-02
        # 14:30:47: the wake got nothing back). The engine reconnects on its
        # own; he gets told, through the add-on's own TTS lane, not silence.
        if hasattr(openai_service, "on_turn_lost"):
            from app.providers.gemini_live import TURN_LOST_LINE

            async def _on_turn_lost():
                try:
                    if self.say is not None:
                        await self.say(TURN_LOST_LINE, client_id)
                except Exception as e:
                    logger.warning(f"⚠️ could not say the turn was lost: {e!r}")
                await phase_emitter.force_idle("turn-lost")

            openai_service.on_turn_lost = _on_turn_lost

        if serializer is not None:
            serializer.set_interrupt_handler(_on_device_interrupt)
            serializer.set_session_start_handler(_on_device_session_start)
            serializer.set_mic_flush_handler(_on_device_mic_flush)
            serializer.set_wake_handler(_on_device_wake)

            # Speaker context v1 (fork): per-wake voice-type verdict → injected
            # straight into the LLM service (make_speaker_note). OpenAI gets
            # it immediately as a conversation item; Gemini gets it silently
            # appended via send_client_content(turn_complete=False) plus the
            # service's own pending-turn-close bridge, folded in only when
            # the model next responds to a real user turn -- see
            # make_speaker_note's and _send_gemini_note_silently's
            # docstrings for the full trace and that one-turn-later cost.
            # Out-of-band w.r.t. the audio path either way; on OpenAI it
            # lands ~2.5 s after the wake, so the FIRST reply of a turn may
            # not have it yet — follow-ups and later turns do; on Gemini it
            # never lands before the current/next turn completes, by
            # construction. Gating of speaker-restricted tools does NOT
            # depend on this injection (see
            # SafeRealtimeLLMService.register_function in
            # app/providers/openai_realtime.py) and is
            # unaffected on both engines.
            if connection.speaker_probe is not None and connection.speaker_probe.enabled:
                connection.speaker_probe.on_verdict = make_speaker_note(
                    connection, openai_service, connection.speaker_probe
                )
                serializer.set_speaker_probe(connection.speaker_probe)

            if self.enrollment_recorder is not None:
                serializer.set_enrollment_recorder(self.enrollment_recorder)
            # Button-cancel shortly after a wake = user flagging a false
            # trigger: label the latest probe capture like mark_false_wake.
            async def _on_button_cancel():
                # Same rule as the mark_false_wake tool: the counter is the
                # part that matters and it needs no audio, so it is published
                # whether or not a capture was kept. This used to sit inside
                # one try with the listdir, so on a default install (no
                # recordings, no /share/voice-probes at all) the button
                # flagged nothing and the sensor never moved.
                from .speaker_context import mark_latest_probe_as_false_wake
                try:
                    marked = mark_latest_probe_as_false_wake()
                    logger.info(
                        f"🏷️ button-flagged false wake: {marked}" if marked
                        else "🏷️ button-flagged false wake (no capture kept)"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ button false-wake capture not marked: {e!r}")
                try:
                    from .ha_sensors import PUBLISHER
                    await PUBLISHER.false_wake()
                except Exception as e:
                    logger.warning(f"⚠️ false-wake counter not published: {e!r}")
            serializer.set_button_cancel_handler(_on_button_cancel)

            async def _on_first_audio():
                await connection.send_json({"type": "ack"})
            serializer.set_first_audio_handler(_on_first_audio)

            if self.enrollment_conductor is not None:
                async def _on_device_enroll_stopped():
                    if self.enrollment_conductor.device_id == connection.device_id:
                        await self.enrollment_conductor.stop()
                serializer.set_enroll_stopped_handler(_on_device_enroll_stopped)

        return pipeline, runner, task

    async def _wedge_check(
        self, connection: DeviceConnection, phase_emitter: PhaseEmitter, wake_mono: float
    ) -> None:
        """Reconnect a quiet wake only while its connection is still live."""
        await asyncio.sleep(self.WEDGE_TIMEOUT_S)
        if getattr(phase_emitter, "last_vad_mono", 0.0) >= wake_mono:
            return
        recovery = connection.recovery
        if recovery is None:
            return
        # force_reconnect stands back from an engine that heals itself, so on
        # Gemini this would log an alarming warning about a repair that never
        # happens. Checked here too, so the log stays honest. DeviceConnection
        # defaults its provider to "" (a connection built before the engine was
        # resolved, and every test that constructs one directly); OPENAI is the
        # same fallback ConnectionRecovery uses, so an unnamed engine keeps
        # today's behaviour rather than raising inside a background task.
        from app.providers import self_heals

        if self_heals(connection.provider or OPENAI):
            return
        logger.warning(
            "🧟 no server VAD activity %.0fs after wake — presuming a "
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
            "audio_out": "pcm",
            "follow_up_ms": self.follow_up_ms,
            "follow_up_open_delay_ms": self.follow_up_open_delay_ms,
            "wake_open_delay_ms": self.wake_open_delay_ms,
            "playback_prebuffer_ms": self.playback_prebuffer_ms,
        }

    async def _publish_provider_status(self, connection: DeviceConnection, status: dict) -> None:
        """Send the provider-sensor HTTP publish to Home Assistant.

        Runs as a background task (see serve_connection), never awaited by
        the connection setup path: the file's own rule is that a sensor
        which cannot be written must never stop a conversation, and an
        awaited publish sitting in front of create_transport would make a
        slow or unreachable supervisor delay every new connection by up to
        the httpx timeout. Wrapping the call in its own try/except -- rather
        than relying on asyncio's default "Task exception was never
        retrieved" handling -- means a failure here is still logged, just
        never blocking.

        The `finally` clears `connection.provider_status_task` once this is
        actually done, so nothing keeps a finished task referenced forever;
        the identity check is so a second connection's (never possible here,
        since each connection gets its own task, but cheap insurance) or a
        stale reference can't be cleared out from under a newer one.
        """
        try:
            from .ha_sensors import PUBLISHER
            await PUBLISHER.provider(status)
        except Exception as e:
            logger.debug(f"provider sensor failed: {e!r}")
        finally:
            if connection.provider_status_task is asyncio.current_task():
                connection.provider_status_task = None

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
        await websocket.accept()
        device_id = device_id_from_websocket(websocket)
        logger.info(f"🔗 device {device_id} connected ({len(self.devices) + 1} total)")

        serializer = RawAudioSerializer(device_id)
        connection = DeviceConnection(
            device_id=device_id, websocket=websocket, serializer=serializer
        )
        # Decide the engine for this ENTIRE connection right here, once, if
        # main.py wired a router in. This is the only read of self.router
        # for this connection: the transport needs the answer immediately
        # (to declare the mic rate), and create_service below reads it back
        # off connection.provider rather than asking the router again -- a
        # second read, taken after the pipeline lock and an awaited MCP
        # tool-schema fetch, could come back different if another
        # connection's failure landed in that real gap, splitting the
        # transport's declared rate from the engine actually built.
        provider = self.router.current() if self.router is not None else OPENAI
        connection.provider = provider
        # Report which engine is live, right here and not in create_service:
        # status() calls current() again internally, and the comment above
        # exists precisely because a second router read taken after the
        # pipeline lock / MCP tool-schema fetch can disagree with this one.
        # Calling it back-to-back with the read above, with no await between
        # them, cannot land in that gap -- nothing else can run first. The
        # status dict is computed synchronously right here for that reason;
        # only the HTTP publish itself (the part that can be slow) is
        # deferred to a background task, so a slow/unreachable HA supervisor
        # can never delay create_transport below or the session it builds.
        if self.router is not None:
            try:
                # Held on the connection, not discarded: asyncio only keeps a
                # weak reference to a running task, so nothing else holding
                # this one could let it be garbage-collected mid-publish.
                # Per-connection (not on self) so a second device's task can
                # never clobber this one's; _publish_provider_status clears
                # it itself once done, so nothing holds a finished task
                # forever.
                connection.provider_status_task = asyncio.create_task(
                    self._publish_provider_status(connection, self.router.status())
                )
            except Exception as e:
                # Computing status() itself is synchronous and could in
                # principle raise (e.g. a router double in a test that
                # doesn't implement it) -- same rule as the publish itself:
                # never let this stop the connection being built.
                logger.debug(f"provider sensor failed: {e!r}")
        connection.transport = self.create_transport(websocket, serializer, provider)

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
            # The device reconnected mid-conversation (Wi-Fi blip): the new
            # engine would start asleep and drop the next follow-up.
            if displaced is not None and getattr(displaced.openai_service, "sover", True) is False:
                connection.recovery.vakna_vid_start = True
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
        if connection.task is not None:
            try:
                await connection.task.cancel()
            except Exception as e:
                logger.debug(f"task cancel for {connection.device_id}: {e!r}")
        if connection.ha_tools_task is not None:
            connection.ha_tools_task.cancel()
            connection.ha_tools_task = None
        recovery = connection.recovery
        if recovery is not None:
            await recovery.close()
        phase_emitter = connection.phase_emitter
        if phase_emitter is not None:
            await phase_emitter.close()
        service = connection.openai_service
        if service is not None:
            # sova() is not the only close: a displacing reconnect and cleanup
            # land here too. bokfor() clears the clock, so a session that
            # already slept is not counted twice.
            bokfor = getattr(service, "bokfor", None)
            if bokfor is not None:
                try:
                    bokfor()
                except Exception as e:
                    logger.warning(f"⚠️ cloud budget not recorded for {connection.device_id}: {e!r}")
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
