"""xAI Grok Voice engine: OpenAI Realtime's protocol on xAI's socket.

xAI's realtime API speaks the OpenAI Realtime protocol closely enough that
SafeRealtimeLLMService runs it -- with these differences, all handled here
(every one seen in raw events from the live key, 2026-10-02):

1. Events pipecat 0.0.97 has no model for (`ping`, the cumulative
   `conversation.item.input_audio_transcription.updated`): pipecat's
   `parse_server_event` raises on them, which kills the reader and leaves the
   device deaf. Dropped before pipecat sees them.
2. Events pipecat knows, in a shape its models refuse: `usage: {}` on every
   response.created/response.done, `role: "tool"` on function-call items,
   no `part` on content_part.done, no `output_index` on arguments.delta,
   xAI's own session shape echoed in session.created/updated.
   A refused response.done would be a reply that never ends, so they are
   filled in rather than dropped.
3. Server-side web search reports itself as a `web_search` function call
   AFTER the spoken answer. Answering it makes the model talk again, so that
   call never reaches pipecat's function runner.
4. The session wants xAI's own shape (see xai_session).
5. Billing is per minute, not per token, so the OpenAI cost sensor is skipped.
6. Turn end is decided here by the local Silero VAD (0.25.3), not by xAI's
   server_vad: live 2026-10-02 20:29:42 "Vad är det för väder i helgen?"
   reached "thinking" 13 s after the wake, and later turns sat 5-9 s, music
   and room noise keeping server_vad from hearing the silence. The session
   gets `turn_detection: null`; at the local end the agent commits the
   buffer and asks for the answer (or lets bana 0 decide first).
   XAI_TURN_DETECTION=server keeps xAI's server_vad.
7. xAI closes a session after 900 s without a turn ("Conversation timed out
   ... due to inactivity", server_error/timeout). That is an idle hang-up,
   like Gemini's: the socket is closed and reconnected, no strike.
"""
import os
import asyncio
import json
import logging

from pipecat.frames.frames import UserStartedSpeakingFrame
from pipecat.services.openai.realtime import events as rt_events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.providers.local_turns import LocalTurns, LocalTurnsMixin
from app.providers.openai_realtime import SafeRealtimeLLMService

logger = logging.getLogger(__name__)

XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime"

# xAI's names for events pipecat knows under OpenAI's name.
_RENAMED = {"response.audio.delta": "response.output_audio.delta"}
# Tools xAI runs itself; their calls are reports, not requests.
SERVER_TOOLS = {"web_search", "x_search"}
_ROLES = {"user", "assistant", "system"}
_ZERO_USAGE = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
               "input_token_details": {}, "output_token_details": {}}


def _fix_item(item):
    if isinstance(item, dict) and item.get("role") not in _ROLES:
        item.pop("role", None)


def translate_server_event(raw, server_tools=SERVER_TOOLS):
    """One raw xAI event in, the OpenAI-shaped event pipecat can parse out.

    Returns:
        The JSON string to hand pipecat, or None to drop the event.
    """
    try:
        evt = json.loads(raw)
    except (TypeError, ValueError):
        return None
    kind = _RENAMED.get(evt.get("type"), evt.get("type"))
    model = rt_events._server_event_types.get(kind)
    if model is None:
        return None
    if kind == "response.function_call_arguments.done" and evt.get("name") in server_tools:
        return None
    evt["type"] = kind
    if "session" in evt:
        # xAI echoes its own session shape (transcription.language_hint),
        # which pipecat's model refuses; pipecat never reads it. Dropping
        # session.updated instead would leave the session never "ready": deaf.
        evt["session"] = {}
    response = evt.get("response")
    if isinstance(response, dict):
        response["usage"] = {**_ZERO_USAGE, **(response.get("usage") or {})}
        if response.get("status") == "failed" and not isinstance(response.get("status_details"), dict):
            response["status_details"] = {"error": {"message": str(response.get("status_details"))}}
        for item in response.get("output") or []:
            _fix_item(item)
    _fix_item(evt.get("item"))
    if kind == "response.content_part.done":
        evt.setdefault("part", {"type": "audio"})
    if kind == "response.function_call_arguments.delta":
        evt.setdefault("output_index", 0)
    try:
        model.model_validate(evt)
    except Exception as e:
        logger.warning(f"⚠️ xai: dropped {kind} pipecat cannot parse: {str(e)[:200]}")
        return None
    return json.dumps(evt)


