"""HA tools come back on the next wake after a failed fetch (raawr D-70, fynd 8).

Live 2026-09-30: HA was restarting when the office speaker connected; the MCP
POST answered 404 in 0.2 s and the session was built with 14 tools instead of
59. The connection then lived for hours and the HA tools never came back until
the speaker reconnected.

0.21.1 pushed the recovered tools into the live OpenAI session
(session.update); live 2026-10-01 that left the session deaf. 0.21.2 closed
the speaker instead, and the firmware's reconnect woke the cloud again
(granskning 2026-10-04, fynd 8). The speaker now stays up. The next wake
fetches the list again and the engine connects with it.
"""
import asyncio
import time

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from tests.test_provider_selection import _bare_app


def _schema():
    return ToolsSchema(standard_tools=[
        FunctionSchema(name="HassTurnOn", description="on", properties={}, required=[]),
        FunctionSchema(name="HassMediaSearchAndPlay", description="dup", properties={}, required=[]),
    ])


class _FlakyMcpClient:
    """Fails like HA mid-restart, then answers."""

    def __init__(self, failures=1):
        self.failures = failures

    async def get_tools_schema(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Client error '404 Not Found'")
        return _schema()

    async def register_tools_schema(self, schema, service):
        for f in schema.standard_tools:
            service.register_function(f.name, lambda params: None)


class _Socket:
    def __init__(self):
        self.closes = []

    async def close(self, code=1000, reason=None):
        self.closes.append(code)


class _Phase:
    def __init__(self, phase=None):
        self.phase = phase

    async def close(self):
        pass


def _tool_names(service):
    return [t["name"] for t in service._session_properties.tools]


async def _connect(app, phase=None):
    from app.device_registry import DeviceConnection
    from app.providers import OPENAI

    connection = DeviceConnection(device_id="office", websocket=_Socket())
    connection.provider = OPENAI
    connection.phase_emitter = _Phase(phase)
    service = await app.create_service(connection)
    connection.openai_service = service  # what serve_connection does next
    return connection, service


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    monkeypatch.setenv("MCP_RECYCLE_POLL_SECONDS", "0.05")
    monkeypatch.setenv("MCP_RECYCLE_QUIET_SECONDS", "0")


@pytest.mark.asyncio
async def test_ha_back_while_idle_does_not_close_the_speaker(fast):
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="idle")
    tools_before = _tool_names(service)

    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []
    assert _tool_names(service) == tools_before
    assert "HassTurnOn" not in tools_before
    await app.hamta_verktyg_vid_vakning(connection)
    assert "HassTurnOn" in _tool_names(service)
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_no_recycle_during_a_turn_until_the_device_is_idle(fast):
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="replying")

    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []

    connection.phase_emitter.phase = "idle"
    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_no_recycle_right_after_a_wake(fast, monkeypatch):
    """Phase stays idle between wake and the first speech; a recent wake counts."""
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_RECYCLE_QUIET_SECONDS", "0.4")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="idle")
    connection.touch()  # a wake just now

    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_recovery_stops_when_the_device_disconnects(fast):
    from app.providers import OPENAI
    from app.websocket_handler import WebSocketHandler

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=10**6)
    connection, service = await _connect(app, phase="idle")
    assert connection.ha_tools_task is None

    handler = WebSocketHandler(host="127.0.0.1", port=0)
    await handler._teardown(connection)
    await asyncio.sleep(0.1)
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_no_recycle_when_the_first_fetch_worked(fast):
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=0)
    connection, service = await _connect(app, phase="idle")

    assert connection.ha_tools_task is None
    assert _tool_names(service).count("HassTurnOn") == 1
    assert "HassMediaSearchAndPlay" not in _tool_names(service)
    await asyncio.sleep(0.2)
    assert connection.websocket.closes == []


# --- D-72 -----------------------------------------------------------------


class _Speaker:
    """An enrollment conductor whose _say plays until released."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def _say(self, text, device_id=None):
        self.started.set()
        await self.release.wait()


class _Handler:
    """The two lookups _guarded_say makes, against one connection."""

    def __init__(self, connection, follow_up_ms=0):
        self.connection = connection
        self.follow_up_ms = follow_up_ms

    def resolve_device(self, device_id=None):
        return self.connection

    def serializer_for(self, device_id=None):
        return None


@pytest.mark.asyncio
async def test_no_recycle_while_an_announcement_plays(fast):
    """(1) An announcement has no phase and no wake; it must still count as busy."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=10**6)
    connection, service = await _connect(app, phase="idle")
    app.websocket_handler = _Handler(connection)
    app.enrollment_conductor = _Speaker()

    say = asyncio.create_task(app._guarded_say("Tvätten är klar"))
    await app.enrollment_conductor.started.wait()
    app.mcp_client.failures = 0  # HA comes back mid-announcement

    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []

    app.enrollment_conductor.release.set()
    await say
    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_no_recycle_inside_the_follow_up_window_after_a_long_turn(fast, monkeypatch):
    """(2) The wake was long ago, the turn just ended: the follow-up window is open."""
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_RECYCLE_QUIET_SECONDS", "0.2")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=10**6)
    connection, service = await _connect(app, phase="replying")
    app.websocket_handler = _Handler(connection, follow_up_ms=400)
    connection.last_active = time.monotonic() - 40  # the wake, 40 s ago
    app.mcp_client.failures = 0
    await asyncio.sleep(0.2)

    connection.phase_emitter.phase = "idle"  # the turn ends now
    connection.phase_emitter.idle_since = time.monotonic()
    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_phase_emitter_records_when_the_turn_went_idle():
    from app.phase_emitter import PhaseEmitter

    emitter = PhaseEmitter(None)
    await emitter._emit("replying")
    before = time.monotonic()
    await emitter._emit("idle")
    assert emitter.idle_since >= before


@pytest.mark.asyncio
async def test_ha_flapping_does_not_close_either_speaker(fast, monkeypatch):
    """HA coming and going must not bounce the speaker. The wake loads the tools."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    first, _ = await _connect(app, phase="idle")
    await asyncio.sleep(0.3)
    assert first.websocket.closes == []

    app.mcp_client.failures = 1
    second, second_service = await _connect(app, phase="idle")
    await asyncio.sleep(0.3)
    assert second.websocket.closes == []
    assert "HassTurnOn" not in _tool_names(second_service)
    await app.hamta_verktyg_vid_vakning(second)
    assert "HassTurnOn" in _tool_names(second_service)
    assert second.websocket.closes == []


@pytest.mark.asyncio
async def test_teardown_of_a_short_session_does_not_close_the_speaker(fast):
    """There is no background close left for teardown to cancel."""
    from app.providers import OPENAI
    from app.websocket_handler import WebSocketHandler

    handler = WebSocketHandler(host="127.0.0.1", port=0)
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="idle")

    await handler._teardown(connection)
    await asyncio.sleep(0.2)
    assert connection.ha_tools_task is None
    assert connection.websocket.closes == []
