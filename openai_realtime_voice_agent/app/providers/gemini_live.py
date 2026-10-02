"""Google Gemini Live, wearing the same shape as the OpenAI session.

Proven against the live account 2026-09-08: all 52 tools accepted at once --
this add-on's 8 plus Home Assistant's 44 -- and the model picked search_home by
itself when asked about the car's battery, then answered in Swedish.

⛔ pipecat 0.0.97 defaults to models/gemini-2.0-flash-live-001, which the API
refuses with `1008 ... not supported for bidiGenerateContent`. Every live model
is a preview name and will be retired in turn, so the model is a setting and
this default is only the one that worked on the day it was written.
"""
import logging
import time
from typing import Any, Dict, List

from google.genai.types import (
    EndSensitivity,
    HttpOptions,
    ProactivityConfig,
    StartSensitivity,
)
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiVADParams,
    InputParams,
)
from pipecat.transcriptions.language import Language

from app.providers.tool_registration import ToolRegistrationMixin

logger = logging.getLogger(__name__)

# Verified live 2026-09-08. The other working one is
# models/gemini-2.5-flash-native-audio-latest, which additionally supports
# thinking and affective dialog.
DEFAULT_MODEL = "models/gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Charon"

# Falls back here if the configured language string isn't a Language member.
# Swedish because that's the house this add-on runs in.
FALLBACK_LANGUAGE = Language.SV_SE

# Google's automatic activity detection decides when a user turn starts and
# ends. Pass it nothing and the API runs it at START_SENSITIVITY_HIGH, which
# is why the first live session answered room noise, its own speaker echo and
# stray half-words ("Och?", "Ja.", "Né?", one whole sentence in Portuguese) as
# if they were questions. These maps turn the add-on's plain "low"/"high"
# strings into the enums google-genai wants.
_START_SENSITIVITY = {
    "low": StartSensitivity.START_SENSITIVITY_LOW,
    "high": StartSensitivity.START_SENSITIVITY_HIGH,
}
_END_SENSITIVITY = {
    "low": EndSensitivity.END_SENSITIVITY_LOW,
    "high": EndSensitivity.END_SENSITIVITY_HIGH,
}

# Proactive audio (the model deciding for itself whether it was spoken to)
# lives behind API version v1beta and only on the native-audio models. The
# preview model this add-on defaults to, gemini-3.1-flash-live-preview, does
# NOT support it: Google's own guide says so, and the session is refused
# rather than degraded. Turning it on without moving the model just breaks
# the engine, so a mismatch is refused here, loudly, instead of at 1008.
# MEASURED against the live account 2026-09-09, not taken from the guide.
# Google's own Live API page says these features need "v1beta"; they do not.
# On models/gemini-2.5-flash-native-audio-latest, opening a session with
# `proactivity` on v1beta is refused outright:
#
#   1007 Invalid JSON payload received. Unknown name "proactivity" at
#   'setup': Cannot find field.
#
# and google-genai already defaults to v1beta, so "set it to v1beta" is a
# no-op that looks like a fix. v1alpha accepts proactivity, affective dialog
# and both together. pipecat's own docstring says v1alpha too. Probed all six
# combinations before changing this line.
NATIVE_AUDIO_API_VERSION = "v1alpha"
NATIVE_AUDIO_MODEL_MARKER = "native-audio"

# The native-audio models refuse an explicit language code this house needs.
# Probed live 2026-09-09 against models/gemini-2.5-flash-native-audio-latest:
#
#   None   -> OK        sv     -> 1007 Unsupported language code 'sv'
#   en-US  -> OK        sv-SE  -> 1007 Unsupported language code 'sv-SE'
#   de-DE  -> OK
#
# With no code at all the session opens and the model takes its language from
# what it hears and from the system instruction -- which is written entirely
# in Swedish and says so explicitly. So on these models the language is
# steered by the prompt rather than pinned by a setting. That is a real
# difference worth knowing about, so it is logged, not hidden.
NATIVE_AUDIO_PINS_LANGUAGE = False


