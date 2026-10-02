"""OpenAI Realtime engine: the SafeRealtimeLLMService fixes and the session builder.

Everything here is specific to OpenAI's Realtime API. Nothing outside
`app/providers/` should import from this module directly except the
`app.providers` boundary itself.
"""
import asyncio
import logging
import os

from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.providers.tool_registration import ToolRegistrationMixin

from app.realtime_payload import transform_gpt_transcription_language

logger = logging.getLogger(__name__)


def _max_context_messages() -> int:
    """Read MAX_CONTEXT_MESSAGES the same way main.py does (default 12).

    `main.py` reads this once at startup and passes it into `SessionManager`
    for the client-reconnect restore path; `_reseed_context_after_reset`
    reads it directly here rather than threading a SessionManager reference
    through the service, since the value is the SAME env var, set once for
    the process lifetime — reading it again is not a second source of
    truth, just a second read of the one that exists.
    """
    try:
        value = int(os.environ.get("MAX_CONTEXT_MESSAGES", "12"))
    except (TypeError, ValueError):
        value = 12
    return max(0, value)


def add_history_cap(payload: dict) -> None:
    """Cap the conversation OpenAI re-bills on every response.

    gpt-realtime sends the whole conversation with each response, and the
    account allows 40k tokens/min. `truncation.token_limits.post_instructions`
    bounds everything after instructions+tools; retention_ratio 0.8 drops an
    extra 20% when it cuts, so the cache is busted once rather than every
    turn. REALTIME_HISTORY_TOKENS=0 leaves OpenAI's default (the model's full
    window). pipecat 0.0.97's SessionProperties has no `truncation` field,
    hence the payload edit.
    """
    if payload.get("type") != "session.update":
        return
    try:
        limit = int(os.environ.get("REALTIME_HISTORY_TOKENS", "2500"))
    except ValueError:
        limit = 2500
    if limit > 0:
        payload.setdefault("session", {})["truncation"] = {
            "type": "retention_ratio",
            "retention_ratio": 0.8,
            "token_limits": {"post_instructions": limit},
        }


