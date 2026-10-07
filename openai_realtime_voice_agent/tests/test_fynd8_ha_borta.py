"""Finding 8 (granskning 2026-10-04): HA tools, and a probe that blocks the loop.

(a) The tool list is fetched once per connection. When that fetch failed, or
returned fewer tools than a list this process has already seen, the next wake
fetches it again and puts the tools on the session that is about to connect.
It does not close the speaker. Closing the socket is what woke the cloud
again (vakna_vid_start) and is the repair the review rejects. Nothing here
may call sova, book minutes, or send session.update into a live session
(that left OpenAI deaf, 2026-10-01).

(b) The backup probe is awaited. A two-second GET must not freeze the loop,
and it is not moved onto a thread.
"""
import asyncio
import inspect
import time

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from tests.test_ha_tools_recovery import _connect, _schema, _tool_names
from tests.test_provider_selection import _bare_app


def _one(name):
    return ToolsSchema(standard_tools=[
        FunctionSchema(name=name, description="x", properties={}, required=[]),
    ])


class _Queue:
    """Each get_tools_schema takes the next item. An Exception is raised."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0
        self.registered = []

    async def get_tools_schema(self):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def register_tools_schema(self, schema, service):
        self.registered.append([f.name for f in schema.standard_tools])
        for f in schema.standard_tools:
            service.register_function(f.name, lambda params: None)


def _names_gemini(service):
    tools = service._tools_from_init or []
    decls = next(t["function_declarations"] for t in tools if "function_declarations" in t)
    return [d["name"] for d in decls]


def _sleep_untouched(service):
    """The wake refetch must not connect, sleep, or book the cloud."""
    service.sover = True
    service._uppkopplad_sedan = None
    booked = []
    service.bokfor = lambda: booked.append(1)
    return booked


@pytest.mark.asyncio
async def test_misslyckad_hamtning_fylls_vid_nasta_vakning():
    """HA was down at connect. The next wake loads the tools. The speaker stays up."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _Queue([RuntimeError("404 Not Found"), _schema()])
    connection, service = await _connect(app, phase="idle")
    assert "HassTurnOn" not in _tool_names(service)
    booked = _sleep_untouched(service)

    await app.hamta_verktyg_vid_vakning(connection)

    assert "HassTurnOn" in _tool_names(service)
    assert "HassMediaSearchAndPlay" not in _tool_names(service)
    assert connection.websocket.closes == []
    assert service.sover is True
    assert service._uppkopplad_sedan is None
    assert booked == []
    assert app.mcp_client.registered == [["HassTurnOn", "HassMediaSearchAndPlay"]]


@pytest.mark.asyncio
async def test_farre_verktyg_hamtas_om_vid_vakning_utan_session_update():
    """A shorter list than one already seen is incomplete. Wake fills it in place."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    full, short = _schema(), _one("HassMediaSearchAndPlay")
    app.mcp_client = _Queue([full, short, full])
    first, _ = await _connect(app, phase="idle")
    assert "HassTurnOn" in _tool_names(first.openai_service)

    connection, service = await _connect(app, phase="idle")
    connection.device_id = "kitchen"
    assert "HassTurnOn" not in _tool_names(service)
    sent = []

    async def _update_settings():
        sent.append("session.update")

    service._update_settings = _update_settings
    _sleep_untouched(service)
    await app.hamta_verktyg_vid_vakning(connection)

    assert "HassTurnOn" in _tool_names(service)
    assert sent == []
    assert connection.websocket.closes == []
    assert service.sover is True
    assert service._uppkopplad_sedan is None
    assert app.mcp_client.calls == 3


@pytest.mark.asyncio
async def test_en_hel_lista_hamtas_inte_om():
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app.mcp_client = _Queue([_schema(), _schema()])
    connection, service = await _connect(app, phase="idle")
    await app.hamta_verktyg_vid_vakning(connection)
    assert app.mcp_client.calls == 1
    assert _tool_names(service).count("HassTurnOn") == 1


@pytest.mark.asyncio
async def test_kort_svar_vid_vakning_lamnar_listan_och_forsoker_nasta_gang():
    """A wake that still sees fewer tools must not shrink the session."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app._bast_ha_verktyg = 2
    app.mcp_client = _Queue([
        _one("HassMediaSearchAndPlay"),
        _one("HassTurnOff"),
        _schema(),
    ])
    connection, service = await _connect(app, phase="idle")
    assert "HassTurnOn" not in _tool_names(service)
    before = _tool_names(service)

    await app.hamta_verktyg_vid_vakning(connection)
    assert _tool_names(service) == before
    assert connection.ha_verktyg_saknas is True

    await app.hamta_verktyg_vid_vakning(connection)
    assert "HassTurnOn" in _tool_names(service)
    assert connection.ha_verktyg_saknas is False


