"""One narrow door to every voice engine.

Everything that differs between OpenAI Realtime and Gemini Live lives behind
this module. No code outside it may know which engine is running -- except the
router, which only ever handles names.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# The engine names used in the add-on config, in logs and by the router.
OPENAI = "openai"
GEMINI = "gemini"
# xAI Grok Voice: OpenAI Realtime's protocol on xAI's socket, so it takes the
# OpenAI side of every branch below (0.25.0).
XAI = "xai"
PROVIDERS = (OPENAI, GEMINI, XAI)

# What each engine wants the microphone audio to be. The device produces
# 16 kHz; OpenAI needs it raised, Gemini takes it as it is. Both answer with
# 24 kHz, which is what the pipeline already plays.
_INPUT_RATE = {OPENAI: 24000, GEMINI: 16000, XAI: 24000}

# pipecat's OpenAI Realtime service has no reconnect logic; a dead socket
# floods ErrorFrames forever. The Gemini service has _reconnect,
# _handle_connection_error and session resumption, so it repairs itself and
# ConnectionRecovery must keep its hands off.
_SELF_HEALS = {OPENAI: False, GEMINI: True, XAI: False}

# Raw client events are OpenAI Realtime's own protocol. Gemini Live has no
# equivalent, so anything sent that way reaches one engine and vanishes on the
# other -- which is how the speaker's name silently stopped reaching the model.
_CLIENT_EVENTS = {OPENAI: True, GEMINI: False, XAI: True}


@dataclass
class ProviderOptions:
    """Every knob the add-on can set, for whichever engine reads it.

    Fields an engine does not have are simply ignored by that engine's module.
    Gemini has no playback speed and no noise reduction; OpenAI has no thinking
    budget. Keeping one shape means the caller never branches on the engine.
    """

    api_key: str
    model: str
    voice: str
    instructions: str
    max_output_tokens: Optional[int] = None
    # OpenAI only
    speed: float = 1.0
    noise_reduction: str = ""
    turn_detection_type: str = "semantic_vad"
    vad_eagerness: str = "medium"
    vad_threshold: float = 0.5
    vad_prefix_padding_ms: int = 300
    vad_silence_duration_ms: int = 500
    semantic_vad_create_response: bool = True
    interrupt_response: bool = True
    transcription_model: str = "gpt-4o-transcribe"
    transcription_language: str = ""
    # Gemini only
    language: str = "sv-SE"
    # Gemini's own turn detection (Google's "automatic activity detection").
    # Left unset, Google runs it at START_SENSITIVITY_HIGH, which treats room
    # noise, the speaker's own echo and half-words as a user turn -- observed
    # live 2026-09-09 as answers to "Och?", "Ja." and one Portuguese sentence
    # nobody said. That is why START was LOW from 2026-09-09 -- and on
    # 2026-10-02 LOW turned out to be a deaf assistant: one turn opened for
    # seven things said, the first answered 22 s late from cached audio, the
    # rest never. The device only streams after a wake or in a follow-up
    # window, with the mic shut while the assistant speaks, so START is HIGH:
    # hearing the person who just woke it matters more than ignoring the room.
    gemini_vad_start_sensitivity: str = "high"
    gemini_vad_end_sensitivity: str = "low"
    gemini_vad_prefix_padding_ms: int = 300
    gemini_vad_silence_duration_ms: int = 800
    # Local turn detection (the default since 0.22.5): how long a silence ends
    # his turn. 800 ms cut him off mid-question at every natural pause
    # (2026-10-02); 1200 ms lets him breathe. Separate from the knob above,
    # which is Google's own VAD and only used when local detection is off.
    gemini_turn_silence_ms: int = 1200
    # xAI: "local" (default since 0.25.3) = the same local Silero turn end as
    # Gemini, with xai_turn_silence_ms; "server" = xAI's server_vad, which
    # ended turns 5-13 s late in a room with music (2026-10-02).
    xai_turn_detection: str = "local"
    xai_turn_silence_ms: int = 1200
    # "Proactive audio": Google's own answer to a speaker that hears the room.
    # The model listens to everything but decides for itself whether the audio
    # was addressed to it, and stays silent when it was not (silence is not
    # billed as output audio). This is the behaviour the Gemini app has.
    # It needs a NATIVE-AUDIO model -- gemini-2.5-flash-native-audio-* -- on
    # API version v1beta; Gemini 3.1 Flash Live does not support it and the
    # session is refused outright, so it is off unless asked for.
    gemini_proactive_audio: bool = False
    # "Affective dialog": the model matches the expression and tone it hears
    # instead of reading every answer in the same flat voice. Same gate as
    # proactive audio -- native-audio model, v1beta.
    gemini_affective_dialog: bool = False


def _known(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
    return provider


def input_sample_rate(provider: str) -> int:
    """The microphone sample rate this engine wants, in Hz."""
    return _INPUT_RATE[_known(provider)]


def self_heals(provider: str) -> bool:
    """Whether this engine reconnects its own dead socket."""
    return _SELF_HEALS[_known(provider)]


async def drop_pending_input_audio(provider: str, service, keep_speech: bool = False) -> str:
    """Tell the engine the microphone stopped and to drop what it is holding.

    Both engines have this; they spell it differently, which is why it lives
    here rather than in three branches at the call sites. OpenAI Realtime has
    the raw client event `input_audio_buffer.clear`. Gemini Live has
    `audioStreamEnd` on its realtime-input channel -- documented as the way to
    "flush any cached audio" when an audio stream pauses -- and this add-on
    never sent it, so a sentence cut off by a closing follow-up window stayed
    cached on Google's side and could be completed into a stale answer later.

    Args:
        provider: "openai" or "gemini".
        service: That engine's live service object.
        keep_speech: The follow-up window closed (not a stop word). Gemini's
            and xAI's local VAD then answer speech already under way instead
            of dropping it; OpenAI's server VAD has no such view, unchanged.

    Returns:
        The name of what was sent, for the caller's log line.

    Raises:
        Whatever the engine's send path raises -- every caller is a device
        event that already logs and swallows, and a silent failure here would
        hide the exact thing this function exists to guarantee.
    """
    if supports_client_events(provider) and getattr(type(service), "end_audio_stream", None) is None:
        from pipecat.services.openai.realtime import events as openai_rt_events

        await service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
        return "input_audio_buffer.clear"
    # Gemini, and xAI (whose local turns answer a cut-off utterance too).
    return await service.end_audio_stream(keep_speech=keep_speech) or "audioStreamEnd"


async def bana0_hit(provider: str, service, text: str) -> None:
    """Bana 0 hit: Home Assistant already did it and said so; the model must not.

    OpenAI heard the turn, so it is told what HA said (and never asked to
    answer). Gemini's turn was held back and is simply dropped: the model
    never heard the order, so it cannot answer it or do it again.
    """
    from app import bana0

    if supports_client_events(provider):
        await bana0.lagg_till_svar(service, text)
    else:
        await service.drop_turn()


async def bana0_miss(provider: str, service) -> None:
    """Bana 0 missed: let the model answer the turn."""
    from app import bana0

    if supports_client_events(provider):
        await bana0.be_om_svar(service)
        service.arm_silence_ack()
    else:
        await service.answer_turn()  # arms the silence ack at its activityEnd


def supports_client_events(provider: str) -> bool:
    """Whether this engine accepts raw OpenAI Realtime client events."""
    return _CLIENT_EVENTS[_known(provider)]


def build_service(provider: str, options: ProviderOptions, tools: List[Dict[str, Any]]):
    """Build a configured, unconnected session for one device.

    Args:
        provider: "openai" or "gemini".
        options: Every knob; each engine reads the ones it has.
        tools: Tool definitions in OpenAI Realtime shape. The Gemini module
            converts them; nothing outside providers/ needs two shapes.

    Returns:
        A pipecat LLMService.
    """
    # The one place every engine's tools pass: slow tools learn to say what
    # they are about to do (early_ack.with_ack_hint, 0.25.6).
    from app.early_ack import with_ack_hint
    tools = with_ack_hint(tools)
    from app.providers.sovlage import SovlageMixin, sovlage_pa
    if _known(provider) == OPENAI:
        from app.providers import openai_realtime
        service = openai_realtime.build(options, tools)
    elif provider == XAI:
        from app.providers import xai_realtime
        service = xai_realtime.build(options, tools)
    else:
        from app.providers import gemini_live
        service = gemini_live.build(options, tools)
    # Asleep from the start: pipecat's start() connects nothing until the
    # device's wake (providers/sovlage.py, raawr INKAST 2026-10-04).
    if isinstance(service, SovlageMixin):
        service.sover = sovlage_pa()
    return service
