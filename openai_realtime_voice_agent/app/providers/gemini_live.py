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
from pipecat.frames.frames import OutputAudioRawFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiModalities,
    GeminiVADParams,
    InputParams,
)
from pipecat.transcriptions.language import Language

from app import eko_vakt as _eko
from app import sprakkoll
from app.providers.local_turns import PRE_END_MS, LocalTurns, LocalTurnsMixin
from app.providers.sovlage import SovlageMixin
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


class ResilientGeminiLiveService(SovlageMixin, LocalTurnsMixin, ToolRegistrationMixin, GeminiLiveLLMService):
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

    _transkr_start: str = ""
    _transkr_kollad: bool = False
    _tur_text: str = ""  # everything the model has heard this turn, for gating tools on what it understood

    def tur_text(self) -> str:
        return self._tur_text

    async def _handle_msg_input_transcription(self, message) -> None:
        """Pass it on, and log (once per turn) when the model's own transcription starts in another language
        than Swedish: only the first words, no audio. Kitchen 2026-10-09: an answer began in Italian."""
        try:
            self._tur_text += message.server_content.input_transcription.text or ""
            if not self._transkr_kollad:
                self._transkr_start += message.server_content.input_transcription.text or ""
                if len(sprakkoll.forsta_orden(self._transkr_start)) >= sprakkoll.FORSTA_ORD:
                    self._transkr_kollad = True
                    hit = sprakkoll.annat_sprak(self._transkr_start)
                    if hit:
                        logger.warning(f"🌐 Gemini's transcription starts in another language ({hit[0]}): "
                                       f"'{' '.join(hit[1])}' (the answer language is pinned to Swedish)")
        except Exception as e:  # logging must never touch the turn
            logger.debug(f"transcription language check failed: {e!r}")
        await super()._handle_msg_input_transcription(message)

    _svar_text: str = ""
    svar = None  # eko_vakt.Svar: what he has said, for the full-duplex echo net (US-060)

    async def _handle_msg_output_transcription(self, message) -> None:
        """Keep what the model says: for the journal when the turn ends (raawr D-97), and for the echo net."""
        try:
            text = message.server_content.output_transcription.text or ""
            self._svar_text += text
            if self.svar is None:
                self.svar = _eko.Svar()
            self.svar.mata(text)
        except Exception as e:  # logging must never touch the turn
            logger.debug(f"output transcription not kept: {e!r}")
        await super()._handle_msg_output_transcription(message)

    async def _handle_msg_tool_call(self, message) -> None:
        self._answer_after_tool = True
        self._turn_rescue = None  # a tool may have acted already: the turn is no longer safe to replay
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
        if self._svar_text.strip():
            logger.info(f"💬 Björn sa: {self._svar_text.strip()[:400]}")
        self._svar_text = ""
        if self.svar is not None:
            self.svar.klar()
        self._transkr_start, self._transkr_kollad, self._tur_text = "", False, ""  # next turn: look again
        self._turn_rescue = None  # answered (also as text without sound): never replay it
        self.turer_klara += 1
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
    # Called (plain, no args) when a held turn has been quiet for PRE_END_MS but is not
    # over yet: bana 0 starts its speech-to-text early (raawr US-032).
    on_user_turn_pre_end = None
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
        if self.eko_vakt is not None and await self._eko_kandidat(frame, event):
            return
        if self._niva_slut:
            if event in ("start", "end") or getattr(turns, "_speaking", False):
                self._niva_slut = 0.0  # the turn detector has the speech: its own end comes
            elif time.monotonic() >= self._niva_slut:
                self._niva_slut = 0.0
                event = "end"
        if event == "end" and self._tidigt:
            self._tidigt = False  # this turn was ended early (end_early); this is only its normal end
            event = None
        elif event == "start":
            self._tidigt = False
            self._avgjord = False
        elif self._tidigt and getattr(turns, "_pre_speaking", False):
            # He went on talking after the turn was ended early. The detector would call it one long
            # speech and never open the continuation: start it over so the next speech is a turn of its own.
            logger.warning("⚠️ speech resumed after the early turn end: the continuation starts a new turn")
            self._tidigt = False
            turns.reset()
            if getattr(self, "_held", None):  # bana 0 still holds the decided turn: keep the rest apart
                self._forts = []
        if event == "preend":
            self._preend_t = time.monotonic()
        held = getattr(self, "_held", None)
        if held is None and self._forts is not None:
            # The first turn's decision ended (a miss or a ping, not drop_turn) while the continuation was being
            # collected: the continuation is the held turn now and goes on through the branch below.
            held = self._held = self._forts
            self._forts = None
            if event == "start" and self.on_user_turn_start is not None:
                self.on_user_turn_start()
        if held is not None and self._forts is not None:
            self._forts.append(frame)  # the continuation of an early-ended turn: its own turn, decided after the first
            if event == "start" and self.on_user_turn_start is not None:
                self.on_user_turn_start()
            if event == "end":
                forts, self._forts = self._forts, None
                asyncio.get_running_loop().create_task(self._fortsatt(forts))
            return
        if held is not None:
            # Bana 0 holds this turn; keep collecting until it decides. Speech
            # that resumes during the decision belongs to the same turn.
            held.append(frame)
            if event == "preend" and self.on_user_turn_pre_end is not None:
                self.on_user_turn_pre_end()
            if event == "end":
                self._avgjord = True
                self._tider_start()
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
            self._tider_start()
            await self.push_frame(UserStoppedSpeakingFrame())
            await self._end_activity()

    # Full duplex, layer 2 (US-060, D-22). eko_vakt(): True while he replies or just after. eko_dom(pcm): True = the
    # speech heard is his own words. Both are set by the handler when BARGE_IN=1; None = this does nothing.
    eko_vakt = None
    eko_dom = None
    on_barge_in = None  # called once when a person's speech is confirmed over his reply
    # Speech AFTER the turn detector said "speech", before the candidate is written out and judged. The pre-roll (up to
    # 1 s from before he spoke) used to count too, so the speech-to-text got the first 0.02-0.3 s of the word: Henrik's
    # "vänta" was written 'Tack.' twice (Kontoret 2026-10-13 09:48). Replayed: 0.3 s 'Välta?', 0.5 s and 0.7 s 'Vänta.'
    EKO_MIN_S = 0.5
    _kandidat = None
    _kandidat_fore = 0
    _kandidat_task = None
    _kandidat_slut = False

    @staticmethod
    def _for_tyst(frames) -> bool:
        """The loudest 0.25 s of the candidate is under the floor: room noise or a faint echo, not a person."""
        from app.avbrott import dbfs
        pcm = b"".join(f.audio for f in frames)
        steg = [dbfs(pcm[i:i + 8000]) for i in range(0, max(len(pcm) - 8000, 1), 8000)]
        return bool(steg) and max(steg) < _eko.niva_dbfs()

    async def _eko_kandidat(self, frame, event) -> bool:
        """Speech that starts over his reply is a candidate, not yet a turn: it is written out locally and compared with
        what he said. Echo is dropped; a person is let in as a normal turn start. True = this frame was taken."""
        turns = self._turns
        kand = self._kandidat
        nu = time.monotonic()
        if self.niva_person is not None and nu - self._niva_sist > 2.0 and self.niva_person():
            # Loud over a canceller that works: a person, now. No waiting for the turn detector or the text.
            self._niva_sist = nu
            if kand is None:
                pre = self._take_preroll()
                kand = [type(frame)(audio=pre, sample_rate=frame.sample_rate, num_channels=frame.num_channels)]
            if self._kandidat_task is not None:
                self._kandidat_task.cancel()
            self._kandidat = self._kandidat_task = None
            self._kandidat_slut = False
            logger.info("🔊 over his reply: loud over a working canceller -> a person, interrupts")
            # The turn detector may never call a shouted "stopp" speech; then nothing would end the turn either.
            self._niva_slut = nu + getattr(turns, "silence_s", 0.8) + 0.6
            await self._slapp_in(kand + [frame], False)
            return True
        if kand is None:
            # _niva_slut: the turn is already open, on level; the detector's late "start" is the same speech.
            if event != "start" or self._niva_slut or not self.eko_vakt():
                return False
            pre = self._take_preroll()
            self._kandidat = [type(frame)(audio=pre, sample_rate=frame.sample_rate, num_channels=frame.num_channels), frame]
            self._kandidat_fore = len(pre)
            self._kandidat_slut, self._kandidat_task = False, None
            return True
        kand.append(frame)
        if event == "end":
            self._kandidat_slut = True
        if self._kandidat_task is None:
            if self._kandidat_slut or sum(len(f.audio) for f in kand) - self._kandidat_fore >= self.EKO_MIN_S * frame.sample_rate * 2:
                pcm = b"".join(f.audio for f in kand)
                self._kandidat_task = asyncio.get_running_loop().create_task(self.eko_dom(pcm, frame.sample_rate))
            return True
        if not self._kandidat_task.done():
            return True
        try:
            eko = bool(self._kandidat_task.result())
        except Exception as e:
            logger.warning(f"⚠️ echo net failed ({e!r}): the speech is let through")
            eko = False
        frames, slut = self._kandidat, self._kandidat_slut
        self._kandidat = self._kandidat_task = None
        self._kandidat_slut = False
        if not eko and self._for_tyst(frames):
            logger.info("🔇 over his reply: too quiet to be a person, ignored")
            eko = True
        if eko:
            turns.reset()
            return True
        await self._slapp_in(frames, slut)
        return True

    niva_person = None  # set by the handler: True = a loud sound over a working canceller, right now, while he replies
    _niva_sist = 0.0
    _niva_slut = 0.0    # a turn opened on level alone ends here unless the turn detector takes it over

    async def _slapp_in(self, frames, slut) -> None:
        """A person spoke over his reply: cut him and let the speech in as a normal turn start."""
        if self.on_barge_in is not None:
            self.on_barge_in()
        await self.push_frame(UserStartedSpeakingFrame())
        if self.on_user_turn_start is not None:
            self.on_user_turn_start()
        if self.on_user_turn_end is not None:
            self._held = list(frames)
            if slut:
                self._avgjord = True
                self._tider_start()
                await self.push_frame(UserStoppedSpeakingFrame())
                self._decide_turn()
            return
        self._activity_open = True
        await self._send_activity(activity_start=ActivityStart())
        for f in frames:
            await self._send_pcm(f, f.audio)
        if slut:
            self._tider_start()
            await self.push_frame(UserStoppedSpeakingFrame())
            await self._end_activity()

    async def _end_activity(self) -> None:
        self._activity_open = False
        self._answer_after_tool = False  # a new question; the old step is moot
        self._reply_awaited_at = time.monotonic()
        logger.debug("🎙️ local VAD: silence → activityEnd")
        await self._send_activity(activity_end=ActivityEnd())
        self.arm_silence_ack()

    tider = None  # the connection's TurnTider (app/turn_tider.py), set by the handler

    def _tider_start(self, tystnad_s: Optional[float] = None) -> None:
        if self.tider is not None and self._turns is not None:
            self.tider.start(tystnad_s if tystnad_s is not None else getattr(self._turns, "silence_s", 0.8))

    _forts = None  # frames of a continuation that came while the early-ended turn was still being decided

    async def _fortsatt(self, frames) -> None:
        """Decide the continuation as a turn of its own once the first turn's decision is done."""
        task = self._turn_end_task
        if task is not None:
            await asyncio.wait({task})
        if getattr(self, "_held", None) is not None:  # something else took the turn meanwhile
            return
        self._held = frames
        self._avgjord = True
        self._tider_start()
        await self.push_frame(UserStoppedSpeakingFrame())
        self._decide_turn()

    _avgjord = False  # the held turn has been decided (early or at the normal end)
    _preend_t = 0.0
    _tidigt = False  # the held turn was ended at "preend"; the detector's own "end" is then swallowed

    def end_early(self) -> bool:
        """End the held turn now, at "preend", because bana 0's early speech-to-text is a whole command
        (raawr US-047). Refused when no turn is held or the speaker has started again. True = ended."""
        held = getattr(self, "_held", None)
        turns = self._turns
        if not held or turns is None or self.on_user_turn_end is None or self._tidigt or self._avgjord:
            return False  # no turn, or it is already decided (the text came after the normal end)
        if getattr(turns, "_pre_speaking", False):
            return False
        self._tidigt = True
        self._avgjord = True
        # The speech ended PRE_END_MS before "preend" was said; the text may have come later than that.
        self._tider_start(PRE_END_MS / 1000 + max(0.0, time.monotonic() - self._preend_t))
        asyncio.get_running_loop().create_task(self.push_frame(UserStoppedSpeakingFrame()))
        self._decide_turn()
        return True

    # Seconds of speech Live has given (the daily cap counts these), turns it has finished, and the
    # event a fed text waits on for its first sound (core_strom.LiveMatare).
    ljud_s = 0.0
    turer_klara = 0
    _forsta_ljud: Optional[asyncio.Event] = None

    async def mata_text(self, text: str) -> None:
        """Feed `text` to the connected session as a message to answer (a Core sentence to read aloud)."""
        self._forsta_ljud = asyncio.Event()
        await self._send_activity(text=text)

    async def vanta_forsta_ljud(self, timeout: float) -> bool:
        """True when sound came after the last `mata_text` within `timeout` seconds."""
        try:
            await asyncio.wait_for(self._forsta_ljud.wait(), timeout)
            return True
        except (asyncio.TimeoutError, AttributeError):
            return False

    async def push_frame(self, frame, *args, **kwargs):
        """Tell the turn's timing line when the model's first audio leaves for the device."""
        if isinstance(frame, OutputAudioRawFrame):
            self._turn_rescue = None  # the model has begun to answer
            self.ljud_s += len(frame.audio) / (2 * (frame.sample_rate or 24000))
            if self._forsta_ljud is not None:
                self._forsta_ljud.set()
            if self.tider is not None:
                self.tider.mark("modell")
        return await super().push_frame(frame, *args, **kwargs)

    def held_seconds(self) -> Optional[float]:
        """Seconds of mic audio held back for bana 0 (what the model would be given on a
        miss); None when no turn is held."""
        held = getattr(self, "_held", None)
        if not held:
            return None
        return sum(len(f.audio) / (2 * (f.sample_rate or 16000)) for f in held)

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

    async def _vaken_for_tur(self) -> bool:
        """A turn is answered on an awake engine. Kitchen 2026-10-09 16:51:16: the follow-up
        question came after the engine had been put to sleep, `_send_activity` returns quietly
        when there is no session, and the device waited 15 s for a reply that could not come.
        A sleeping engine is woken first; if it will not wake, say so in the log."""
        if not getattr(self, "sover", False):
            return True
        if await self.vakna() or not getattr(self, "sover", False):
            return True
        logger.warning("⚠️ a turn arrived while the cloud engine sleeps and it would not wake; the turn is lost")
        return False

    async def answer_turn(self) -> None:
        """Bana 0 missed: give Google the held turn and let the model answer."""
        held, self._held = getattr(self, "_held", None), None
        if not held:
            return
        if not await self._vaken_for_tur():
            return
        self._turn_rescue = (list(held), time.monotonic())
        await self._send_activity(activity_start=ActivityStart())
        for frame in held:
            await self._send_pcm(frame, frame.audio)
        await self._end_activity()

    # The audio of the turn the model was last asked to answer, until it has begun to answer (first
    # sound), acted (a tool call) or finished. If the device's link drops in between, the handler
    # hands it to the next connection so the turn is answered instead of dying (a reconnect must
    # never kill a turn, kitchen 2026-10-09).
    _turn_rescue: Optional[tuple] = None
    TURN_RESCUE_MAX_AGE_S = 20.0

    def take_rescue(self) -> Optional[list]:
        """The frames of a turn the model never began to answer, once; None if there is none or it is stale."""
        rescue, self._turn_rescue = self._turn_rescue, None
        if rescue is None or time.monotonic() - rescue[1] > self.TURN_RESCUE_MAX_AGE_S:
            return None
        return rescue[0]

    async def ping_and_answer(self, ping: str) -> None:
        """Bana 0 hit with BANA0_PING: tell the model what the fast track did, then give it the held audio
        too, so it answers personally without repeating the tool call and can correct a misheard order."""
        if not getattr(self, "_held", None):
            return
        if not await self._vaken_for_tur():
            await self.drop_turn()  # the audio is lost either way: do not let the miss path send it without the line
            return
        await self._send_activity(text=ping)
        await self.answer_turn()
        # The hit is already done: a replay of this audio after a dropped link would reach the model without
        # the line (G on #42) and could do it a second time.
        self._turn_rescue = None

    async def answer_turn_text(self, text: str) -> None:
        """Bana 0 missed and the local speech-to-text has the words: give Google THOSE
        instead of the held audio (LOCAL_TEXT_TO_MODEL, raawr US-032 experiment).
        Live 2026-10-09: Gemini heard 'Kan du ta den på toalettet kanske?' where the
        local STT, on the same audio, got 'Kan du tända kontoret kanske?'. The reply
        is still audio; only the model's ears change."""
        held, self._held = getattr(self, "_held", None), None
        if not held:
            logger.warning("⚠️ bana0: the locally heard text had no held turn to answer; nothing was sent")
            return
        if not await self._vaken_for_tur():
            return
        # Variant B (2026-10-09 10:30): text on its own, no activity signals. Variant A
        # (activityStart, text, activityEnd) made Google close the socket: 1007 'Precondition
        # check failed'.
        await self._send_activity(text=text)
        self._reply_awaited_at = time.monotonic()
        self.arm_silence_ack()

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
        forts, self._forts = getattr(self, "_forts", None), None
        self._held = None
        self._turn_rescue = None
        self._activity_open = False
        # A continuation being collected (see _fortsatt) is not the dropped turn: the reset below makes the
        # detector open it again, so what was said of it so far goes back in as the pre-roll.
        self._preroll = bytearray(b"".join(f.audio for f in forts)) if forts else bytearray()
        self._transkr_start, self._transkr_kollad, self._tur_text = "", False, ""  # a dropped turn must not mute the next one's check
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

    async def _ateranslut(self, forut: bool) -> None:  # SovlageMixin
        """Resume the earlier conversation with Google's handle when there is one."""
        handle = getattr(self, "_session_resumption_handle", None) if forut else None
        start = time.monotonic()
        self._vanta_till = start + 3.0
        await self._connect(handle)
        if not handle or await self._ar_uppkopplad():
            return
        task = getattr(self, "_connection_task", None)
        if task is None or not task.done():
            return  # no answer yet: the net, not Google. One attempt per wake.
        # Google refused the handle (live 2026-10-07: 1011 in 0.9 s). The wake
        # was lost and the handle stayed for the next one. Start a fresh
        # conversation now, inside ConnectionRecovery.VAKNA_TIMEOUT_S (5 s).
        logger.warning("☁️ Gemini refused the resumption handle — starting a fresh conversation")
        self._session_resumption_handle = None
        self._connection_task = None
        self._vanta_till = min(time.monotonic() + 3.0, start + 3.5)  # + teardown ≤ 1 s < 5 s
        await self._connect(None)

    async def _ar_uppkopplad(self, timeout: float = 3.0) -> bool:  # SovlageMixin
        """pipecat connects in a background task; wait for the session or its failure.

        Waits until the deadline _ateranslut set for this wake, so a second
        call (sovlage after _ateranslut) does not add another full wait.
        """
        slut = getattr(self, "_vanta_till", None) or time.monotonic() + timeout
        while time.monotonic() < slut:
            if self._session:
                return True
            task = getattr(self, "_connection_task", None)
            if task is not None and task.done():
                return bool(self._session)
            await asyncio.sleep(0.05)
        return bool(self._session)

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
    turns = LocalTurns.create(int(options.gemini_turn_silence_ms), pre_ms=PRE_END_MS)
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