def to_gemini_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI Realtime tool definitions to Gemini declarations.

    The two differ in exactly two ways: OpenAI wraps each tool in a
    ``"type": "function"`` envelope, and it allows a tool with no parameters at
    all, where Gemini insists on an object schema even when it is empty.

    Args:
        tools: Tool definitions in OpenAI Realtime shape.

    Returns:
        Function declarations for Gemini Live.
    """
    return [
        {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": _strip_additional_properties(
                tool.get("parameters") or {"type": "object", "properties": {}}
            ),
        }
        for tool in tools
    ]


def _strip_additional_properties(schema: Any) -> Any:
    """Remove every "additionalProperties" key, at any depth.

    Gemini rejects the keyword outright. Home Assistant generates its tool
    schemas, and some of them carry it, so one such tool would take the whole
    session down with it. pipecat's own adapter strips it for exactly this
    reason -- see GeminiLLMAdapter.to_provider_tools_format.
    """
    if isinstance(schema, dict):
        return {
            key: _strip_additional_properties(value)
            for key, value in schema.items()
            if key != "additionalProperties"
        }
    if isinstance(schema, list):
        return [_strip_additional_properties(item) for item in schema]
    return schema


def _resolve_language(language: str) -> Language:
    """Turn the add-on's plain language string into pipecat's enum.

    ``InputParams.language`` is typed ``Optional[Language]`` (a pipecat
    StrEnum), not a bare string -- passing "sv-SE" straight through fails
    pydantic validation the moment a session actually starts. An unresolvable
    value falls back rather than crashing the whole session over a typo in
    add-on config.

    Args:
        language: A BCP-47-ish string like "sv-SE", from ProviderOptions.

    Returns:
        The matching Language member, or FALLBACK_LANGUAGE if none matches.
    """
    try:
        return Language(language)
    except ValueError:
        logger.warning(
            f"⚠️ Unknown Gemini language {language!r}; falling back to {FALLBACK_LANGUAGE}"
        )
        return FALLBACK_LANGUAGE


def _build_vad_params(options) -> GeminiVADParams:
    """Turn the add-on's turn-detection knobs into Gemini's VAD config.

    Every field is always sent. pipecat only attaches
    ``realtime_input_config`` when at least one field is set, and a
    half-filled config would leave the rest at Google's defaults -- which is
    the state this function exists to get away from. An unknown END string
    falls back to LOW (a cut-off sentence is worse than a slower reply). An
    unknown START string falls back to HIGH: measured 2026-10-02, START LOW
    on this device is not "a missed word" but a turn that never opens --
    see ProviderOptions.gemini_vad_start_sensitivity.

    Args:
        options: The ProviderOptions carrying the four gemini_vad_* knobs.

    Returns:
        A fully populated GeminiVADParams.
    """
    start = (options.gemini_vad_start_sensitivity or "high").strip().lower()
    end = (options.gemini_vad_end_sensitivity or "low").strip().lower()
    if start not in _START_SENSITIVITY:
        logger.warning(f"⚠️ Unknown gemini_vad_start_sensitivity {start!r}; using 'high'")
        start = "high"
    if end not in _END_SENSITIVITY:
        logger.warning(f"⚠️ Unknown gemini_vad_end_sensitivity {end!r}; using 'low'")
        end = "low"
    return GeminiVADParams(
        start_sensitivity=_START_SENSITIVITY[start],
        end_sensitivity=_END_SENSITIVITY[end],
        prefix_padding_ms=max(0, int(options.gemini_vad_prefix_padding_ms)),
        silence_duration_ms=max(0, int(options.gemini_vad_silence_duration_ms)),
    )


class ResilientGeminiLiveService(ToolRegistrationMixin, GeminiLiveLLMService):
    """Gemini Live that does not mistake a quiet house for a broken engine.

    The Voice PE is push-to-talk: it streams the microphone only during a turn
    and the follow-up window, so between conversations this add-on sends
    Google nothing at all. Google hangs up on a session it hears nothing from
    -- measured live 2026-09-09 at a very regular ~152 s of silence, with the
    server's own GoAway on the way out. pipecat reconnects in about half a
    second, and the reconnect is invisible to the user, so an idle hang-up is
    ordinary housekeeping for this device, not a failure.

    pipecat counts it as one anyway, and there is a bug in how it forgives
    them: `_check_and_reset_failure_counter` (which clears the count once a
    connection has stood for CONNECTION_ESTABLISHED_THRESHOLD seconds) is
    called only from inside the receive loop, when a message ARRIVES from the
    server. A silent connection delivers no messages, so on an idle device
    the reset never runs no matter how long the socket lived. Three idle
    hang-ups in a row -- about seven and a half quiet minutes -- reach
    MAX_CONSECUTIVE_FAILURES and are pushed as a fatal error. Observed
    2026-09-09 17:05:41: the engine died and the house had no voice until the
    add-on was restarted 45 minutes later.

    The rule pipecat already has is the right one; it just never gets a turn.
    Running it at the moment of failure -- when the connection's lifetime is
    known exactly -- is enough. A connection that stood for its threshold
    before dying is forgiven; three failures inside that window still go
    fatal, which is the case the counter exists for.
    """

    def set_turn_complete_handler(self, handler) -> None:
        """Wire an async callable() told when Gemini finishes a reply.

        The pipeline already carries this news as LLMFullResponseEndFrame --
        but LLMAssistantAggregator sits between this service and PhaseEmitter
        and consumes it, so downstream it never arrives. Measured live
        2026-09-09 19:04: every single turn logged "no end-of-turn from the
        engine". Calling out directly is the only path that reaches the phase
        machine.
        """
        self._on_turn_complete = handler

    async def _handle_msg_turn_complete(self, message) -> None:
        """Pass the engine's own end-of-turn on, then behave as before."""
        await super()._handle_msg_turn_complete(message)
        handler = getattr(self, "_on_turn_complete", None)
        if handler is None:
            return
        try:
            await handler()
        except Exception as e:
            # A phase-machine failure must never break the turn that just
            # succeeded.
            logger.warning(f"⚠️ turn-complete handler failed: {e!r}")

    def drop_language_code(self) -> None:
        """Open the session without pinning a language.

        pipecat has no way to say "no language": `InputParams.language=None`
        is turned into the string "en-US" (its own default) before it ever
        reaches the wire, which would pin a Swedish house to English --
        quietly, since en-US is a code the model DOES accept. The only honest
        way to send nothing is to clear the resolved setting the connect path
        reads. See NATIVE_AUDIO_PINS_LANGUAGE for why this is needed at all.
        """
        self._language_code = None
        self._settings["language"] = None

    async def end_audio_stream(self) -> None:
        """Tell Google the microphone just stopped, and drop what it holds.

        This is Gemini Live's answer to OpenAI Realtime's
        `input_audio_buffer.clear`, and this add-on has never sent it. Google's
        own guide:

            When audio streams pause, send an `audioStreamEnd` event to flush
            any cached audio. The client can then resume sending audio data at
            any time without reconnecting.

        Two things follow from never sending it. Half an utterance -- the
        follow-up window closing mid-sentence -- stays cached on Google's side
        and can be completed into a stale answer on the next wake, which is
        exactly the case the OpenAI path has cleared since 2026-06-12. And a
        pause is indistinguishable from a dead client: this device is
        push-to-talk, so between conversations it simply stops sending, and
        Google hangs up (see this class's own docstring).

        Silent when there is no live session: the caller is a device event,
        and a device event arriving between sessions must never raise.
        """
        session = self._session
        if session is None or self._disconnecting:
            return
        await session.send_realtime_input(audio_stream_end=True)

    async def _handle_connection_error(self, error: Exception) -> bool:
        """Forgive a connection that stood long enough before it dropped."""
        lifetime = None
        if self._connection_start_time:
            lifetime = time.time() - self._connection_start_time
        # Runs pipecat's own stable-connection rule, which the receive loop
        # can only reach while the server is talking to us.
        self._check_and_reset_failure_counter()
        if lifetime is not None and self._consecutive_failures == 0:
            logger.info(
                f"🔁 Gemini socket closed after {lifetime:.0f}s of an idle house — "
                f"reconnecting, not counting it against the engine"
            )
        return await super()._handle_connection_error(error)


