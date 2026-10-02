"""Gemini takes the same tools, in its own shape."""

import pytest

from app.providers import ProviderOptions, build_service
from app.providers.gemini_live import to_gemini_tools

OPENAI_SHAPE = [
    {
        "type": "function",
        "name": "search_home",
        "description": "Search the house.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "list_timers",
        "description": "List timers.",
        # A tool with no arguments at all. Gemini refuses a declaration with no
        # parameters block, so one must be invented.
    },
]


def _options(**over):
    base = dict(
        api_key="AIza-test",
        model="models/gemini-3.1-flash-live-preview",
        voice="Charon",
        instructions="Du är Björn.",
        max_output_tokens=1024,
        language="sv-SE",
    )
    base.update(over)
    return ProviderOptions(**base)


def test_tools_keep_their_name_description_and_parameters():
    decls = to_gemini_tools(OPENAI_SHAPE)
    assert [d["name"] for d in decls] == ["search_home", "list_timers"]
    assert decls[0]["parameters"]["properties"]["query"]["type"] == "string"
    assert decls[0]["description"] == "Search the house."


def test_a_tool_without_parameters_gets_an_empty_object():
    decls = to_gemini_tools(OPENAI_SHAPE)
    assert decls[1]["parameters"] == {"type": "object", "properties": {}}


def test_the_openai_only_type_key_is_dropped():
    # "type": "function" is OpenAI Realtime's envelope, not part of the schema.
    for d in to_gemini_tools(OPENAI_SHAPE):
        assert "type" not in d


def test_the_service_is_built_with_our_model_voice_and_instructions():
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    assert service.model_name == "models/gemini-3.1-flash-live-preview"
    assert service._voice_id == "Charon"
    # pipecat stashes the raw string until the context is initialized on
    # connect; it never lands under a plain `_system_instruction` attribute.
    assert "Björn" in service._system_instruction_from_init


def test_the_dead_default_model_is_never_used():
    # pipecat defaults to models/gemini-2.0-flash-live-001, which no longer
    # exists and refuses the socket with 1008. Our default must be a live one.
    from app.providers.gemini_live import DEFAULT_MODEL
    assert DEFAULT_MODEL != "models/gemini-2.0-flash-live-001"
    assert DEFAULT_MODEL.startswith("models/gemini-")


def test_the_language_is_resolved_to_a_gemini_language_enum():
    # InputParams.language is typed Optional[Language] (a pipecat StrEnum), not
    # a bare string. Passing "sv-SE" straight through would fail pydantic
    # validation the first time someone actually spoke in the house.
    from pipecat.transcriptions.language import Language

    service = build_service("gemini", _options(language="sv-SE"), OPENAI_SHAPE)
    assert service._language == Language.SV_SE


def test_an_unresolvable_language_falls_back_instead_of_crashing():
    from pipecat.transcriptions.language import Language

    service = build_service(
        "gemini", _options(language="not-a-real-language"), OPENAI_SHAPE
    )
    assert isinstance(service._language, Language)


# --- the shape google-genai actually accepts --------------------------------

def test_the_service_gets_tools_wrapped_the_way_the_api_wants_them():
    """One wrapper deeper than the declarations themselves.

    Handing pipecat the bare declarations made google-genai reject every field
    of every tool as "extra inputs are not permitted", and the session never
    opened. Found in the house, not by the suite — the old tests only checked
    what `to_gemini_tools` returned, never what was done with it.
    """
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    tools = service._tools_from_init
    assert isinstance(tools, list) and len(tools) == 1
    declarations = tools[0]["function_declarations"]
    assert [d["name"] for d in declarations] == ["search_home", "list_timers"]


def test_gemini_never_sees_additional_properties():
    """Gemini rejects the keyword outright, and Home Assistant generates
    schemas that carry it — one such tool would take the whole session down."""
    noisy = [{
        "type": "function",
        "name": "noisy",
        "description": "Has the keyword at three depths.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "nested": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"deep": {"type": "string"}},
                },
                "listed": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
            },
        },
    }]
    params = to_gemini_tools(noisy)[0]["parameters"]
    assert "additionalProperties" not in params
    assert "additionalProperties" not in params["properties"]["nested"]
    assert "additionalProperties" not in params["properties"]["listed"]["items"]
    assert params["properties"]["nested"]["properties"]["deep"]["type"] == "string"


def test_the_session_always_carries_turn_detection_settings():
    """Live 2026-09-09: the first Gemini session sent no realtime_input_config
    at all, so Google ran its automatic activity detection at its own default
    (START_SENSITIVITY_HIGH). The device answered room noise, its own speaker
    echo and half-words nobody said -- "Och?", "Ja.", "Ne?", and one whole
    sentence in Portuguese. pipecat only attaches the config when at least one
    field is set, so every field is sent."""
    from google.genai.types import EndSensitivity

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    vad = service._vad_params
    assert vad is not None
    assert vad.end_sensitivity == EndSensitivity.END_SENSITIVITY_LOW
    assert vad.prefix_padding_ms == 300
    assert vad.silence_duration_ms == 800


