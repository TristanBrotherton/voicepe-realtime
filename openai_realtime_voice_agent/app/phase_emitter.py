"""Emit va_client phase messages from Pipecat speaking frames.

The Home Assistant Voice PE firmware (maxmaxme `va_client` component) drives its
LED ring, mic-streaming gate and a 7 s no-speech watchdog from `phase` JSON
messages sent by the backend:

    {"type": "phase", "value": "listening" | "thinking" | "replying" | "idle"}

Without these messages the device aborts each turn after the watchdog fires, so
emitting them is required (not just cosmetic). This processor maps Pipecat's
standard speaking frames onto those phases and forwards them to the device over
the websocket as TEXT frames.

Mapping:
    UserStartedSpeakingFrame  -> listening   (server VAD heard the user)
    UserStoppedSpeakingFrame  -> thinking    (generating a response)
    BotStartedSpeakingFrame   -> replying    (TTS audio is playing)
    BotStoppedSpeakingFrame   -> idle, but DEBOUNCED (see below)

IMPORTANT — idle debounce:
    OpenAI Realtime TTS arrives in segments (per sentence, and around tool
    calls), so BotStoppedSpeakingFrame fires several times *within a single
    reply*, with sub-second gaps before the next BotStartedSpeakingFrame. If we
    emitted "idle" on every BotStoppedSpeakingFrame the device's LED would flap
    replying -> idle -> replying mid-answer, and — because the firmware only
    arms the "stop" wake word during "replying" — the user would briefly lose
    the ability to interrupt. So we do NOT go idle immediately on BotStopped:
    we arm a short timer and only emit "idle" if no further bot/user speech
    starts before it elapses (i.e. the reply has truly finished). Any
    Bot/UserStartedSpeaking cancels the pending idle.

A barge-in mid-reply surfaces as a fresh UserStartedSpeakingFrame -> "listening"
(which cancels the pending idle); the firmware uses that to flush playback.

IMPORTANT — thinking watchdog + forced idle (v0.5.3):
    `thinking` is the one phase with no natural exit when a turn dies without
    a reply: a rate-limited / failed response.create produces no Bot frames,
    so the device blinks "thinking" forever WITH AN OPEN MIC (observed live
    2026-06-12: 44 s stuck, during which the mic picked up unrelated talking
    and the model answered it). Two defenses here:

    1. `force_idle(reason)` — the turn-death paths (ConnectionRecovery's
       rate-limit unstick + reconnect) call this INSTEAD of broadcasting idle
       around this processor, so the internal state stays consistent, AND it
       suppresses subsequent `thinking` emissions until real activity (user
       or bot speech) follows. Without the suppression, a VAD stop event
       already in flight re-emits `thinking` right after the unstick idle —
       the exact 400 ms race observed.
    2. A thinking watchdog — if `thinking` sees no model activity for
       THINKING_TIMEOUT_S it forces idle as a generic safety net (covers
       turn deaths that produce no ErrorFrame at all). While a tool call is
        in flight (tool handlers update this emitter's liveness signal through
       SafeRealtimeLLMService.register_function to tick it) the watchdog
       WAITS WITH NO CAP — explicit user decision 2026-06-12: a long web
       search on a hard question must get all the time it needs, the user
       knowingly waits. This cannot wait forever: every tool is bounded by
       its own client timeout (MCP ~30 s HTTP; web search the OpenAI
       client's 600 s default, whose except-path feeds the model a spoken
       error), and the wrapper's `finally` guarantees in_flight always
       drops back to 0 — after which the normal THINKING_TIMEOUT_S window
       applies again. If a slow-but-alive turn is ever cut off, the late
       reply still plays (BotStarted -> replying) — degraded but never stuck.
"""
import asyncio
import logging
import os
import time

