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
"""
import json
import logging

from pipecat.services.openai.realtime import events as rt_events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

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


def xai_session(payload, language, server_search, create_response=True):
    """Rewrite pipecat's OpenAI session.update into the shape xAI accepts."""
    if payload.get("type") != "session.update":
        return
    session = payload.setdefault("session", {})
    session.pop("truncation", None)
    audio_in = session.get("audio", {}).get("input", {})
    audio_in.pop("noise_reduction", None)
    if not create_response and audio_in.get("turn_detection"):
        # Bana 0: the agent asks for the answer itself, only on a miss.
        audio_in["turn_detection"]["create_response"] = False
    if language:
        audio_in["transcription"] = {"language_hint": language.split("-")[0]}
    else:
        audio_in.pop("transcription", None)
    if server_search and session.get("tools") is not None:
        tools = [t for t in session["tools"] if t.get("name") != "web_search"]
        session["tools"] = tools + [{"type": "web_search"}]


class XaiRealtimeLLMService(SafeRealtimeLLMService):
    """SafeRealtimeLLMService pointed at xAI. See the module docstring."""

    def __init__(self, language="", server_search=True, create_response=True, **kwargs):
        super().__init__(**kwargs)
        self._language = language
        self._server_search = server_search
        self._xai_create_response = create_response  # not _create_response: that is pipecat's method

    async def send_client_event(self, event):  # type: ignore[override]
        payload = event.model_dump(exclude_none=True)
        xai_session(payload, self._language, self._server_search, self._xai_create_response)
        await self._ws_send(payload)

    async def _receive_task_handler(self):  # type: ignore[override]
        if self._websocket is not None and not isinstance(self._websocket, _XaiSocket):
            self._websocket = _XaiSocket(
                self._websocket, SERVER_TOOLS if self._server_search else set()
            )
        await super()._receive_task_handler()

    async def _handle_evt_response_done(self, evt):  # type: ignore[override]
        # Skip the OpenAI token-price sensor: xAI bills per minute.
        await OpenAIRealtimeLLMService._handle_evt_response_done(self, evt)


def build(options, tools):
    """Build a configured xAI Grok Voice session for one device."""
    from pipecat.services.openai.realtime.events import (
        AudioConfiguration,
        AudioInput,
        AudioOutput,
        SessionProperties,
        TurnDetection,
    )

    session_properties = SessionProperties(
        instructions=options.instructions,
        max_output_tokens=options.max_output_tokens,
        audio=AudioConfiguration(
            # xAI has server_vad or nothing; semantic_vad does not exist there.
            input=AudioInput(turn_detection=TurnDetection(
                type="server_vad",
                threshold=options.vad_threshold,
                prefix_padding_ms=options.vad_prefix_padding_ms,
                silence_duration_ms=options.vad_silence_duration_ms,
            )),
            output=AudioOutput(voice=options.voice, speed=options.speed),
        ),
        tools=tools,
    )
    logger.info(
        f"🎚️ xai: {options.model} voice={options.voice} server_vad "
        f"(silence_duration_ms={options.vad_silence_duration_ms}), web_search=server-side"
    )
    return XaiRealtimeLLMService(
        api_key=options.api_key,
        model=options.model,
        base_url=XAI_REALTIME_URL,
        session_properties=session_properties,
        start_audio_paused=False,
        language=options.transcription_language,
        create_response=options.semantic_vad_create_response,
    )
