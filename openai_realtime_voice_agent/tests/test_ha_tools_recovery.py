"""HA tools come back on their own after a failed fetch (raawr D-70).

Live 2026-09-30: HA was restarting when the office speaker connected; the MCP
POST answered 404 in 0.2 s and the session was built with 14 tools instead of
59. The connection then lived for hours and the HA tools never came back until
the speaker reconnected.

0.21.1 pushed the recovered tools into the live OpenAI session
(session.update); live 2026-10-01 that left the session deaf ("no server VAD
activity 12s after wake") until the device reconnected. So recovery now closes
the device's socket once it is idle, and the firmware's reconnect builds a
fresh session through the normal path.
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


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    monkeypatch.setenv("MCP_RECYCLE_POLL_SECONDS", "0.05")
    monkeypatch.setenv("MCP_RECYCLE_QUIET_SECONDS", "0")


@pytest.mark.asyncio
async def test_ha_back_while_idle_recycles_the_connection_once(fast):
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=2)
    connection, service = await _connect(app, phase="idle")
    tools_before = _tool_names(service)

    assert await _wait_for(lambda: connection.websocket.closes), "connection never recycled"
    await asyncio.sleep(0.2)
    assert connection.websocket.closes == [1000]
    # The live session is left alone: the fresh one gets the tools.
    assert _tool_names(service) == tools_before
    assert "HassTurnOn" not in _tool_names(service)


@pytest.mark.asyncio
async def test_no_recycle_during_a_turn_until_the_device_is_idle(fast):
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="replying")

    await asyncio.sleep(0.4)
    assert connection.websocket.closes == []

    connection.phase_emitter.phase = "idle"
    assert await _wait_for(lambda: connection.websocket.closes)
    assert connection.websocket.closes == [1000]


@pytest.mark.asyncio
async def test_no_recycle_right_after_a_wake(fast, monkeypatch):
    """Phase stays idle between wake and the first speech; a recent wake counts."""
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_RECYCLE_QUIET_SECONDS", "0.4")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="idle")
    connection.touch()  # a wake just now

    await asyncio.sleep(0.2)
    assert connection.websocket.closes == []
    assert await _wait_for(lambda: connection.websocket.closes)


@pytest.mark.asyncio
async def test_recovery_stops_when_the_device_disconnects(fast):
    from app.providers import OPENAI
    from app.websocket_handler import WebSocketHandler

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=10**6)
    connection, service = await _connect(app, phase="idle")
    task = connection.ha_tools_task
    assert task is not None and not task.done()

    handler = WebSocketHandler(host="127.0.0.1", port=0)
    await handler._teardown(connection)
    await asyncio.sleep(0.1)
    assert task.done()
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
    assert await _wait_for(lambda: connection.websocket.closes)


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
    assert await _wait_for(lambda: connection.websocket.closes)


@pytest.mark.asyncio
async def test_phase_emitter_records_when_the_turn_went_idle():
    from app.phase_emitter import PhaseEmitter

    emitter = PhaseEmitter(None)
    await emitter._emit("replying")
    before = time.monotonic()
    await emitter._emit("idle")
    assert emitter.idle_since >= before


@pytest.mark.asyncio
async def test_at_most_one_recycle_per_interval_when_ha_flaps(fast, monkeypatch):
    """(3) HA flapping must not bounce the device again and again."""
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_RECYCLE_MIN_INTERVAL_SECONDS", "0.6")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    first, _ = await _connect(app, phase="idle")
    assert await _wait_for(lambda: first.websocket.closes)

    # The device reconnects while HA is down again, then HA comes back.
    app.mcp_client.failures = 1
    second, _ = await _connect(app, phase="idle")
    await asyncio.sleep(0.3)
    assert second.websocket.closes == [], "recycled twice inside the interval"
    # Still retrying: once the interval is over, the tools do get loaded.
    assert await _wait_for(lambda: second.websocket.closes)


@pytest.mark.asyncio
async def test_recycle_close_is_not_cancelled_by_its_own_teardown(fast):
    """(4) The close leads to _teardown, which cancels connection.ha_tools_task.

    The task drops itself off the connection first, so the close completes.
    """
    from app.providers import OPENAI
    from app.websocket_handler import WebSocketHandler

    handler = WebSocketHandler(host="127.0.0.1", port=0)

    class _TearingSocket(_Socket):
        async def close(self, code=1000, reason=None):
            await handler._teardown(connection)
            await asyncio.sleep(0)  # the closing handshake
            self.closes.append(code)

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="idle")
    connection.websocket = _TearingSocket()
    task = connection.ha_tools_task

    assert await _wait_for(task.done)
    assert not task.cancelled()
    assert connection.websocket.closes == [1000]
