"""HA tools come back on their own after a failed fetch (raawr D-70).

Live 2026-09-30: HA was restarting when the office speaker connected; the MCP
POST answered 404 in 0.2 s and the session was built with 14 tools instead of
59. The connection then lived for hours and the HA tools never came back until
the speaker reconnected.
"""
import asyncio

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
        self.registrations = 0

    async def get_tools_schema(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Client error '404 Not Found'")
        return _schema()

    async def register_tools_schema(self, schema, service):
        self.registrations += 1
        for f in schema.standard_tools:
            service.register_function(f.name, lambda params: None)


def _tool_names(service):
    return [t["name"] for t in service._session_properties.tools]


async def _connect(app, provider="openai"):
    from app.device_registry import DeviceConnection

    connection = DeviceConnection(device_id="office", websocket=object())
    connection.provider = provider
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


@pytest.mark.asyncio
async def test_ha_tools_are_recovered_into_the_live_session_without_a_new_connection(monkeypatch):
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=2)

    connection, service = await _connect(app)
    assert "HassTurnOn" not in _tool_names(service)  # first fetch failed

    assert await _wait_for(lambda: "HassTurnOn" in _tool_names(service)), _tool_names(service)
    assert "HassTurnOn" in service._functions
    # The same filtering as the normal path.
    assert "HassMediaSearchAndPlay" not in _tool_names(service)
    assert _tool_names(service).count("play_media") == 1


@pytest.mark.asyncio
async def test_recovery_running_twice_does_not_duplicate_tools(monkeypatch):
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)

    connection, service = await _connect(app)
    assert await _wait_for(lambda: "HassTurnOn" in _tool_names(service))
    await app._apply_ha_tools(service, _schema())

    assert _tool_names(service).count("HassTurnOn") == 1
    assert len(_tool_names(service)) == len(set(_tool_names(service)))


@pytest.mark.asyncio
async def test_recovery_stops_when_the_device_disconnects(monkeypatch):
    from app.providers import OPENAI
    from app.websocket_handler import WebSocketHandler

    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=10**6)

    connection, service = await _connect(app)
    task = connection.ha_tools_task
    assert task is not None and not task.done()

    handler = WebSocketHandler(host="127.0.0.1", port=0)
    await handler._teardown(connection)
    await asyncio.sleep(0.1)
    assert task.done()


@pytest.mark.asyncio
async def test_no_recovery_task_when_the_first_fetch_worked(monkeypatch):
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=0)

    connection, service = await _connect(app)
    assert connection.ha_tools_task is None
    assert _tool_names(service).count("HassTurnOn") == 1


@pytest.mark.asyncio
async def test_a_connected_openai_session_is_sent_the_new_tool_list():
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app)

    sent = []

    async def _update_settings():
        sent.append(_tool_names(service))

    service._websocket = object()  # connected
    service._update_settings = _update_settings
    await app._apply_ha_tools(service, _schema())

    assert sent and "HassTurnOn" in sent[0]
    connection.ha_tools_task.cancel()


@pytest.mark.asyncio
async def test_gemini_gets_the_tools_for_its_next_connect(monkeypatch):
    from app.providers import GEMINI

    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    app = _bare_app(GEMINI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, GEMINI)

    def names():
        return [d["name"] for g in service._tools_from_init for d in g["function_declarations"]]

    assert "HassTurnOn" not in names()
    assert await _wait_for(lambda: "HassTurnOn" in names()), names()
    assert names().count("play_media") == 1
    assert "HassTurnOn" in service._functions
