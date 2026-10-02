"""Google Gemini Live, wearing the same shape as the OpenAI session.

Proven against the live account 2026-09-08: all 52 tools accepted at once --
this add-on's 8 plus Home Assistant's 44 -- and the model picked search_home by
itself when asked about the car's battery, then answered in Swedish.

⛔ pipecat 0.0.97 defaults to models/gemini-2.0-flash-live-001, which the API
refuses with `1008 ... not supported for bidiGenerateContent`. Every live model
is a preview name and will be retired in turn, so the model is a setting and
this default is only the one that worked on the day it was written.
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from google.genai.types import (
    ActivityEnd,
    ActivityStart,
    EndSensitivity,
    HttpOptions,
    ProactivityConfig,
    StartSensitivity,
    ThinkingConfig,
)
from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiModalities,
    GeminiVADParams,
    InputParams,
)
from pipecat.transcriptions.language import Language

from app.providers.local_turns import LocalTurns, LocalTurnsMixin
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

# The native-audio models THINK by default (dynamic budget), and pipecat sends
# no thinking_config unless told to. Live 2026-10-02 14:35:46 on
# models/gemini-2.5-flash-native-audio-latest, "släck kontoret": the thought
# text names the exact tool ("intent__HassTurnOff is more aligned with the
# direct 'släck' command") -- so the declarations arrived -- and the model
# then SAYS "Släckt i kontoret" without ever sending a toolCall. Same 59 tools
# on gemini-3.1-flash-live-preview were called (12:53 kalender_sok, 14:30
# web_search). The model reasons its way to the action and narrates it
# instead of doing it. Budget 0 turns thinking off, which Google documents
# for the native-audio models; include_thoughts=False keeps the summaries
# off the wire as well.
NATIVE_AUDIO_THINKING = ThinkingConfig(thinking_budget=0, include_thoughts=False)

# Spoken through the add-on's own TTS lane when Google drops the socket while
# a turn is in flight (1011 "Internal error encountered", 2026-10-02 14:30:47:
# the wake got nothing back, ever). Short, honest, and it tells him what to do.
TURN_LOST_LINE = "Tappade tråden mot Google mitt i. Säg det igen."
# How long after activityEnd a dropped socket still counts as a lost turn. A
# reply with a slow tool (web search) can take 20 s; past this the drop is the
# idle hang-up this class already forgives quietly.
TURN_LOST_WINDOW_S = 45.0


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


class ResilientGeminiLiveService(LocalTurnsMixin, ToolRegistrationMixin, GeminiLiveLLMService):
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

    # True from a toolCall until the model's next audio. gemini-3.8-live (the
    # 3.1 live backend) closes the tool step with an EMPTY turn_complete --
    # usage metadata, no audio, 0.01 s after our tool response -- and speaks
    # the answer in a NEW turn (probed on the live key 2026-10-02, twice for a
    # two-call turn). Taken as the end of the reply, that empty one cleared
    # the lost-turn clock, flushed the follow-up request before the answer
    # was spoken and let PhaseEmitter go idle after a spoken preamble.
    _answer_after_tool = False

    async def _handle_msg_tool_call(self, message) -> None:
        self._answer_after_tool = True
        await super()._handle_msg_tool_call(message)

    async def _handle_msg_turn_complete(self, message) -> None:
        """Pass the engine's own end-of-turn on, then behave as before.

        Not the empty one that closes a tool step (see _answer_after_tool):
        that is swallowed whole, pipecat's bookkeeping included, so the
        answer's turn continues the same reply for everything downstream.
        """
        # ponytail: a tool step the model never follows with audio stays open
        # until the next activityEnd; PhaseEmitter's thinking watchdog (15 s)
        # and mid-turn grace (8 s) are the bound on the device.
        if self._answer_after_tool:
            logger.info("⏳ empty turn_complete after a tool call — the answer comes in a new turn")
            return
        self._reply_awaited_at = None
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

    # Set by build() when local turn detection is on (the default). None =
    # Google's automatic activity detection, the pre-0.22.5 behaviour.
    _turns: Optional[LocalTurns] = None
    _activity_open = False

    # Bana 0 (raawr US-016), same hooks as the OpenAI service. With
    # on_user_turn_end set, a turn's audio is HELD here instead of streamed:
    # the hook decides, and calls answer_turn() (a miss: Google gets
    # activityStart, the held audio, activityEnd) or drop_turn() (Home
    # Assistant already did it: Google never hears the order, so it can
    # neither answer it nor carry it into the next turn and do it twice).
    on_user_turn_start = None  # plain callable
    on_user_turn_end = None  # async callable
    _turn_end_task = None
    # Told (async, no args) when the socket dies with a turn in flight.
    on_turn_lost = None
    _reply_awaited_at: Optional[float] = None

    async def _send_activity(self, **kw) -> None:
        if self._disconnecting or not self._session:
            return
        try:
            await self._session.send_realtime_input(**kw)
        except Exception as e:
            await self._handle_send_error(e)

    async def _send_pcm(self, frame, audio: bytes) -> None:
        if audio:
            await super()._send_user_audio(
                type(frame)(audio=audio, sample_rate=frame.sample_rate,
                            num_channels=frame.num_channels)
            )

    async def _send_user_audio(self, frame):
        """With local turns: send audio only inside an activity we opened."""
        turns = self._turns
        if turns is None:
            return await super()._send_user_audio(frame)
        event = turns.feed(frame.audio)
        held = getattr(self, "_held", None)
        if held is not None:
            # Bana 0 holds this turn; keep collecting until it decides. Speech
            # that resumes during the decision belongs to the same turn.
            held.append(frame)
            if event == "end":
                await self.push_frame(UserStoppedSpeakingFrame())
                self._decide_turn()
            return
        if not self._activity_open:
            if event != "start":
                self._keep_preroll(frame)
                return
            preroll = self._take_preroll()
            # Phase "listening" now, from our own VAD. The device closes its
            # follow-up window unless it hears "listening" in time, and on
            # Gemini that used to come only from the input transcript --
            # which can arrive after the window has already cut him off.
            await self.push_frame(UserStartedSpeakingFrame())
            if self.on_user_turn_start is not None:
                self.on_user_turn_start()
            if self.on_user_turn_end is not None:
                self._held = [type(frame)(audio=preroll, sample_rate=frame.sample_rate,
                                          num_channels=frame.num_channels), frame]
                return
            self._activity_open = True
            logger.debug("🎙️ local VAD: speech → activityStart")
            await self._send_activity(activity_start=ActivityStart())
            await self._send_pcm(frame, preroll)
        await super()._send_user_audio(frame)
        if event == "end":
            await self.push_frame(UserStoppedSpeakingFrame())
            await self._end_activity()

    async def _end_activity(self) -> None:
        self._activity_open = False
        self._answer_after_tool = False  # a new question; the old step is moot
        self._reply_awaited_at = time.monotonic()
        logger.debug("🎙️ local VAD: silence → activityEnd")
        await self._send_activity(activity_end=ActivityEnd())
        self.arm_silence_ack()

    def _decide_turn(self) -> None:
        """Run bana 0's decision once per held turn, off the audio path."""
        if self._turn_end_task is None or self._turn_end_task.done():
            self._turn_end_task = asyncio.get_running_loop().create_task(self._run_turn_end())

    async def _run_turn_end(self) -> None:
        try:
            await self.on_user_turn_end()
        finally:
            # A hook that died without deciding must not strand the turn.
            if getattr(self, "_held", None):
                await self.answer_turn()

    async def answer_turn(self) -> None:
        """Bana 0 missed: give Google the held turn and let the model answer."""
        held, self._held = getattr(self, "_held", None), None
        if not held:
            return
        await self._send_activity(activity_start=ActivityStart())
        for frame in held:
            await self._send_pcm(frame, frame.audio)
        await self._end_activity()

    async def drop_turn(self) -> None:
        """Forget the current turn: held audio, or an activity already open.

        An open activity is abandoned without an activityEnd, so the model is
        never asked to answer it.
        """
        # ponytail: whether Google accepts the next activityStart without an
        # activityEnd for the abandoned one is unmeasured; a refusal surfaces
        # as a reconnect in the journal. Bana 0 holds instead of streaming so
        # its hits never take this path.
        if self._activity_open:
            logger.info("🧽 open Gemini activity abandoned (device dropped the input)")
        self._held = None
        self._activity_open = False
        self._preroll = bytearray()
        self.cancel_silence_ack()
        if self._turns is not None:
            self._turns.reset()

    async def _handle_msg_model_turn(self, msg) -> None:
        """Speak audio only; never let a text part pose as the answer.

        In an AUDIO session the answer is the audio, and its words come back
        as output transcription. Text parts there are the model's thinking:
        live 2026-10-02 the assistant transcript read "**Analyzing Command
        Execution** I've determined the user intends to turn off..." in front
        of the spoken reply, and that text went into the conversation context
        as if it had been said. pipecat also reads only parts[0], so an audio
        part behind a thought part was lost. Each part is handled on its own.
        """
        parts = list(msg.server_content.model_turn.parts or [])
        if any(p.inline_data and p.inline_data.data for p in parts):
            self._answer_after_tool = False
        if self._settings.get("modalities") != GeminiModalities.AUDIO:
            return await super()._handle_msg_model_turn(msg)
        for part in parts:
            if part.text:
                logger.debug(f"💭 Gemini text part dropped (not the answer): {part.text[:80]!r}")
                continue
            msg.server_content.model_turn.parts = [part]
            await super()._handle_msg_model_turn(msg)

    # Set by create_service: a callable() -> the full system instruction,
    # Idag block included. Called on every connect, so each new socket gets
    # the current time and the last fetched weather and calendar.
    instructions_provider = None
    instructions_at = 0.0

    async def _connect(self, session_resumption_handle=None):
        """Render the instruction again, then connect as pipecat does.

        Measured 2026-10-02 on the live key: a session resumed with its
        handle and a NEW system instruction follows the new one, with the
        conversation intact. That is how the Idag block is refreshed without
        touching a live session.
        """
        if self.instructions_provider is not None and not self._session:
            try:
                self._system_instruction_from_init = self.instructions_provider()
                self.instructions_at = time.monotonic()
            except Exception as e:
                logger.warning(f"⚠️ instruction not re-rendered, keeping the last ({e!r})")
        await super()._connect(session_resumption_handle)

    async def refresh_instructions(self) -> bool:
        """Reconnect to pick up a new instruction, only between turns.

        Not while he speaks, while bana 0 holds a turn, or while an answer
        is awaited. The caller also checks the device is idle.
        """
        busy = self._activity_open or getattr(self, "_held", None) or self._reply_awaited_at
        if busy or not self._session or self._disconnecting:
            return False
        await self._reconnect()
        return True

    async def _handle_session_ready(self, session):
        """A new socket knows nothing of an activity the old one had open."""
        self._activity_open = False
        self._answer_after_tool = False
        self._preroll = bytearray()
        if self._turns is not None:
            self._turns.reset()
        await super()._handle_session_ready(session)

    async def end_audio_stream(self, keep_speech: bool = False) -> None:
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

        Args:
            keep_speech: The follow-up window closed, it was not a stop word.
                With local turns, speech our VAD is sure of is then ended and
                answered (through bana 0 when it holds the turn) instead of
                dropped: the device shut the mic on him, he did not take the
                question back. Nothing is cached to go stale, because the
                answer comes now.
        """
        if self._turns is not None:
            # Manual activity mode: audioStreamEnd belongs to Google's
            # automatic detection and is not sent.
            if keep_speech and getattr(self, "_held", None):
                logger.info("🧽 follow-up closed mid-utterance — answering it, not dropping it")
                self._decide_turn()
                self._turns.reset()  # the mic is shut; the next wake starts clean
                return
            if keep_speech and self._activity_open:
                logger.info("🧽 follow-up closed mid-utterance — answering it, not dropping it")
                await self._end_activity()
                self._turns.reset()
                return
            await self.drop_turn()
            return
        session = self._session
        if session is None or self._disconnecting:
            return
        await session.send_realtime_input(audio_stream_end=True)

    async def _handle_connection_error(self, error: Exception) -> bool:
        """Forgive a connection that stood long enough before it dropped."""
        lifetime = None
        if self._connection_start_time:
            lifetime = time.time() - self._connection_start_time
        # A turn was in flight: he is speaking (activity open) or waiting for
        # the answer to one. Google takes it down with the socket -- the new
        # session never saw it -- so say so instead of leaving him in silence.
        awaited = self._reply_awaited_at
        lost = self._activity_open or (
            awaited is not None and time.monotonic() - awaited < TURN_LOST_WINDOW_S
        )
        self._reply_awaited_at = None
        if lost:
            logger.warning(f"💔 Gemini dropped the socket mid-turn ({error}) — telling him")
            if self.on_turn_lost is not None:
                asyncio.get_running_loop().create_task(self.on_turn_lost())
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


def _google_search_on(model: str) -> bool:
    """GEMINI_GOOGLE_SEARCH, or unset: on for the 3.x models, off for 2.5.

    On 2.5 native audio (-latest = preview-12-2025), google_search + function
    tools + thinking_budget 0 kill the socket with 1011 before the toolCall
    whenever a house question also smells like a web fact ("Vilken
    temperatur är det i kontoret?" 10/12, "Vad kostar elen?" 3/3; probed on
    the live key 2026-10-02). Google-side; Google documents built-in +
    custom tool combinations for Gemini 3 only. gemini-3.8-live and
    3.1-flash-live-preview: 0 of 15 with grounding, and grounded web answers.
    """
    value = os.environ.get("GEMINI_GOOGLE_SEARCH", "").strip().lower()
    if value:
        return value == "true"
    return "gemini-3" in model


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
    search = _google_search_on(model)
    gemini_tools = []
    if search:
        # Google's own search replaces our web_search round trip (Gemini ->
        # OpenAI Responses -> back). Never both: the model would pick either.
        tools = [t for t in tools if t.get("name") != "web_search"]
    if tools:
        # One wrapper deeper than it looks: the Live API takes a LIST OF
        # TOOLS, each of which carries its function declarations. Handing it
        # the bare declarations makes google-genai reject every one of them
        # as an extra field, and the session never opens. pipecat's own
        # adapter wraps them the same way -- see
        # GeminiLLMAdapter.to_provider_tools_format.
        gemini_tools.append({"function_declarations": to_gemini_tools(tools)})
    if search:
        gemini_tools.append({"google_search": {}})
    language = _resolve_language(options.language or "sv-SE")
    turns = LocalTurns.create(int(options.gemini_turn_silence_ms))
    vad = GeminiVADParams(disabled=True) if turns else _build_vad_params(options)
    proactivity, affective, http_options = _native_audio_features(options, model)
    native = NATIVE_AUDIO_MODEL_MARKER in model
    drop_language = native and not NATIVE_AUDIO_PINS_LANGUAGE
    params = InputParams(
        # Only the native-audio models: the 3.x models call their tools as
        # they are. Probed 2026-10-02 on gemini-3.8-live: thinking_level is
        # refused (1007 "not supported for this model"), thinking_budget 0
        # and 512 are accepted, so nothing is sent; language sv and sv-SE,
        # voice Charon and proactivity are accepted, affective dialog is
        # refused (1007) -- _native_audio_features never asks for it there.
        thinking=NATIVE_AUDIO_THINKING if native else None,
        max_tokens=options.max_output_tokens or 4096,
        language=language,
        vad=vad,
        proactivity=proactivity,
        enable_affective_dialog=affective,
    )
    logger.info(
        f"🔧 Gemini Live session: model={model} voice={voice} "
        f"lang={'(from prompt)' if drop_language else language} tools={len(tools)}"
        f"{' +google_search' if search else ''}"
        f"{' thinking=off' if native else ''}"
    )
    if turns:
        logger.info(
            f"🎚️ Gemini turn detection: LOCAL (Silero, end after "
            f"{options.gemini_turn_silence_ms}ms silence) → activityStart/activityEnd"
        )
    else:
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
        tools=gemini_tools or None,
        start_audio_paused=False,
        params=params,
        # Only passed when proactive audio asked for it; None otherwise leaves
        # google-genai on its own default version.
        http_options=http_options,
    )
    service._turns = turns
    if drop_language:
        logger.info(
            f"🌍 {model} refuses an explicit '{language}' — sending no language code "
            f"and letting the Swedish system prompt steer it"
        )
        service.drop_language_code()
    return service