@pytest.mark.asyncio
async def test_gemini_far_verktygen_vid_vakning():
    from app.device_registry import DeviceConnection
    from app.providers import GEMINI
    from tests.test_ha_tools_recovery import _Phase, _Socket

    app = _bare_app(GEMINI)
    app.mcp_client = _Queue([RuntimeError("404"), _schema()])
    connection = DeviceConnection(device_id="office", websocket=_Socket())
    connection.provider = GEMINI
    connection.phase_emitter = _Phase("idle")
    service = await app.create_service(connection)
    connection.openai_service = service
    assert "HassTurnOn" not in _names_gemini(service)
    _sleep_untouched(service)

    await app.hamta_verktyg_vid_vakning(connection)

    assert "HassTurnOn" in _names_gemini(service)
    assert service.sover is True
    assert service._uppkopplad_sedan is None
    assert connection.websocket.closes == []


@pytest.mark.asyncio
async def test_ha_tillbaka_stanger_inte_hogtalaren(monkeypatch):
    """The old repair closed the socket once HA answered. That woke the cloud."""
    from app.providers import OPENAI
    from tests.test_ha_tools_recovery import _FlakyMcpClient

    monkeypatch.setenv("MCP_TOOLS_RETRY_SECONDS", "0.05")
    monkeypatch.setenv("MCP_RECYCLE_POLL_SECONDS", "0.05")
    monkeypatch.setenv("MCP_RECYCLE_QUIET_SECONDS", "0")
    app = _bare_app(OPENAI)
    app.mcp_client = _FlakyMcpClient(failures=1)
    connection, service = await _connect(app, phase="idle")
    _sleep_untouched(service)

    await asyncio.sleep(0.4)

    assert connection.websocket.closes == []
    assert service.sover is True
    assert service._uppkopplad_sedan is None


def test_vakningen_hamtar_fore_molnet_kopplas():
    from app.main import Application
    from app.websocket_handler import WebSocketHandler

    app = Application()
    app.websocket_handler = WebSocketHandler(host="127.0.0.1", port=0)
    app._wire_websocket_handler()
    assert app.websocket_handler.hamta_verktyg == app.hamta_verktyg_vid_vakning

    src = inspect.getsource(WebSocketHandler.build_pipeline)
    wake = src.split("async def _on_device_wake", 1)[1]
    assert wake.index("self.hamta_verktyg") < wake.index("recovery.vakna()")


# --- (b) the backup probe must not block the loop ---------------------------


@pytest.mark.asyncio
async def test_reservproben_vantar_och_fryser_inte_loopen():
    """Two backups, the first slow and sick. Other work runs during the wait."""
    from app.provider_router import ProviderRouter
    from tests.test_provider_router import FakeClock

    order = []

    async def probe(name):
        order.append("start " + name)
        await asyncio.sleep(0.2)
        order.append("end " + name)
        return name == "openai"

    async def other():
        await asyncio.sleep(0.05)
        order.append("other")

    router = ProviderRouter(
        "gemini", "xai", probe=probe, clock=FakeClock(), extra=["openai"]
    )
    task = asyncio.create_task(other())
    after = await router.report_failure("gemini", "insufficient_quota")
    await task

    assert after == "openai"
    assert order.index("other") < order.index("end xai")
    assert "start openai" in order
    assert order.index("end xai") < order.index("start openai")


@pytest.mark.asyncio
async def test_probe_engine_ar_asynkron(monkeypatch):
    import httpx

    import app.main as main

    monkeypatch.setenv("XAI_API_KEY", "xai-test")

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"data": []})

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    seen = []

    async def other():
        await asyncio.sleep(0.05)
        seen.append("other")

    task = asyncio.create_task(other())
    started = time.monotonic()
    ok = await main.probe_engine("xai")
    await task

    assert ok is True
    assert seen == ["other"]
    assert time.monotonic() - started < 1.0
    src = inspect.getsource(main.probe_engine)
    assert "to_thread" not in src
    assert "run_in_executor" not in src


@pytest.mark.asyncio
async def test_natet_nere_vantar_en_asynkron_prob():
    """An async probe that says down must count as down. to_thread would see a coroutine."""
    from app import bana0

    async def probe(engine):
        await asyncio.sleep(0.05)
        return False

    assert await bana0.natet_nere(probe, ("xai", "gemini"), 0.5) is True


def test_routern_vantar_pa_report_failure():
    from app.websocket_handler import ConnectionRecovery

    src = inspect.getsource(ConnectionRecovery._report)
    assert "await self._router.report_failure" in src