def _native_audio_features(options, model: str):
    """The two features that only the native-audio models carry.

    Proactive audio lets the model decide it was not spoken to and say
    nothing; affective dialog lets it match the tone it hears. Both need API
    version v1alpha (see the constant -- the published guide's "v1beta" is
    wrong, and measurably so) AND a native-audio model. Google's guide is explicit that
    Gemini 3.1 Flash Live supports neither, and asking anyway gets the whole
    session refused — so a mismatch loses the feature, never the assistant.

    Args:
        options: The ProviderOptions carrying the two flags.
        model: The resolved model name, which decides whether either feature
            can be asked for at all.

    Returns:
        A (ProactivityConfig|None, affective|None, HttpOptions|None) triple.
        http_options is set whenever either feature is on, since they share
        the same API-version requirement.
    """
    wanted = options.gemini_proactive_audio or options.gemini_affective_dialog
    if not wanted:
        return None, None, None
    if NATIVE_AUDIO_MODEL_MARKER not in model:
        logger.warning(
            f"⚠️ proactive audio / affective dialog are on but {model} supports "
            f"neither (they need a *-{NATIVE_AUDIO_MODEL_MARKER}-* model, e.g. "
            f"models/gemini-2.5-flash-native-audio-latest) — starting WITHOUT them "
            f"rather than letting the session be refused"
        )
        return None, None, None
    proactivity = None
    if options.gemini_proactive_audio:
        logger.info("🤫 Proactive audio ON — the model may stay silent when not addressed")
        proactivity = ProactivityConfig(proactive_audio=True)
    affective = None
    if options.gemini_affective_dialog:
        logger.info("🎭 Affective dialog ON — the model matches the tone it hears")
        affective = True
    return proactivity, affective, HttpOptions(api_version=NATIVE_AUDIO_API_VERSION)