class SafeRealtimeLLMService(ToolRegistrationMixin, OpenAIRealtimeLLMService):
    """OpenAIRealtimeLLMService with audio-truncation-on-interruption disabled.

    pipecat's `_truncate_current_audio_response()` (called by `_handle_interruption`
    on EVERY interruption — both our device "stop" AND pipecat's own server-VAD
    barge-in when the user wakes/speaks mid-reply) sends a
    `conversation.item.truncate` with `audio_end_ms = wall-clock ms since audio
    start`. But OpenAI BURSTS the reply faster than real-time, so that elapsed
    value massively overshoots the audio that actually exists, and OpenAI rejects
    it with `invalid_request_error("Audio content of N ms is already shorter than
    M ms")`. That errored truncate wedges the realtime session, so the user's very
    next turn gets NO response — the recurring "interrupt, then immediately ask
    again → silence" bug (confirmed in logs: session goes quiet right after
    `_truncate_current_audio_response`).

    The device stops playback authoritatively on its own, so server-side
    truncation buys us nothing. No-op it. (Cost: OpenAI's conversation history
    keeps the full assistant text the user may not have fully heard — purely
    cosmetic for context.)
    """

    def __init__(self, **kwargs):  # type: ignore[override]
        super().__init__(**kwargs)
        # Declared here (FIX ROUND 3, review) rather than left as an
        # implicit, getattr-only attribute: it's real instance state a
        # reconnect cycle can set, read, and clear, not incidental scratch
        # space. See _reseed_context_after_reset / _handle_evt_session_updated.
        self._pending_reseed_messages = None
        # Bana 0 (raawr US-016): called on every end of a user turn when the
        # fast path is configured. None = today's behaviour, nothing extra.
        self.on_user_turn_end = None
        self.on_user_turn_start = None  # plain callable, run before pipecat's handling
        self._turn_end_task = None

    async def _truncate_current_audio_response(self):  # type: ignore[override]
        return

    async def _handle_evt_speech_started(self, evt):  # type: ignore[override]
        if self.on_user_turn_start is not None:
            self.on_user_turn_start()
        await super()._handle_evt_speech_started(evt)

    async def _handle_evt_speech_stopped(self, evt):  # type: ignore[override]
        await super()._handle_evt_speech_stopped(evt)
        if self.on_user_turn_end is not None:
            # A task: the receive loop must keep reading while bana 0 runs.
            self._turn_end_task = asyncio.get_running_loop().create_task(self.on_user_turn_end())

    async def send_client_event(self, event):  # type: ignore[override]
        """Serialize GPT transcription models with their required `languages` field.

        Pipecat 0.0.97 only exposes the legacy singular `language` field on
        InputAudioTranscription. OpenAI rejects that field for newer GPT
        transcription models, which instead expect `languages: [<ISO code>]`.
        """
        payload = event.model_dump(exclude_none=True)
        transform_gpt_transcription_language(payload)
        add_history_cap(payload)
        await self._ws_send(payload)

    # Per-response cost accounting (fork). The API reports exact token usage in
    # every response.done; pipecat only pushes it as metrics frames. Log the
    # breakdown + estimated $ (measured 2026-07-12: warm turn ≈ $0.003-0.013,
    # cold session-first turn ≈ $0.019 — the 4.4k-token instruction+tool prefix
    # uncached) and publish a daily cost sensor to HA.
    _RATES = (  # $/1M tokens: (text_in, text_out, audio_in, audio_out, cached)
        (0.60, 2.40, 10.0, 20.0, 0.06)
        if "mini" in (os.environ.get("OPENAI_MODEL_CUSTOM") or os.environ.get("OPENAI_MODEL") or "")
        else (4.0, 24.0, 32.0, 64.0, 0.40)
    )

    async def _handle_evt_response_done(self, evt):  # type: ignore[override]
        try:
            u = evt.response.usage
            itd = getattr(u, "input_token_details", None)
            otd = getattr(u, "output_token_details", None)
            ctd = getattr(itd, "cached_tokens_details", None)
            in_text = getattr(itd, "text_tokens", 0) or 0
            in_audio = getattr(itd, "audio_tokens", 0) or 0
            cached = getattr(itd, "cached_tokens", 0) or 0
            c_text = getattr(ctd, "text_tokens", 0) or 0
            c_audio = getattr(ctd, "audio_tokens", 0) or 0
            out_text = getattr(otd, "text_tokens", 0) or 0
            out_audio = getattr(otd, "audio_tokens", 0) or 0
            ti, to, ai, ao, ca = self._RATES
            cost = (max(0, in_text - c_text) * ti + max(0, in_audio - c_audio) * ai
                    + cached * ca + out_text * to + out_audio * ao) / 1e6
            logger.info(
                f"💰 usage: in {in_text}txt+{in_audio}aud (cached {cached}) "
                f"out {out_text}txt+{out_audio}aud ≈ ${cost:.4f}"
            )
            from app.ha_sensors import PUBLISHER
            asyncio.get_running_loop().create_task(PUBLISHER.usage(cost, {
                "in_text": in_text, "in_audio": in_audio, "cached": cached,
                "out_text": out_text, "out_audio": out_audio}))
        except Exception as e:
            logger.debug(f"usage accounting failed: {e!r}")
        await super()._handle_evt_response_done(evt)

    async def reset_conversation(self):  # type: ignore[override]
        """Reconnect WITHOUT forcing a response on the reconnected session.

        pipecat's reset_conversation() (used by ConnectionRecovery on a 60-min cap
        / keepalive drop) reconnects and leaves `_llm_needs_conversation_setup =
        True`. The collision: if a turn was mid-flight when the WS dropped,
        `_create_response()` had already set `_run_llm_when_api_session_ready =
        True` (because `_api_session_ready` went False on disconnect). After the
        reconnect, the `session.updated` handler sees that flag and fires
        `_create_response()` — but under semantic_vad (`create_response=true`) the
        SERVER also auto-creates a response for the user's next turn. Two
        response.create events collide → `conversation_already_has_active_response`,
        and that turn gets no answer (observed: first turn right after a reconnect
        fails, ~1 in 20 reconnects — whenever the user happens to speak in the few
        seconds just after a reconnect).

        Fix: after the normal reconnect, clear `_run_llm_when_api_session_ready` so
        the reconnected session does NOT self-create a response, and set
        `_llm_needs_conversation_setup = False` (same as the startup pre-seed) — the
        server-VAD drives every user-turn response, so we never need to create one
        ourselves on reconnect.

        CORRECTED (Task 9b fix round 1): this comment used to claim "the live
        context is untouched (it's restored by the SessionManager on the next
        real turn)". That was false, and remained false after Task 9b's first
        pass at this file — the SessionManager's restore path
        (ContextInitializer) only ever runs once, on a brand-new client
        WebSocket connection; it never re-fires for an in-place reconnect of
        this SAME service instance, because no new StartFrame is produced.
        Meanwhile `self._context` (this Python object) is untouched by
        reset — it still holds the full conversation up to the disconnect —
        but the FRESH OpenAI-side session this reconnect just opened knows
        NOTHING about it, and clearing `_llm_needs_conversation_setup` above
        stops `_create_response()`'s own lazy "send initial messages" loop
        from ever sending it either. Net effect before this fix: every
        60-minute session cap silently wiped the model's memory of the
        conversation. `_reseed_context_after_reset()` below re-sends
        `self._context`'s own messages onto the freshly reconnected session,
        the same way `ContextInitializer` seeds a brand-new one — using this
        service's own live context (more current than SessionManager's cache,
        which only snapshots on disconnect), not the SessionManager's cache.
        The actual send is deferred until the session confirms it's ready —
        see `_reseed_context_after_reset` and `_handle_evt_session_updated`.
        """
        self._resetting_conversation = True
        try:
            await super().reset_conversation()
            try:
                self._run_llm_when_api_session_ready = False
                self._llm_needs_conversation_setup = False
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"⚠️ could not clear post-reconnect response flags: {e!r}")
            await self._reseed_context_after_reset()
        finally:
            self._resetting_conversation = False

    async def _reseed_context_after_reset(self):
        """Re-seed the freshly reconnected session with the conversation it
        just lost (see `reset_conversation`'s docstring for why this is
        needed at all).

        Uses this service's OWN `_context` — the live, ongoing conversation
        pipecat has been tracking for this connection all along — not the
        SessionManager's cache (that cache is keyed by client_id for a
        brand-new WebSocket connection; this is an in-place reconnect of the
        same service, which the SessionManager never even sees). Tool calls
        are stripped first for the same reason `_strip_tool_plumbing` exists
        for the client-reconnect path: their ids belong to the conversation
        that just ended, and the fresh session rejects a replayed id. The
        result is capped by `_cap_restored_messages` at the same
        `MAX_CONTEXT_MESSAGES` setting SessionManager's client-reconnect
        restore uses (see that function's docstring) — this path fires on
        every 60-minute cap and every proactive refresh, so an uncapped
        conversation would otherwise grow without bound over a long day.

        FIX ROUND 2 (review): this used to send immediately once this method
        ran, i.e. as soon as `_connect()` (called by `super().reset_conversation()`)
        returned — which only means the websocket handshake finished, not
        that OpenAI's session is ready to accept `conversation.item.create`.
        If the server rejects something sent that early, it replies with an
        `error` event, and pipecat's `_receive_task_handler` treats EVERY
        unrecognised error as fatal (`_handle_evt_error(evt); return` — see
        that method's override below): the reader task dies, and the device
        is deaf until the NEXT reconnect. Not a lost memory — a dead
        session, every hour, to save one round trip.

        Fixed by mirroring the exact deferral pattern `_create_response()`
        already uses for the identical question ("is the session ready
        yet?"): `_api_session_ready` is the signal (set True by
        `_handle_evt_session_updated`, which fires once our OWN
        `session.update` — sent from `_handle_evt_session_created` in
        response to `session.created` — has round-tripped). If not ready
        yet, the prepared messages are stashed in
        `self._pending_reseed_messages` and sent later, from the
        `_handle_evt_session_updated` override below, once
        `_api_session_ready` is confirmed True — so the re-seed's
        `conversation.item.create`s always follow `session.update` being
        acknowledged on the same socket, never race ahead of it.

        Considered and rejected: hooking `_handle_evt_session_created`
        directly instead. `session.created` only proves the server accepted
        the TCP/TLS handshake and is about to read our settings — it says
        nothing about whether the settings we're about to send (and the
        conversation.item.create calls after them) will be accepted.
        `_api_session_ready` is the strictly later, already-proven-safe
        point `_create_response()` itself insists on for the same class of
        send, so this reuses that guarantee rather than a weaker one.

        Failure mode considered: if the socket dies AGAIN before
        `session.updated` ever arrives (e.g. the settings themselves are
        rejected), `_pending_reseed_messages` is simply left set and never
        sent for THIS attempt — but `_receive_task_handler`'s override
        below already turns that death into a fresh `ConnectionRecovery`
        reconnect trigger, and the NEXT `reset_conversation()` call
        recomputes `_pending_reseed_messages` from `self._context` (still
        intact) and overwrites the stale one before it could ever be acted
        on. No permanently-lost re-seed; at worst one retried cycle.
        """
        try:
            context = getattr(self, "_context", None)
            if context is None:
                # FIX ROUND 3 (review, Minor): clear any stash left over from
                # an earlier, still-pending reseed. Without this, a stale
                # `_pending_reseed_messages` from a PREVIOUS reset attempt
                # (one that had a context) would survive this call and still
                # get delivered by a later `_handle_evt_session_updated`,
                # even though THIS reset determined there is nothing to
                # re-seed now.
                self._pending_reseed_messages = None
                return
            from app.context_restore import _cap_restored_messages, _strip_tool_plumbing

            messages = _strip_tool_plumbing(context.get_messages())
            messages = _cap_restored_messages(messages, _max_context_messages())
            if not messages:
                self._pending_reseed_messages = None  # see comment above
                return
            if not getattr(self, "_api_session_ready", False):
                self._pending_reseed_messages = messages
                return
            await self._send_pending_reseed(messages)
        except Exception as e:
            logger.warning(f"⚠️ Failed to prepare context re-seed after reset: {e!r}")

    async def _send_pending_reseed(self, messages):
        """Actually deliver a seed prepared by `_reseed_context_after_reset`
        (the hourly/reconnect re-seed) or by `_seed_openai_context_silently`
        (a client reconnect's cached conversation) -- immediately, if the
        session was already ready, or later from
        `_handle_evt_session_updated` once it becomes ready."""
        from app.context_restore import restore_context_silently
        from app.providers import OPENAI

        try:
            restored = await restore_context_silently(OPENAI, self, messages)
        except Exception as e:
            logger.warning(f"⚠️ Failed to re-seed context after reset: {e!r}")
            return
        if restored:
            logger.info(
                f"📤 Seeded {len(messages)} message(s) onto the ready session "
                f"(waiting for user)"
            )
        else:
            # FIX ROUND 2 (review): the old code only ever logged success,
            # so a failed re-seed (no websocket, or restore_context_silently
            # itself declining) reopened the hourly hole with NOTHING in the
            # log to notice — the exact invisibility this whole task is
            # about. warning, not info: by this point the session claimed to
            # be ready, so a False return here means something is actually
            # wrong, not a normal transient state.
            logger.warning(
                f"⚠️ Context re-seed was NOT sent ({len(messages)} message(s) "
                f"prepared) -- the model will not remember this conversation "
                f"until the next successful reconnect"
            )

    async def _handle_evt_session_updated(self, evt):  # type: ignore[override]
        """After `_api_session_ready` is confirmed (see
        `_reseed_context_after_reset`'s docstring), deliver any re-seed that
        was deferred waiting for exactly this signal.

        FIX ROUND 3 (review): this used to `await super()` FIRST. Read
        super's body (pipecat 0.0.97,
        `pipecat/services/openai/realtime/llm.py`) before reordering it:

            self._api_session_ready = True
            if self._run_llm_when_api_session_ready:
                self._run_llm_when_api_session_ready = False
                await self._create_response()

        `_run_llm_when_api_session_ready` can already be True by the time
        `session.updated` arrives — anything that tried to create a
        response while we were still waiting (e.g. a completed tool result
        arriving in that window) hits `_create_response()`'s own "not ready
        yet" branch and sets exactly this flag. Calling `super()` first
        meant THAT deferred `_create_response()` could fire, send a bare
        `response.create` over a conversation `reset_conversation` had
        already stripped of `_llm_needs_conversation_setup` (so no initial
        messages get sent either), and only THEN would our own re-seed's
        `conversation.item.create`s land — arriving mid-response instead of
        before it. Net effect: a turn answered with no memory, plus
        conversation items landing while a response is already streaming.

        Fixed by taking over `_api_session_ready`'s assignment ourselves and
        delivering the pending re-seed BEFORE calling `super()` at all. By
        the time `super()` runs (and possibly fires `_create_response()` via
        `_run_llm_when_api_session_ready`), the restored conversation is
        already in place. `super()` still owns
        `_run_llm_when_api_session_ready`'s own check-and-clear and the
        `_create_response()` call itself — this only reorders WHEN that call
        can see our re-seed, not what it does.
        """
        self._api_session_ready = True
        pending = getattr(self, "_pending_reseed_messages", None)
        if pending:
            self._pending_reseed_messages = None
            await self._send_pending_reseed(pending)
        await super()._handle_evt_session_updated(evt)

    # Error codes that must NOT kill the realtime session. pipecat 0.0.97's
    # _receive_task_handler does `_handle_evt_error(evt); return` on EVERY
    # error event — the reader task dies, the in-flight reply cuts off
    # mid-sentence and the session is deaf until the next connection death.
    # Observed live (2026-06-10): semantic_vad split one utterance into two
    # turns, the server's auto-created second response collided with the
    # first → conversation_already_has_active_response → the playing reply
    # stopped at 4.4 s and the session wedged. These codes are harmless
    # protocol races; the right move is to keep reading.
    BENIGN_ERROR_CODES = {
        # The server auto-created a response while one was still active
        # (VAD split a sentence into two turns). The active response keeps
        # streaming — nothing is broken.
        "conversation_already_has_active_response",
        # response.cancel landed after the response already finished (device
        # "stop" / the post-interrupt racing-response kill) — nothing to
        # cancel, nothing broken.
        "response_cancel_not_active",
        # input_audio_buffer.commit raced our input_audio_buffer.clear (device
        # "stop"): an empty commit is exactly the outcome we wanted.
        "input_audio_buffer_commit_empty",
    }

    async def _maybe_handle_evt_retrieve_conversation_item_error(self, evt):  # type: ignore[override]
        """Generic benign-error filter, hooked into pipecat's receive loop.

        pipecat's `_receive_task_handler` treats a True return from this
        method as "error handled — keep the receive loop alive"; every other
        error event kills the reader task (`_handle_evt_error` + `return`).
        It is the ONLY surviving path, so besides the original retrieve-item
        case (super()), we declare our benign protocol races handled here
        instead of letting them cut off live audio and wedge the session.
        """
        if await super()._maybe_handle_evt_retrieve_conversation_item_error(evt):
            return True
        code = getattr(getattr(evt, "error", None), "code", None)
        if code in self.BENIGN_ERROR_CODES:
            logger.warning(
                f"⚠️ benign realtime error ignored (session stays alive): {code}"
            )
            return True
        return False

    async def _receive_task_handler(self):  # type: ignore[override]
        """Surface OpenAI reader death as an ErrorFrame so recovery can act.

        pipecat's receive loop can end without producing ANY ErrorFrame: a
        silent server-side close ends the `async for` normally, and a network
        drop raises ConnectionClosed, which the task manager merely LOGS
        ("unexpected exception"). Nothing reaches ConnectionRecovery either
        way, so the session sat deaf for HOURS until the next user utterance
        hit the dead socket — losing that utterance (observed live twice).
        Wrap the loop and report its end; ConnectionRecovery treats the
        message as a reconnect trigger.
        """
        try:
            await super()._receive_task_handler()
        except asyncio.CancelledError:
            raise  # our own disconnect/reset tearing the task down — not a death
        except Exception as e:
            await self.push_error(error_msg=f"realtime receive loop died: {e!r}")
            return
        # reset_conversation() intentionally closes the old reader before
        # connecting the replacement session. That normal close is not a
        # recoverable failure and may arrive after the processor has stopped.
        if getattr(self, "_resetting_conversation", False):
            return
        # Loop ended without an exception: a clean server-side close, or the
        # fatal-error path (which already pushed its own ErrorFrame —
        # duplicates collapse in ConnectionRecovery's cooldown/guard).
        await self.push_error(error_msg="realtime receive loop ended — connection closed")