def test_the_sensitivities_follow_the_add_on_settings():
    from google.genai.types import EndSensitivity, StartSensitivity

    service = build_service(
        "gemini",
        _options(
            gemini_vad_start_sensitivity="high",
            gemini_vad_end_sensitivity="high",
            gemini_vad_prefix_padding_ms=120,
            gemini_vad_silence_duration_ms=400,
        ),
        OPENAI_SHAPE,
    )
    vad = service._vad_params
    assert vad.start_sensitivity == StartSensitivity.START_SENSITIVITY_HIGH
    assert vad.end_sensitivity == EndSensitivity.END_SENSITIVITY_HIGH
    assert vad.prefix_padding_ms == 120
    assert vad.silence_duration_ms == 400


def test_a_turn_starts_easily_by_default():
    """Live 2026-10-02 12:52-12:54, office, START_SENSITIVITY_LOW: Gemini
    opened ONE user turn for seven things said to it. The mic audio reached
    Google intact (every frame -- checked against a local fake server through
    1008 reconnects and audioStreamEnd), yet "Hallo!" and "Vad är klockan?",
    said loudly into the device, never produced an input transcription, and
    "Vad händer, frågar jag" was answered 22 s after it was spoken, from
    cached audio, once later room sounds finally tripped the start detector.
    The Voice PE only streams after a wake or inside a follow-up window, and
    the mic is closed while the assistant speaks (barge_in: false), so the
    start detector does not have to defend against the room all day -- it has
    to hear the person who just woke it."""
    from google.genai.types import StartSensitivity

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    assert service._vad_params.start_sensitivity == StartSensitivity.START_SENSITIVITY_HIGH


def test_an_unreadable_start_sensitivity_falls_back_to_the_default():
    """A typo must not quietly pick LOW: on this device that is a deaf
    assistant (see test_a_turn_starts_easily_by_default)."""
    from google.genai.types import StartSensitivity

    service = build_service(
        "gemini", _options(gemini_vad_start_sensitivity="lowish"), OPENAI_SHAPE
    )
    assert service._vad_params.start_sensitivity == StartSensitivity.START_SENSITIVITY_HIGH


def test_a_negative_padding_is_clamped_rather_than_sent():
    service = build_service(
        "gemini", _options(gemini_vad_prefix_padding_ms=-50), OPENAI_SHAPE
    )
    assert service._vad_params.prefix_padding_ms == 0


def test_proactive_audio_is_off_unless_asked_for():
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    assert not service._settings.get("proactivity")


def test_proactive_audio_needs_a_native_audio_model():
    """Google's guide is explicit: proactive audio and affective dialog are
    not supported on Gemini 3.1 Flash Live. Sending the config anyway gets the
    whole session refused, so an operator who ticks the box without moving the
    model must lose the feature, not the assistant."""
    service = build_service(
        "gemini",
        _options(gemini_proactive_audio=True),  # still the 3.1 preview model
        OPENAI_SHAPE,
    )
    assert not service._settings.get("proactivity")


def test_affective_dialog_shares_the_native_audio_gate():
    service = build_service(
        "gemini", _options(gemini_affective_dialog=True), OPENAI_SHAPE
    )
    assert not service._settings.get("enable_affective_dialog")

    service = build_service(
        "gemini",
        _options(
            model="models/gemini-2.5-flash-native-audio-latest",
            gemini_affective_dialog=True,
        ),
        OPENAI_SHAPE,
    )
    assert service._settings["enable_affective_dialog"] is True


@pytest.mark.asyncio
async def test_ending_the_audio_stream_tells_google_the_mic_stopped():
    """Gemini Live's answer to input_audio_buffer.clear. Never sent before, so
    a sentence cut off by a closing follow-up window stayed cached on Google's
    side and could be completed into a stale answer on the next wake."""
    sent = []

    class FakeSession:
        async def send_realtime_input(self, **kw):
            sent.append(kw)

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    service._session = FakeSession()
    service._disconnecting = False

    await service.end_audio_stream()

    assert sent == [{"audio_stream_end": True}]


@pytest.mark.asyncio
async def test_ending_the_audio_stream_between_sessions_is_silent():
    """The callers are device events. One arriving while no session is up must
    not raise -- the device does not know or care what the engine is doing."""
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    service._session = None

    await service.end_audio_stream()  # must not raise