def build(options, tools: List[Dict[str, Any]]) -> GeminiLiveLLMService:
    """Build a configured Gemini Live session for one device.

    Args:
        options: The ProviderOptions carrying every knob from the add-on config.
            Speed and noise reduction have no equivalent here and are ignored.
            Turn detection DOES have one -- Google's automatic activity
            detection -- but it is configured through the gemini_vad_* knobs,
            not the OpenAI semantic_vad ones, so those are ignored too.
        tools: Tool definitions in OpenAI Realtime shape.

    Returns:
        A GeminiLiveLLMService, not yet connected.
    """
    model = options.model or DEFAULT_MODEL
    voice = options.voice or DEFAULT_VOICE
    language = _resolve_language(options.language or "sv-SE")
    vad = _build_vad_params(options)
    proactivity, affective, http_options = _native_audio_features(options, model)
    drop_language = NATIVE_AUDIO_MODEL_MARKER in model and not NATIVE_AUDIO_PINS_LANGUAGE
    params = InputParams(
        max_tokens=options.max_output_tokens or 4096,
        language=language,
        vad=vad,
        proactivity=proactivity,
        enable_affective_dialog=affective,
    )
    logger.info(
        f"🔧 Gemini Live session: model={model} voice={voice} "
        f"lang={'(from prompt)' if drop_language else language} tools={len(tools)}"
    )
    logger.info(
        f"🎚️ Gemini turn detection: start={vad.start_sensitivity} "
        f"end={vad.end_sensitivity} prefix={vad.prefix_padding_ms}ms "
        f"silence={vad.silence_duration_ms}ms"
    )
    service = ResilientGeminiLiveService(
        api_key=options.api_key,
        model=model,
        voice_id=voice,
        system_instruction=options.instructions,
        # One wrapper deeper than it looks: the Live API takes a LIST OF TOOLS,
        # each of which carries its function declarations. Handing it the bare
        # declarations makes google-genai reject every one of them as an extra
        # field, and the session never opens. pipecat's own adapter wraps them
        # the same way -- see GeminiLLMAdapter.to_provider_tools_format.
        tools=[{"function_declarations": to_gemini_tools(tools)}] if tools else None,
        start_audio_paused=False,
        params=params,
        # Only passed when proactive audio asked for it; None otherwise leaves
        # google-genai on its own default version.
        http_options=http_options,
    )
    if drop_language:
        logger.info(
            f"🌍 {model} refuses an explicit '{language}' — sending no language code "
            f"and letting the Swedish system prompt steer it"
        )
        service.drop_language_code()
    return service
