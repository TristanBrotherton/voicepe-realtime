"""A Home Assistant that hangs must not stop new sessions (raawr US-014 AC-2).

2026-09-30: HA was restarted; the agent kept receiving audio but never opened
a model session until it was itself restarted 35 minutes later. create_service
fetches HA's MCP tool list under the pipeline lock, and pipecat's MCP client
lets a read sit for up to sse_read_timeout (300 s) per attempt -- so one hung
fetch held the lock and every reconnect queued behind it.
"""
import asyncio

import pytest

from tests.test_provider_selection import _bare_app


class _HungMcpClient:
    """get_tools_schema never returns, like HA mid-restart behind the proxy."""

    def __init__(self):
        self.registered = False

    async def get_tools_schema(self):
        await asyncio.Event().wait()

    async def register_tools_schema(self, _schema, _service):
        self.registered = True


@pytest.mark.asyncio
async def test_a_hung_mcp_fetch_times_out_and_the_session_is_built_without_ha_tools(monkeypatch):
    from app.device_registry import DeviceConnection
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_TOOLS_TIMEOUT_SECONDS", "0.2")
    app = _bare_app(OPENAI)
    app.mcp_client = _HungMcpClient()

    for device in ("office", "kitchen"):  # the second proves the lock was released
        connection = DeviceConnection(device_id=device, websocket=object())
        connection.provider = OPENAI
        # The guard: without the fix this hangs; wait_for turns that into a failure.
        service = await asyncio.wait_for(app.create_service(connection), timeout=2.0)
        assert service is not None

    assert app.mcp_client.registered is False