class _XaiSocket:
    """The live websocket, with xAI's events made readable for pipecat."""

    def __init__(self, ws, server_tools):
        self._ws = ws
        self._server_tools = server_tools

    def __getattr__(self, name):
        return getattr(self._ws, name)

    async def __aiter__(self):
        async for raw in self._ws:
            out = translate_server_event(raw, self._server_tools)
            if out is not None:
                yield out


def xai_session(payload, language, server_search, create_response=True, manual_turns=False):
    """Rewrite pipecat's OpenAI session.update into the shape xAI accepts."""
    if payload.get("type") != "session.update":
        return
    session = payload.setdefault("session", {})
    session.pop("truncation", None)
    audio_in = session.setdefault("audio", {}).setdefault("input", {})
    audio_in.pop("noise_reduction", None)
    if manual_turns:
        # null, not absent: absent leaves xAI's default server_vad on.
        audio_in["turn_detection"] = None
    elif not create_response and audio_in.get("turn_detection"):
        # Bana 0: the agent asks for the answer itself, only on a miss.
        audio_in["turn_detection"]["create_response"] = False
    if language:
        audio_in["transcription"] = {"language_hint": language.split("-")[0]}
    else:
        audio_in.pop("transcription", None)
    if server_search and session.get("tools") is not None:
        tools = [t for t in session["tools"] if t.get("name") != "web_search"]
        session["tools"] = tools + [{"type": "web_search"}]


# The error xAI sends before closing a session nobody spoke to for 900 s.
IDLE_TIMEOUT_CODE = "timeout"
IDLE_TIMEOUT_MARKER = "inactivity"


class XaiRealtimeLLMService(LocalTurnsMixin, SafeRealtimeLLMService):
    """SafeRealtimeLLMService pointed at xAI. See the module docstring."""

    _turn_open = False

    def __init__(self, language="", server_search=True, create_response=True, **kwargs):
        super().__init__(**kwargs)
        self._language = language
        self._server_search = server_search
        self._xai_create_response = create_response  # not _create_response: that is pipecat's method

    async def send_client_event(self, event):  # type: ignore[override]
        if self._turns is not None and isinstance(event, rt_events.InputAudioBufferClearEvent):
            # Device stop, follow-up cut-off or a clean start: the buffer goes,
            # and so does the turn we were building in it.
            self._forget_turn()
        payload = event.model_dump(exclude_none=True)
        xai_session(payload, self._language, self._server_search, self._xai_create_response,
                    manual_turns=self._turns is not None)
        await self._ws_send(payload)

    async def _send_user_audio(self, frame):  # type: ignore[override]
        """With local turns: append audio only inside a turn our VAD opened."""
        if self._turns is None:
            return await super()._send_user_audio(frame)
        event = self._turns.feed(frame.audio, frame.sample_rate)
        if not self._turn_open:
            if event != "start":
                self._keep_preroll(frame)
                return
            self._turn_open = True
            # What xAI's speech_started did, minus the interruption-and-wait
            # (it would wait on this very frame task). The device mic is shut
            # while the model speaks, so there is nothing to interrupt.
            if self.on_user_turn_start is not None:
                self.on_user_turn_start()
            await self.push_frame(UserStartedSpeakingFrame())
            preroll = self._take_preroll()
            if preroll:
                await super()._send_user_audio(
                    type(frame)(audio=preroll, sample_rate=frame.sample_rate,
                                num_channels=frame.num_channels)
                )
        await super()._send_user_audio(frame)
        if event == "end":
            await self._end_turn()

    async def _end_turn(self) -> None:
        """Commit the turn and hand it on, as speech_stopped does on OpenAI."""
        self._turn_open = False
        logger.debug("🎙️ local VAD: silence → input_audio_buffer.commit")
        await self.send_client_event(rt_events.InputAudioBufferCommitEvent())
        if self.on_user_turn_end is None:
            await self.send_client_event(rt_events.ResponseCreateEvent())
        # Bana 0 on a task (it asks for the answer on a miss), or the silence ack.
        await self._handle_evt_speech_stopped(None)

    def _forget_turn(self) -> None:
        if self._turn_open:
            logger.info("🧽 open xAI turn dropped with the input buffer")
        self._turn_open = False
        self._preroll = bytearray()
        self.cancel_silence_ack()
        self._turns.reset()

    async def end_audio_stream(self, keep_speech: bool = False) -> str:
        """The device dropped the input (providers.drop_pending_input_audio).

        keep_speech: the follow-up window closed mid-utterance. Speech our VAD
        is sure of is then answered, not dropped -- he did not take it back.
        """
        if keep_speech and self._turns is not None and self._turn_open:
            logger.info("🧽 follow-up closed mid-utterance — answering it, not dropping it")
            await self._end_turn()
            self._turns.reset()  # the mic is shut; the next wake starts clean
            return "input_audio_buffer.commit"
        await self.send_client_event(rt_events.InputAudioBufferClearEvent())
        return "input_audio_buffer.clear"

    async def _maybe_handle_evt_retrieve_conversation_item_error(self, evt):  # type: ignore[override]
        """xAI's 900 s idle close: hang up and reconnect, never a strike.

        Left to pipecat it is a fatal error event: an ErrorFrame the router
        counts as an xai hiccup (live 2026-10-02 20:22:49, 1/2 after one quiet
        quarter of an hour). Closing the socket here ends the reader instead,
        and ConnectionRecovery reconnects a dead socket without reporting it.
        """
        error = getattr(evt, "error", None)
        if (getattr(error, "code", None) == IDLE_TIMEOUT_CODE
                and IDLE_TIMEOUT_MARKER in (getattr(error, "message", "") or "")):
            logger.info("🔁 xAI closed an idle session (900 s) — reconnecting, not counting it")
            if self._websocket is not None:
                asyncio.get_running_loop().create_task(self._websocket.close())
            return True
        return await super()._maybe_handle_evt_retrieve_conversation_item_error(evt)

    async def _receive_task_handler(self):  # type: ignore[override]
        if self._websocket is not None and not isinstance(self._websocket, _XaiSocket):
            self._websocket = _XaiSocket(
                self._websocket, SERVER_TOOLS if self._server_search else set()
            )
        await super()._receive_task_handler()

    async def _handle_evt_response_done(self, evt):  # type: ignore[override]
        # Skip the OpenAI token-price sensor: xAI bills per minute.
        await OpenAIRealtimeLLMService._handle_evt_response_done(self, evt)


