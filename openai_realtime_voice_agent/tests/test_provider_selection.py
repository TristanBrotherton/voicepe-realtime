"""The app must ask the router, and hand each engine its own key."""

import os

import pytest

from app.provider_router import ProviderRouter


@pytest.mark.asyncio
async def test_a_backup_of_none_means_no_failover(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "none")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "30")
    r = build_router()
    assert r.primary == "gemini"
    assert r.backup is None
    assert await r.report_failure("gemini", "insufficient_quota") == "gemini"


def test_the_cooldown_is_read_in_minutes(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "gemini")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "45")
    assert build_router().cooldown_s == 45 * 60


def test_an_empty_cooldown_env_var_falls_back_to_thirty_minutes(monkeypatch):
    """An empty string is falsy, so `os.environ.get(...) or 30` substitutes
    the int default BEFORE float() is ever called -- this exercises that
    short-circuit, not the except branch below (see the "abc" test for that)."""
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "gemini")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "")
    assert build_router().cooldown_s == 1800.0


def test_an_unparseable_cooldown_falls_back_to_thirty_minutes(monkeypatch):
    """A non-empty, non-numeric value IS truthy, so it reaches float() and
    must be caught there by the except branch -- "" alone can't prove that
    branch works, since it never gets that far."""
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "gemini")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "abc")
    assert build_router().cooldown_s == 1800.0


def test_a_backup_the_same_as_the_primary_is_no_backup(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "gemini")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "30")
    assert build_router().backup is None


def test_an_unknown_primary_falls_back_to_openai(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDER", "claude")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "none")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MINUTES", "30")
    assert build_router().primary == "openai"


def test_provider_options_gives_each_engine_its_own_key_model_and_voice(monkeypatch):
    """The knobs must actually differ per engine, not just exist per engine.

    Every value below that has a ProviderOptions dataclass default is
    deliberately set to something OTHER than that default (speed=1.25 not
    1.0, turn_detection_type="server_vad" not "semantic_vad",
    transcription_language="nl-NL" not "" / "sv-SE") -- otherwise a dropped
    kwarg in provider_options() would silently fall back to the same value
    the test expects, and the assertion would pass whether or not the code
    actually wired that field through.
    """
    from app.main import Application
    from app.providers import GEMINI, OPENAI

    app = Application()
    app.instructions = "Base instructions."
    app.gemini_api_key = "gm-key"
    app.gemini_model = "models/gemini-3.1-flash-live-preview"
    app.gemini_voice = "Charon"
    # Gemini's own turn-detection knobs, deliberately away from the dataclass
    # defaults (low/low/300/800) so a dropped kwarg cannot hide behind them.
    app.gemini_vad_start_sensitivity = "high"
    app.gemini_vad_end_sensitivity = "high"
    app.gemini_vad_prefix_padding_ms = 111
    app.gemini_vad_silence_duration_ms = 222
    app.gemini_proactive_audio = True
    app.gemini_affective_dialog = True
    app.transcription_language = "nl-NL"
    app.max_output_tokens = None
    app.openai_api_key = "sk-key"
    app.model = "gpt-realtime-2"
    app.voice = "marin"
    app.openai_speed = 1.25
    app.noise_reduction = ""
    app.turn_detection_type = "server_vad"
    app.vad_eagerness = "low"
    app.vad_threshold = 0.5
    app.vad_prefix_padding_ms = 300
    app.vad_silence_duration_ms = 800
    app.semantic_vad_create_response = True
    app.interrupt_response = False
    app.transcription_model = "gpt-4o-transcribe"

    gemini_options = app.provider_options(GEMINI)
    openai_options = app.provider_options(OPENAI)

    assert gemini_options.api_key == "gm-key"
    assert gemini_options.model == "models/gemini-3.1-flash-live-preview"
    assert gemini_options.voice == "Charon"
    # Gemini's own field, fed from transcription_language when set.
    assert gemini_options.language == "nl-NL"
    # Gemini's turn detection reaches the engine. Unwired, the session falls
    # back to Google's START_SENSITIVITY_HIGH and answers the room.
    assert gemini_options.gemini_vad_start_sensitivity == "high"
    assert gemini_options.gemini_vad_end_sensitivity == "high"
    assert gemini_options.gemini_vad_prefix_padding_ms == 111
    assert gemini_options.gemini_vad_silence_duration_ms == 222
    assert gemini_options.gemini_proactive_audio is True
    assert gemini_options.gemini_affective_dialog is True

    assert openai_options.api_key == "sk-key"
    assert openai_options.model == "gpt-realtime-2"
    assert openai_options.voice == "marin"
    # provider_options() never sets `language=` in the OpenAI branch, so this
    # must stay the dataclass default -- if it read "nl-NL" too, the two
    # branches would be sharing/mutating one ProviderOptions instead of each
    # building its own.
    assert openai_options.language == "sv-SE"
    assert openai_options.transcription_language == "nl-NL"

    # Both engines get the same instructions -- there is only one prompt.
    assert gemini_options.instructions == openai_options.instructions

    # OpenAI-only knobs, set above to non-default values so a dropped kwarg
    # can't hide behind the dataclass default matching the test's input.
    assert openai_options.speed == 1.25
    assert openai_options.turn_detection_type == "server_vad"