@pytest.mark.asyncio
async def test_each_engine_drops_pending_input_in_its_own_dialect():
    from app.providers import drop_pending_input_audio

    gemini_calls = []

    class FakeGemini:
        async def end_audio_stream(self):
            gemini_calls.append(True)

    assert await drop_pending_input_audio("gemini", FakeGemini()) == "audioStreamEnd"
    assert gemini_calls == [True]

    openai_events = []

    class FakeOpenAI:
        async def send_client_event(self, event):
            openai_events.append(type(event).__name__)

    assert (
        await drop_pending_input_audio("openai", FakeOpenAI())
        == "input_audio_buffer.clear"
    )
    assert openai_events == ["InputAudioBufferClearEvent"]


def test_proactive_audio_reaches_a_native_audio_session():
    service = build_service(
        "gemini",
        _options(
            model="models/gemini-2.5-flash-native-audio-latest",
            gemini_proactive_audio=True,
        ),
        OPENAI_SHAPE,
    )
    assert service._settings["proactivity"].proactive_audio is True


class _Recorder:
    """Collects the fatal errors the service would push."""

    def __init__(self):
        self.fatal = []


def _service_with_failures(failures: int, connection_age_s: float):
    import time as _time

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    recorder = _Recorder()

    async def fake_push_error(error_msg, exception=None):
        recorder.fatal.append(error_msg)

    service.push_error = fake_push_error
    service._consecutive_failures = failures
    service._connection_start_time = _time.time() - connection_age_s
    return service, recorder


@pytest.mark.asyncio
async def test_an_idle_hangup_after_a_long_connection_is_forgiven():
    """The device is push-to-talk, so a quiet house sends Google nothing and
    Google hangs up -- measured at a very regular ~152s. pipecat only forgives
    a failure from inside its receive loop, which a silent connection never
    reaches, so three quiet hang-ups in a row were pushed as fatal. Live
    2026-09-09 17:05:41: the engine died and stayed dead for 45 minutes."""
    service, recorder = _service_with_failures(failures=2, connection_age_s=152)

    should_reconnect = await service._handle_connection_error(RuntimeError("1008"))

    assert should_reconnect is True
    assert recorder.fatal == []
    # Forgiven back to zero, then this one counted: one strike, not three.
    assert service._consecutive_failures == 1


@pytest.mark.asyncio
async def test_a_genuinely_broken_engine_still_reaches_fatal():
    """The forgiveness must not be a blanket amnesty. Three failures inside
    the stable-connection threshold mean the engine really cannot hold a
    socket, which is the case the counter exists for."""
    service, recorder = _service_with_failures(failures=2, connection_age_s=1)

    should_reconnect = await service._handle_connection_error(RuntimeError("1008"))

    assert should_reconnect is False
    assert recorder.fatal and "fatal" in recorder.fatal[0]


@pytest.mark.asyncio
async def test_the_first_failure_is_never_fatal_whatever_the_age():
    service, recorder = _service_with_failures(failures=0, connection_age_s=0.5)

    assert await service._handle_connection_error(RuntimeError("1008")) is True
    assert recorder.fatal == []


def test_the_native_audio_features_ask_for_the_api_version_that_accepts_them():
    """Probed live 2026-09-09, all six combinations. Google's guide says these
    need v1beta; on v1beta the session is refused with

        1007 Unknown name "proactivity" at 'setup': Cannot find field.

    and google-genai already defaults to v1beta, so believing the guide leaves
    a "fix" that changes nothing. v1alpha accepts both features."""
    from app.providers.gemini_live import NATIVE_AUDIO_API_VERSION

    assert NATIVE_AUDIO_API_VERSION == "v1alpha"

    service = build_service(
        "gemini",
        _options(
            model="models/gemini-2.5-flash-native-audio-latest",
            gemini_proactive_audio=True,
        ),
        OPENAI_SHAPE,
    )
    assert service._http_options.api_version == "v1alpha"


def test_a_native_audio_model_gets_no_language_code():
    """Probed live 2026-09-09: models/gemini-2.5-flash-native-audio-latest
    refuses 'sv' AND 'sv-SE' with 1007 Unsupported language code, but opens
    happily with none at all. The system instruction is written in Swedish and
    says so explicitly, so the prompt steers the language instead."""
    service = build_service(
        "gemini",
        _options(model="models/gemini-2.5-flash-native-audio-latest"),
        OPENAI_SHAPE,
    )
    assert service._settings.get("language") is None


def test_the_half_cascade_model_keeps_its_language_code():
    """The rule must be narrow: 3.1 Flash Live does take the code, and losing
    it there would hand a Swedish house a model guessing at its language."""
    from pipecat.transcriptions.language import Language

    service = build_service("gemini", _options(), OPENAI_SHAPE)
    assert service._settings.get("language") == Language.SV_SE