# Grok Voice renders a few inline sound tags in its own voice (owner,
# 2026-10-02). Only on xai: the other engines would read "[chuckle]" aloud.
# XAI_EXPRESSIVE_TAGS=false turns it off.
EXPRESSIVE_TAGS_NOTE = (
    "\n\nLJUD: du kan lägga in ljud i talet med taggar, sparsamt och bara där "
    "det sitter naturligt: [chuckle] eller [laugh] när något är roligt, [sigh] "
    "vid trista besked, [breath] före ett längre svar, [hum-tune] när du väntar "
    "på något, [tsk] när något krånglar. Högst en tagg per svar, aldrig i en "
    "ren kvittens som 'tänt'."
)


def with_expressive_tags(instructions):
    if os.environ.get("XAI_EXPRESSIVE_TAGS", "true").strip().lower() in ("0", "false", "no", "av"):
        return instructions
    return (instructions or "") + EXPRESSIVE_TAGS_NOTE


def build(options, tools):
    """Build a configured xAI Grok Voice session for one device."""
    from pipecat.services.openai.realtime.events import (
        AudioConfiguration,
        AudioInput,
        AudioOutput,
        SessionProperties,
        TurnDetection,
    )

    turns = None
    if options.xai_turn_detection != "server":
        # The input reaches the service at 24 kHz; Silero runs at 16.
        turns = LocalTurns.create(int(options.xai_turn_silence_ms))
    session_properties = SessionProperties(
        instructions=with_expressive_tags(options.instructions),
        max_output_tokens=options.max_output_tokens,
        audio=AudioConfiguration(
            # xAI has server_vad or nothing; semantic_vad does not exist there.
            # With local turns xai_session sends null on the wire.
            input=AudioInput(turn_detection=None if turns else TurnDetection(
                type="server_vad",
                threshold=options.vad_threshold,
                prefix_padding_ms=options.vad_prefix_padding_ms,
                silence_duration_ms=options.vad_silence_duration_ms,
            )),
            output=AudioOutput(voice=options.voice, speed=options.speed),
        ),
        tools=tools,
    )
    if turns:
        detection = (f"turn detection LOCAL (Silero, end after {options.xai_turn_silence_ms}ms "
                     f"silence) → commit")
    else:
        detection = f"server_vad (silence_duration_ms={options.vad_silence_duration_ms})"
    logger.info(f"🎚️ xai: {options.model} voice={options.voice} {detection}, web_search=server-side")
    service = XaiRealtimeLLMService(
        api_key=options.api_key,
        model=options.model,
        base_url=XAI_REALTIME_URL,
        session_properties=session_properties,
        start_audio_paused=False,
        language=options.transcription_language,
        create_response=options.semantic_vad_create_response,
    )
    service._turns = turns
    return service
