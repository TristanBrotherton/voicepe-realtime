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
PROVIDERS = (OPENAI, GEMINI)

# What each engine wants the microphone audio to be. The device produces
# 16 kHz; OpenAI needs it raised, Gemini takes it as it is. Both answer with
# 24 kHz, which is what the pipeline already plays.
_INPUT_RATE = {OPENAI: 24000, GEMINI: 16000}

# pipecat's OpenAI Realtime service has no reconnect logic; a dead socket
# floods ErrorFrames forever. The Gemini service has _reconnect,
# _handle_connection_error and session resumption, so it repairs itself and
# ConnectionRecovery must keep its hands off.
_SELF_HEALS = {OPENAI: False, GEMINI: True}

# Raw client events are OpenAI Realtime's own protocol. Gemini Live has no
# equivalent, so anything sent that way reaches one engine and vanishes on the
# other -- which is how the speaker's name silently stopped reaching the model.
_CLIENT_EVENTS = {OPENAI: True, GEMINI: False}


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
    # nobody said. LOW is the equivalent of the OpenAI side's
    # vad_eagerness="low": harder to start a turn, slower to call it finished.
    gemini_vad_start_sensitivity: str = "low"
    gemini_vad_end_sensitivity: str = "low"
    gemini_vad_prefix_padding_ms: int = 300
    gemini_vad_silence_duration_ms: int = 800
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


async def drop_pending_input_audio(provider: str, service) -> str:
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

    Returns:
        The name of what was sent, for the caller's log line.

    Raises:
        Whatever the engine's send path raises -- every caller is a device
        event that already logs and swallows, and a silent failure here would
        hide the exact thing this function exists to guarantee.
    """
    if _known(provider) == OPENAI:
        from pipecat.services.openai.realtime import events as openai_rt_events

        await service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
        return "input_audio_buffer.clear"
    await service.end_audio_stream()
    return "audioStreamEnd"


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
    if _known(provider) == OPENAI:
        from app.providers import openai_realtime
        return openai_realtime.build(options, tools)
    from app.providers import gemini_live
    return gemini_live.build(options, tools)