from pipecat.frames.frames import (
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

logger = logging.getLogger(__name__)


class TurnLiveness:
    """"Is the model still doing something?" signal for one watchdog.

    Tool handlers are wrapped (see SafeRealtimeLLMService.register_function in
    main.py) to tick this on start/finish. The PhaseEmitter's thinking
    watchdog reads it so a slow tool — web search regularly takes 10-20 s with
    zero pipeline traffic — is never mistaken for a dead turn, and so each
    step of a long tool chain refreshes the window.
    """

    # How long before a tool started the model's own speech still counts as
    # "it already said something" for the early acknowledgement.
    RECENT_BOT_S = 2.0

    def __init__(self) -> None:
        self.in_flight = 0
        self.last_activity = 0.0
        # For the early acknowledgement (tool_registration.py): has the model's
        # own audio started, and has this turn already had its "jag kollar".
        self.bot_speaking = False
        self.bot_started_at = float("-inf")
        self.bot_stopped_at = float("-inf")
        self.acked = False
        # For the silence acknowledgement: a new utterance, or a turn that
        # ended (idle, force_idle) after the model was asked, calls it off.
        self.user_started_at = float("-inf")
        self.turn_over_at = float("-inf")
        # The device's {"type":"wake"} (websocket_handler._on_device_wake). A
        # turn with no wake since the last idle came through the follow-up
        # window. 0.0, not -inf: the first turn of a connection counts as woken.
        self.woke_at = 0.0
        # Tool runs this user turn, per tool name (loop guard, 0.25.1).
        self.tool_counts = {}

    def woke(self) -> None:
        self.woke_at = time.monotonic()

    def from_wake(self) -> bool:
        """This turn began with a wake word, not in a follow-up window."""
        return self.woke_at > self.turn_over_at

    def bot_started(self) -> None:
        self.bot_speaking = True
        self.bot_started_at = time.monotonic()

    def bot_stopped(self) -> None:
        self.bot_speaking = False
        self.bot_stopped_at = time.monotonic()

    def turn_over(self) -> None:
        self.acked = False
        self.turn_over_at = time.monotonic()

    def user_started(self, emulated: bool = False) -> None:
        """A real utterance: a new turn, with its own acknowledgement.

        An emulated start is pipecat turning a transcript into "user started
        speaking". On xAI and OpenAI the transcript of the turn just asked
        arrives ~1 s after it ended (xai 2026-10-02 20:29:56.2 -> 57.0), so it
        called off every silence ack before its 1.5 s were up. It counts only
        when no real start has opened this turn (Gemini on Google's own VAD,
        where the transcript is the only start there is).
        """
        if emulated and self.user_started_at > self.turn_over_at:
            return
        self.user_started_at = time.monotonic()
        self.acked = False
        self.tool_counts = {}

    def no_ack(self) -> None:
        """This stop is not a turn the model answers (dangling VAD, dead turn)."""
        self.acked = True

    def model_spoke_since(self, t: float, recent: float = None) -> bool:
        """The model's audio is playing, or played within `recent` s before `t`."""
        t -= self.RECENT_BOT_S if recent is None else recent
        return self.bot_speaking or self.bot_started_at > t or self.bot_stopped_at > t

    def claim_silence_ack(self, asked_at: float) -> bool:
        """True once per turn, when nothing happened since the model was asked.

        Nothing: no audio from the model, no new utterance, the turn not
        over -- and no acknowledgement yet (a slow tool may have had it).
        """
        if (self.acked or self.model_spoke_since(asked_at, 0.0)
                or self.user_started_at > asked_at or self.turn_over_at > asked_at):
            return False
        self.acked = True
        return True

    def claim_ack(self, tool_started_at: float) -> bool:
        """True once per turn, and never when the model is already talking."""
        if self.acked or self.model_spoke_since(tool_started_at):
            return False
        self.acked = True
        return True

    def tool_started(self) -> None:
        self.in_flight += 1
        self.last_activity = time.monotonic()

    def tool_finished(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.last_activity = time.monotonic()


class PhaseEmitter(FrameProcessor):
    """Forwards phase transitions to the device as JSON text frames."""

    # Thinking watchdog: how long `thinking` may sit without any model
    # activity before we declare the turn dead and force idle. Normal silent
    # gaps (turn end -> first token, tool result -> next response) are 1-4 s,
    # so 15 s has ample margin without leaving the user staring at a blinking
    # LED for long. While a tool is in flight the watchdog waits with NO cap
    # (see the module docstring — explicit user decision).
    THINKING_TIMEOUT_S = 15.0
    WATCHDOG_POLL_S = 1.0
    # How often to log that we're deliberately waiting on a running tool.
    INFLIGHT_LOG_EVERY_S = 30.0

    def __init__(self, send_phase, idle_debounce_s: float = None, liveness=None, **kwargs):
        """
        Args:
            send_phase: async callable(value: str) that delivers the phase to
                the connected device(s).
            idle_debounce_s: seconds the bot must stay silent after a reply
                before we declare the turn idle. Defaults to the
                PHASE_IDLE_DEBOUNCE_MS env var (1500 ms) — long enough to bridge
                the inter-sentence / tool-call gaps in OpenAI Realtime TTS so the
                LED and the "stop" wake word stay active for the whole answer.
        """
        super().__init__(**kwargs)
        self._send_phase = send_phase
        self._liveness = liveness or TurnLiveness()
        if idle_debounce_s is None:
            try:
                idle_debounce_s = float(os.environ.get("PHASE_IDLE_DEBOUNCE_MS", "1500")) / 1000.0
            except (TypeError, ValueError):
                idle_debounce_s = 1.5
        self._idle_debounce_s = max(0.0, idle_debounce_s)
        # How long past the debounce we will keep waiting for an engine that
        # has started a reply but not yet said the turn is over. See
        # _emit_idle_after_debounce. Capped so a missing end-of-turn signal
        # costs a slow idle, never a device stuck in "replying" forever.
        try:
            self._mid_turn_grace_s = float(
                os.environ.get("PHASE_MID_TURN_GRACE_MS", "8000")
            ) / 1000.0
        except ValueError:
            self._mid_turn_grace_s = 8.0
        # True from the first BotStartedSpeaking of a reply until the engine
        # pushes LLMFullResponseEndFrame (its own "that was the whole answer").
        self._model_turn_open = False
        # Set by the request_follow_up tool during a turn; consumed at the
        # engine's end-of-turn, which is the only moment the device can act on
        # it safely (see follow_up_tool.py).
        self._follow_up_wanted = False
        self._send_follow_up = None
        # Only wait on the mid-turn grace once this connection has actually
        # SEEN an end-of-turn. LLMFullResponseEndFrame does not always survive
        # the trip: LLMAssistantAggregator sits between the engine and this
        # processor and consumes it, which cost every Gemini turn an extra 8 s
        # in "replying" — the device stayed shut and the user had to repeat
        # himself (live 2026-09-09 19:04, every single turn). An engine that
        # never signals must behave exactly as it did before the grace existed.
        self._seen_engine_end = False
        self._idle_task = None
        self._watchdog_task = None
        self._current = None  # last phase actually sent, to dedupe redundant emits
        # When the phase last turned "idle": the device opens its follow-up
        # mic then, with no wake (raawr D-72, see main._device_idle).
        self.idle_since = 0.0
        # Set by force_idle(): the turn was declared dead, so a VAD stop event
        # that is still in flight must NOT re-emit `thinking` and re-stick the
        # device. Cleared on the next real activity (user/bot speech start).
        self._suppress_thinking = False
        # Dangling-VAD guard (A). The device sends {"type":"wake"} on every wake;
        # note_wake() resets this to False. A real UserStartedSpeaking sets it
        # True. A UserStoppedSpeaking with this still False is a server-VAD
        # segment from a PREVIOUS turn closing late (the reply gated the mic mid-
        # utterance, so the VAD never saw the stop) — committing it auto-creates
        # a garbage response to an empty turn. We then suppress the thinking and
        # cancel that racing response via the kill-window callback. Defaults True
        # so nothing is suppressed before the first wake signal (and so old
        # firmware that doesn't send `wake` degrades to a no-op).
        self._speech_since_wake = True
        # Callbacks into the websocket_handler's kill-window (set after the
        # _interrupt_kill_until dict exists). _on_dangling_stop arms it (cancel
        # the dangling turn's racing response); _on_real_speech clears it (a
        # genuine new utterance — never cancel ITS response).
        self._on_dangling_stop = None
        self._on_real_speech = None
        # Set by set_turn_success_handler() — called from
        # _emit_idle_after_debounce() the moment a reply genuinely finishes
        # (never from force_idle's turn-death path). ConnectionRecovery wires
        # this to reset its provider-router retry budget: BotStoppedSpeaking
        # is produced downstream of ConnectionRecovery's position in the
        # pipeline and never flows back to it on its own, so this callback is
        # what actually gets "the assistant finished answering" there.
        self._on_turn_success = None

    async def note_engine_turn_complete(self) -> None:
        """The engine says this reply is finished.

        Reached two ways, because one of them is not reliable.
        LLMFullResponseEndFrame travels down the pipeline, but
        LLMAssistantAggregator sits between the engine and this processor and
        consumes it, so on Gemini it never arrived — every turn then spent the
        full mid-turn grace before going idle, the device stayed shut for 8 s
        after each answer, and the user had to repeat himself. The engine's
        service calls this directly as well (see the Gemini provider), which
        is the path that actually works.

        Idempotent: whichever arrives first does the work, the other is a
        no-op, and the follow-up request goes out exactly once.
        """
        self._seen_engine_end = True
        self._model_turn_open = False
        # The only safe moment to ask for the follow-up window: the answer's
        # audio is queued, so the device waits it out before opening the mic.
        # Sending at tool-call time would open it before the question was even
        # spoken.
        await self._flush_follow_up()

    def set_follow_up_sender(self, sender) -> None:
        """Wire the async callable that tells the device to hold the mic open.

        Args:
            sender: async callable(), no arguments. None disables the feature,
                which is what every test and any caller that has not wired one
                gets — the device then falls back to its own follow_up_ms.
        """
        self._send_follow_up = sender

    def note_follow_up_requested(self) -> None:
        """Record that this turn's reply wants the microphone held open."""
        self._follow_up_wanted = True

    async def _flush_follow_up(self) -> None:
        """Send the pending follow-up request, once, at the end of a turn."""
        if not self._follow_up_wanted:
            return
        self._follow_up_wanted = False
        if self._send_follow_up is None:
            return
        try:
            await self._send_follow_up()
            logger.info("🎤 request_follow_up sent — mic stays open for the answer")
        except Exception as e:
            # The user can still answer with the wake word; never let this
            # failure take the turn down with it.
            logger.warning(f"⚠️ could not request the follow-up window: {e!r}")

    def set_turn_success_handler(self, callback) -> None:
        """Wire a callable(), no arguments, invoked when a reply finishes
        cleanly (debounce elapsed, no tool still running) — see
        _emit_idle_after_debounce."""
        self._on_turn_success = callback

    @property
    def phase(self):
        """The last phase sent to the device (None before the first)."""
        return self._current

    def note_wake(self) -> None:
        """Device woke (or a follow-up window closed without speech). Until the
        next real UserStartedSpeaking, any UserStoppedSpeaking is a dangling
        pre-wake VAD segment (see _speech_since_wake)."""
        self._speech_since_wake = False

    def set_kill_window_handlers(self, on_dangling=None, on_real_speech=None) -> None:
        """Wire the dangling-VAD guard to the websocket_handler kill-window."""
        self._on_dangling_stop = on_dangling
        self._on_real_speech = on_real_speech

    async def force_idle(self, reason: str = "") -> None:
        """Declare the current turn dead and put the device in idle.

        Used by the turn-death paths (rate-limit unstick, reconnect,
        thinking watchdog). Goes through the normal emit so the internal
        state stays consistent, and suppresses `thinking` until real
        activity follows — see the module docstring for the race this
        prevents.
        """
        self._cancel_pending_idle()
        self._cancel_watchdog()
        self._suppress_thinking = True
        if reason:
            logger.warning(f"📞 forcing phase idle ({reason[:90]})")
        await self._emit("idle")

    async def _emit(self, value: str) -> None:
        # "listening" is NEVER deduped. The device lifts its post-stop incoming-
        # audio suppression ONLY on receiving a "listening" phase (firmware
        # 14bff74). A stop can RE-SET that suppression after our last "listening"
        # without us emitting a different phase in between, so _current=="listening"
        # no longer reflects the device's suppress state — deduping the next real
        # turn's "listening" then leaves the device muted and the reply is dropped
        # (observed live 2026-06-14: rapid stop/wake testing → web-search answer
        # silently suppressed). A redundant "listening" is idempotent on the
        # device (re-lifts suppress, re-opens the mic gate; the barge-in cut-over
        # is a no-op because the mic is gated during a reply so a real
        # UserStartedSpeaking never coincides with queued TTS).
        if value == self._current and value != "listening":
            return
        if value == "idle":
            self.idle_since = time.monotonic()
            self._liveness.turn_over()
        self._current = value
        logger.info(f"📞 phase -> {value}")  # TEMP instrumentation
        if self._send_phase is not None:
            try:
                await self._send_phase(value)
            except Exception as e:  # never let UI signalling break the audio path
                logger.warning(f"⚠️ Failed to emit phase '{value}': {e}")

    def _cancel_pending_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._thinking_watchdog())

    async def close(self) -> None:
        """Stop idle and watchdog tasks owned by this connection."""
        tasks = (self._idle_task, self._watchdog_task)
        self._idle_task = None
        self._watchdog_task = None
        for task in tasks:
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"phase task shutdown: {e!r}")

    async def _emit_idle_after_debounce(self) -> None:
        try:
            await asyncio.sleep(self._idle_debounce_s)
            # The debounce alone assumes the gaps inside one reply are shorter
            # than it. On Gemini they are not: measured live 2026-09-09, one
            # answer arrived in three bursts with 6.7 s and 4.7 s of silence
            # between them. Each gap outlasted the 1.5 s debounce, so the phase
            # went replying -> idle -> replying twice mid-answer, and the
            # device played its START CHIME on every way back in. The engine
            # knows better than any timer can: LLMFullResponseEndFrame is
            # pushed when the model says the turn is complete. Wait for it —
            # but only up to a cap, so an engine that never sends one (or a
            # turn that dies) still reaches idle instead of hanging.
            waited = 0.0
            while (
                self._seen_engine_end
                and self._model_turn_open
                and waited < self._mid_turn_grace_s
            ):
                await asyncio.sleep(0.1)
                waited += 0.1
            if self._model_turn_open:
                logger.warning(
                    f"📞 no end-of-turn from the engine after {waited:.1f}s — "
                    f"going idle on the cap"
                )
        except asyncio.CancelledError:
            return
        # A tool (web search, MCP call) can still be running when the filler
        # reply's debounce expires — the turn isn't over, the model is
        # "thinking" while it waits for the tool. Going idle here makes the
        # device look done (idle LED, and it opens a follow-up window) while it
        # is actually still working — confusing on a slow web search. Show
        # `thinking` instead and arm the watchdog (which waits without a cap
        # while a tool is in flight); the tool's result response then flips the
        # phase to `replying`. Fast tools never reach here — their result reply
        # cancels this debounce first.
        if self._liveness.in_flight > 0:
            await self._emit("thinking")
            self._arm_watchdog()
            return
        # The bot's reply is genuinely over: no more speech arrived before
        # the debounce elapsed, and no tool is still working. This IS "a turn
        # just finished well" — see set_turn_success_handler.
        if self._on_turn_success is not None:
            self._on_turn_success()
        await self._emit("idle")

    async def _thinking_watchdog(self) -> None:
        """Force idle when `thinking` sits with no model activity (dead turn)."""
        armed_at = time.monotonic()
        last_inflight_log = 0.0
        try:
            while True:
                await asyncio.sleep(self.WATCHDOG_POLL_S)
                if self._current != "thinking":
                    return  # phase moved on — turn is alive, watchdog done
                now = time.monotonic()
                last = max(armed_at, self._liveness.last_activity)
                if self._liveness.in_flight > 0:
                    # A tool is running — the turn is alive by definition, and
                    # a long web search must get all the time it needs (no
                    # cap; see the module docstring). Log occasionally so a
                    # long wait is visibly deliberate in the log.
                    if now - last_inflight_log >= self.INFLIGHT_LOG_EVERY_S:
                        last_inflight_log = now
                        logger.info(
                            f"⏳ thinking-watchdog: {self._liveness.in_flight} tool(s) "
                            f"running for {now - last:.0f}s — waiting (no cap)"
                        )
                    continue
                if now - last < self.THINKING_TIMEOUT_S:
                    continue
                logger.warning(
                    f"⚠️ thinking-watchdog: no model activity for {now - last:.0f}s "
                    f"and no tool in flight — forcing idle to unstick the device"
                )
                await self.force_idle("thinking-watchdog")
                return
        except asyncio.CancelledError:
            return

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            # Liveness stamp for the wedge watchdog: the server VAD is alive.
            self.last_vad_mono = time.monotonic()
            self._suppress_thinking = False
            # A new user turn ends any reply that was still open, whether the
            # engine got round to saying so or not. Without this a reply that
            # died without its end-of-turn would keep the mid-turn grace armed
            # for every idle after it.
            self._model_turn_open = False
            self._follow_up_wanted = False
            # A: a genuine utterance has begun this turn → not a dangling VAD,
            # and the kill-window must NOT cancel THIS turn's response.
            self._speech_since_wake = True
            self._liveness.user_started(getattr(frame, "emulated", False))
            if self._on_real_speech is not None:
                self._on_real_speech()
            self._cancel_pending_idle()
            self._cancel_watchdog()
            await self._emit("listening")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._cancel_pending_idle()
            if self._current == "replying":
                # C: the bot is already replying. With barge_in:false the mic is
                # gated during a reply, so a user-speech-stop here can only be a
                # stale VAD tail of the question that just got its reply (the VAD
                # split the utterance and the second half closed late). Emitting
                # `thinking` would overwrite `replying`, reopen the mic mid-reply
                # (the TTS leaks in) and strand the LED in `thinking` until the
                # 15 s watchdog. Keep replying.
                logger.info("📞 'thinking' suppressed — bot is replying (stale VAD tail)")
            elif not self._speech_since_wake:
                # A: no real speech since the last wake/flush → this stop is a
                # dangling pre-wake server-VAD segment closing late. Suppress the
                # thinking AND cancel the garbage response the server auto-creates
                # for the (empty) committed turn.
                logger.info("📞 'thinking' suppressed + kill armed — dangling VAD (no speech since wake)")
                self._liveness.no_ack()
                if self._on_dangling_stop is not None:
                    self._on_dangling_stop()
            elif self._suppress_thinking:
                # A VAD stop raced a turn-death force_idle — stay idle.
                logger.info("📞 phase 'thinking' suppressed (turn already declared dead)")
                self._liveness.no_ack()
            else:
                await self._emit("thinking")
                self._arm_watchdog()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._liveness.bot_started()
            self._suppress_thinking = False
            self._model_turn_open = True
            self._cancel_pending_idle()
            self._cancel_watchdog()
            await self._emit("replying")
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self.note_engine_turn_complete()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._liveness.bot_stopped()
            # Don't go idle immediately — TTS comes in segments. Only emit idle
            # if the bot stays silent for the debounce window.
            self._cancel_pending_idle()
            self._idle_task = asyncio.create_task(self._emit_idle_after_debounce())

        await self.push_frame(frame, direction)