def build(options, tools):
    """Build a configured OpenAI Realtime session for one device.

    Args:
        options: The ProviderOptions carrying every knob from the add-on config.
        tools: Tool definitions in OpenAI Realtime shape.

    Returns:
        A SafeRealtimeLLMService, not yet connected.
    """
    from pipecat.services.openai.realtime.events import (
        AudioConfiguration,
        AudioInput,
        AudioOutput,
        InputAudioNoiseReduction,
        InputAudioTranscription,
        SemanticTurnDetection,
        SessionProperties,
        TurnDetection,
    )

    # Turn detection: semantic_vad (recommended — semantic end-of-turn,
    # echo-resistant, doesn't cut the user off) or classic server_vad.
    if options.turn_detection_type == "semantic_vad":
        turn_detection = SemanticTurnDetection(
            eagerness=options.vad_eagerness,
            # create_response=True (default): the SERVER creates a
            # response on every detected end-of-turn. This is required for
            # multi-turn conversation. Pipecat 0.0.97's
            # OpenAIRealtimeLLMService._handle_context only auto-creates a
            # response for the FIRST context (turn 1) and after tool
            # results (its else-branch just updates the context); a plain
            # 2nd/3rd user turn therefore gets NO response unless the
            # server makes it. We previously set this False to stop a
            # turn-1 double-response (server + Pipecat first-context both
            # creating → `conversation_already_has_active_response`), but
            # that silently broke every turn after the first (device hung
            # in "thinking"). True is the correct trade: the server drives
            # all user-turn responses; Pipecat still creates the post-tool
            # response via _process_completed_function_calls. To stop the
            # turn-1 double (server + Pipecat-first-context both creating →
            # conversation_already_has_active_response), run() seeds
            # self._context once at startup with a kickoff LLMRunFrame, so
            # the user's first real turn hits the else-branch too.
            create_response=options.semantic_vad_create_response,
            interrupt_response=options.interrupt_response,
        )
    else:
        turn_detection = TurnDetection(
            type="server_vad",
            threshold=options.vad_threshold,
            prefix_padding_ms=options.vad_prefix_padding_ms,
            silence_duration_ms=options.vad_silence_duration_ms,
        )

    # Optionally pin the input-transcription language to stop the model
    # drifting between languages (e.g. "nl"). Empty -> auto-detect.
    # transcription_model picks the STT used for the transcript text.
    transcription = (
        InputAudioTranscription(
            model=options.transcription_model,
            language=options.transcription_language,
        )
        if options.transcription_language
        else None
    )

    # Optional near/far-field input noise reduction (helps the VAD reject
    # background noise / residual speaker leak). None = off (default).
    noise_reduction = (
        InputAudioNoiseReduction(type=options.noise_reduction)
        if options.noise_reduction
        else None
    )

    session_properties = SessionProperties(
        instructions=options.instructions,
        # Cap the reply length: bounds runaway monologues + per-response
        # output-token cost. None = unlimited (the API default "inf").
        max_output_tokens=options.max_output_tokens,
        audio=AudioConfiguration(
            input=AudioInput(
                turn_detection=turn_detection,
                transcription=transcription,
                noise_reduction=noise_reduction,
            ),
            # speed is a post-generation playback rate (0.25-1.5, 1.0 = normal).
            output=AudioOutput(voice=options.voice, speed=options.speed),
        ),
        tools=tools,
    )

    if options.turn_detection_type == "semantic_vad":
        logger.info(
            f"🎚️ Turn detection: semantic_vad (eagerness={options.vad_eagerness}, "
            f"create_response={options.semantic_vad_create_response}, "
            f"interrupt_response={options.interrupt_response})"
            + (f", transcription={options.transcription_model} (lang={options.transcription_language})" if options.transcription_language else " (transcription off)")
        )
    else:
        logger.info(
            f"🎚️ Turn detection: server_vad (threshold={options.vad_threshold}, "
            f"silence_duration_ms={options.vad_silence_duration_ms})"
            + (f", transcription={options.transcription_model} (lang={options.transcription_language})" if options.transcription_language else " (transcription off)")
        )

    return SafeRealtimeLLMService(
        api_key=options.api_key,
        model=options.model,
        session_properties=session_properties,
        start_audio_paused=False,
    )