def _bare_app(provider: str, backup=None):
    """An Application with exactly the attributes create_service touches,
    wired by hand instead of through initialize() -- initialize() also starts
    a Home Assistant MCP client, a voice-print publisher task and an announce
    HTTP server, none of which belong in a unit test."""
    from app.main import Application
    from app.provider_router import ProviderRouter
    from app.session_manager import SessionManager
    from app.timers import TimerRegistry

    app = Application()
    app.router = ProviderRouter(provider, backup)
    app.session_manager = SessionManager()
    app.timer_registry = TimerRegistry()
    app.enrollment_conductor = None
    app.enable_disconnect_tool = False
    app.enable_web_search = False
    app.mcp_client = None
    app.mcp_tool_allowlist = []
    app.speaker_male_name = ""
    app.speaker_female_name = ""
    app.male_only_tools = set()
    app.instructions = "Base instructions."
    app.max_output_tokens = None
    app.transcription_language = ""
    app.gemini_api_key = "gm-test"
    app.gemini_model = "models/gemini-3.1-flash-live-preview"
    app.gemini_voice = "Charon"
    app.gemini_vad_start_sensitivity = "low"
    app.gemini_vad_end_sensitivity = "low"
    app.gemini_vad_prefix_padding_ms = 300
    app.gemini_vad_silence_duration_ms = 800
    app.gemini_proactive_audio = False
    app.gemini_affective_dialog = False
    app.openai_api_key = "sk-test"
    app.model = "gpt-realtime-2"
    app.voice = "marin"
    app.openai_speed = 1.0
    app.noise_reduction = ""
    app.turn_detection_type = "semantic_vad"
    app.vad_eagerness = "low"
    app.vad_threshold = 0.5
    app.vad_prefix_padding_ms = 300
    app.vad_silence_duration_ms = 800
    app.semantic_vad_create_response = True
    app.interrupt_response = False
    app.transcription_model = "gpt-4o-transcribe"
    return app


def _is_gemini_service(service) -> bool:
    """Tell a built Gemini service from a built OpenAI one without importing
    either provider module -- Gemini's carries a voice_id, OpenAI's realtime
    service does not use that attribute name."""
    return hasattr(service, "voice_id") or "Gemini" in type(service).__name__


class _PoisonRouter:
    """Explodes if asked. create_service must never consult the router --
    the engine was already decided once, by WebSocketHandler.serve_connection,
    and stored on connection.provider before create_service ever runs."""

    def current(self) -> str:
        raise AssertionError("create_service must not read the router")


@pytest.mark.asyncio
async def test_create_service_never_reads_the_router_it_uses_the_stored_decision():
    """Fix 1 / ruling 2: create_service must build whichever engine
    connection.provider already names, without asking self.router again. A
    router that raises the moment it's asked proves the "again" -- if
    create_service still called self.router.current() (as it used to, to
    decide the engine for build_service), this test fails immediately with
    the poison router's AssertionError instead of the one below."""
    from app.device_registry import DeviceConnection
    from app.providers import GEMINI

    app = _bare_app(GEMINI)
    app.router = _PoisonRouter()
    connection = DeviceConnection(device_id="kitchen", websocket=object())
    connection.provider = GEMINI  # decided elsewhere, before create_service runs

    service = await app.create_service(connection)

    assert _is_gemini_service(service)
    assert connection.provider == GEMINI


class _FlakyRouter:
    """A router whose answer moves between successive calls -- standing in
    for another connection's failure landing in the real gap (pipeline lock +
    an awaited MCP fetch) between two reads, if the code still took two."""

    def __init__(self, first: str, second: str):
        self._answers = [first, second]

    def current(self) -> str:
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


class _FakeURL:
    query = "device_id=kitchen"


class _FakeWebSocket:
    url = _FakeURL()
    client = None

    async def accept(self):
        return None

    async def send_text(self, _payload):
        return None


class _RateSpyTransport:
    """Stands in for the real transport in the integration test below, and
    simply remembers which provider it was built for -- create_transport's
    own rate math is covered separately by
    test_create_transport_declares_the_providers_input_rate."""

    def __init__(self, provider: str):
        self.provider = provider

    def event_handler(self, _name):
        def register(fn):
            return fn
        return register


def _fake_build_pipeline(_connection, activity_callback=None):
    """A pipeline stand-in that runs and finishes immediately -- this test
    is about which engine gets picked, not about pipecat's frame plumbing
    (covered separately by test_build_pipeline_resamples_to_the_connections_provider_rate)."""

    class _Runner:
        async def run(self, _task):
            return None

    class _Task:
        async def cancel(self):
            return None

    return object(), _Runner(), _Task()


@pytest.mark.asyncio
async def test_transport_and_service_agree_on_the_engine_even_if_the_router_moves():
    """Fix 1, at the level the reviewer asked for: run the REAL
    WebSocketHandler.serve_connection with a router double that answers
    "openai" then "gemini" on successive current() calls, and check that the
    transport built for this connection and the session actually built for
    it agree on which engine that is.

    Before the fix, serve_connection read the router once (for the
    transport) and create_service read it again (for the service) -- two
    reads against a router that moves would give the transport "openai" and
    the session "gemini". This must fail on the OLD code and pass on the
    fixed one, because there is now exactly one read per connection.
    """
    from app.providers import GEMINI, OPENAI, input_sample_rate
    from app.websocket_handler import WebSocketHandler

    app = _bare_app(GEMINI)
    app.router = _FlakyRouter(OPENAI, GEMINI)

    handler = WebSocketHandler()
    handler.router = app.router
    handler.create_transport = lambda _ws, _ser, provider: _RateSpyTransport(provider)
    handler.build_pipeline = _fake_build_pipeline

    seen = {}

    async def factory(connection):
        service = await app.create_service(connection)
        seen["connection"] = connection
        seen["service"] = service
        return service

    handler.openai_service_factory = factory

    await handler.serve_connection(_FakeWebSocket())

    connection = seen["connection"]
    # The transport's provider and the connection's (set inside
    # create_service, from the very same decision) must be the identical
    # single answer the router gave out -- not two different ones.
    assert connection.transport.provider == connection.provider
    assert connection.provider in (OPENAI, GEMINI)
    assert _is_gemini_service(seen["service"]) == (connection.provider == GEMINI)


def test_create_transport_declares_the_providers_input_rate():
    """Step 6: the transport's declared mic rate must follow the engine, and
    the output rate must stay 24000 for both -- getting THAT wrong makes the
    speaker play noise."""
    from app.raw_audio_serializer import RawAudioSerializer
    from app.websocket_handler import WebSocketHandler

    handler = WebSocketHandler()
    serializer = RawAudioSerializer("kitchen")

    gemini_transport = handler.create_transport(object(), serializer, "gemini")
    assert gemini_transport._params.audio_in_sample_rate == 16000
    assert gemini_transport._params.audio_out_sample_rate == 24000

    openai_transport = handler.create_transport(object(), serializer, "openai")
    assert openai_transport._params.audio_in_sample_rate == 24000
    assert openai_transport._params.audio_out_sample_rate == 24000


@pytest.mark.asyncio
async def test_build_pipeline_resamples_to_the_connections_provider_rate():
    """Step 6: InputResampler's out_rate must follow connection.provider, not
    the old fixed PIPELINE_SAMPLE_RATE constant -- for Gemini that must make
    it a pass-through (16000 in, 16000 out), matching the device's own rate."""
    import app.websocket_handler as wh
    from pipecat.processors.frame_processor import FrameProcessor
    from app.device_registry import DeviceConnection
    from app.raw_audio_serializer import RawAudioSerializer

    class FakeService(FrameProcessor):
        pass

    captured = {}
    real_resampler = wh.InputResampler

    class SpyResampler(real_resampler):
        def __init__(self, out_rate=24000, **kwargs):
            captured["out_rate"] = out_rate
            super().__init__(out_rate=out_rate, **kwargs)

    monkeypatch_target = wh.InputResampler
    wh.InputResampler = SpyResampler
    try:
        handler = wh.WebSocketHandler()
        serializer = RawAudioSerializer("kitchen")
        connection = DeviceConnection(
            device_id="kitchen", websocket=object(), serializer=serializer
        )
        connection.provider = "gemini"
        connection.transport = handler.create_transport(object(), serializer, "gemini")
        connection.openai_service = FakeService()

        handler.build_pipeline(connection)
        assert captured["out_rate"] == 16000
    finally:
        wh.InputResampler = monkeypatch_target


def test_the_websocket_handler_gets_the_same_router_instance():
    """Ruling 1: _wire_websocket_handler() (called from run(), split out so
    it's directly testable without starting uvicorn) must point the handler
    at the SAME ProviderRouter object the app holds -- not an equal-looking
    one, not none at all. A source-text search for the assignment can stay
    green even if the line is commented out or dead; asserting object
    identity on the actual, executed result cannot."""
    from app.main import Application
    from app.provider_router import ProviderRouter
    from app.websocket_handler import WebSocketHandler

    app = Application()
    app.router = ProviderRouter("openai", None)
    app.websocket_handler = WebSocketHandler()

    app._wire_websocket_handler()

    assert app.websocket_handler.router is app.router
    assert app.websocket_handler.openai_service_factory == app.create_service
