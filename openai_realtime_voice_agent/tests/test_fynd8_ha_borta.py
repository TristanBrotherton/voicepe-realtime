"""Finding 8 (granskning 2026-10-04): HA tools, and a probe that blocks the loop.

(a) The tool list is fetched once per connection. When that fetch failed, the
next wake fetches it again and puts the tools on the session that is about to
connect. A list Home Assistant actually returned is the truth, even when it
is shorter than one this process has seen: the mark sinks, and the next wake
does not fetch again. The wake fetch has its own ceiling
(MCP_TOOLS_WAKE_TIMEOUT_SECONDS, default 1), so a hung HA does not hold the
cloud for MCP_TOOLS_TIMEOUT_SECONDS. Nothing here closes the speaker, calls
sova, books minutes, or sends session.update into a live session (that left
OpenAI deaf, 2026-10-01).

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


class _Hung:
    """get_tools_schema never returns, like HA mid-restart behind the proxy."""

    def __init__(self):
        self.calls = 0

    async def get_tools_schema(self):
        self.calls += 1
        await asyncio.Event().wait()

    async def register_tools_schema(self, schema, service):
        pass


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
async def test_lyckad_kortare_lista_sanker_market_och_hamtas_inte_igen():
    """HA answered with fewer tools. That list is the truth. The next wake does not refetch."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    short = _one("HassTurnOff")
    app.mcp_client = _Queue([_schema(), short, _schema()])
    first, _ = await _connect(app, phase="idle")
    assert app._bast_ha_verktyg == 2
    assert "HassTurnOn" in _tool_names(first.openai_service)

    connection, service = await _connect(app, phase="idle")
    connection.device_id = "kitchen"
    assert app._bast_ha_verktyg == 1
    assert connection.ha_verktyg_saknas is False
    assert "HassTurnOff" in _tool_names(service)
    assert "HassTurnOn" not in _tool_names(service)
    sent = []

    async def _update_settings():
        sent.append("session.update")

    service._update_settings = _update_settings
    _sleep_untouched(service)
    await app.hamta_verktyg_vid_vakning(connection)

    assert app.mcp_client.calls == 2
    assert app._bast_ha_verktyg == 1
    assert connection.ha_verktyg_saknas is False
    assert "HassTurnOn" not in _tool_names(service)
    assert sent == []
    assert connection.websocket.closes == []
    assert service.sover is True
    assert service._uppkopplad_sedan is None


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
async def test_lyckad_kortare_hamtning_vid_vakning_blir_sanningen():
    """Connect failed while the mark was high. A correct shorter answer on wake is loaded and kept."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app._bast_ha_verktyg = 5
    short = _one("HassTurnOff")
    app.mcp_client = _Queue([RuntimeError("404"), short, short])
    connection, service = await _connect(app, phase="idle")
    assert connection.ha_verktyg_saknas is True
    assert app._bast_ha_verktyg == 5
    assert "HassTurnOff" not in _tool_names(service)
    before = _tool_names(service)
    sent = []

    async def _update_settings():
        sent.append("session.update")

    service._update_settings = _update_settings
    booked = _sleep_untouched(service)

    await app.hamta_verktyg_vid_vakning(connection)

    names = _tool_names(service)
    assert "HassTurnOff" in names
    assert set(before) <= set(names)
    assert app._bast_ha_verktyg == 1
    assert connection.ha_verktyg_saknas is False
    assert sent == []
    assert connection.websocket.closes == []
    assert service.sover is True
    assert service._uppkopplad_sedan is None
    assert booked == []

    await app.hamta_verktyg_vid_vakning(connection)
    assert app.mcp_client.calls == 2


@pytest.mark.asyncio
async def test_misslyckad_vakning_sanker_inte_market():
    """A wake that gets no answer leaves the mark and tries again next time."""
    from app.providers import OPENAI

    app = _bare_app(OPENAI)
    app._bast_ha_verktyg = 5
    app.mcp_client = _Queue([RuntimeError("404"), TimeoutError("no answer"), _schema()])
    connection, service = await _connect(app, phase="idle")
    before = _tool_names(service)

    await app.hamta_verktyg_vid_vakning(connection)

    assert app._bast_ha_verktyg == 5
    assert connection.ha_verktyg_saknas is True
    assert _tool_names(service) == before
    assert app.mcp_client.calls == 2

    await app.hamta_verktyg_vid_vakning(connection)
    assert "HassTurnOn" in _tool_names(service)
    assert app._bast_ha_verktyg == 2
    assert connection.ha_verktyg_saknas is False


@pytest.mark.asyncio
async def test_hangande_ha_vid_vakning_vantar_inte_ut_mcp_taket(monkeypatch):
    """The wake ceiling is its own knob. A hung HA must not sit for MCP_TOOLS_TIMEOUT_SECONDS."""
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_TOOLS_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("MCP_TOOLS_WAKE_TIMEOUT_SECONDS", "0.2")
    app = _bare_app(OPENAI)
    app._bast_ha_verktyg = 4
    app.mcp_client = _Queue([RuntimeError("404")])
    connection, service = await _connect(app, phase="idle")
    assert connection.ha_verktyg_saknas is True
    before = _tool_names(service)
    hung = _Hung()
    app.mcp_client = hung
    booked = _sleep_untouched(service)

    started = time.monotonic()
    try:
        await asyncio.wait_for(app.hamta_verktyg_vid_vakning(connection), timeout=0.7)
    except asyncio.TimeoutError:
        pytest.fail("wake fetch still running after 0.7 s; it is waiting out the long MCP timeout")
    elapsed = time.monotonic() - started

    assert 0.12 <= elapsed < 0.7
    assert hung.calls == 1
    assert connection.ha_verktyg_saknas is True
    assert app._bast_ha_verktyg == 4
    assert _tool_names(service) == before
    assert connection.websocket.closes == []
    assert service.sover is True
    assert service._uppkopplad_sedan is None
    assert booked == []


@pytest.mark.asyncio
async def test_vakningens_tak_ar_en_sekund(monkeypatch):
    """Unset, the wake ceiling is 1 s, not MCP_TOOLS_TIMEOUT_SECONDS."""
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_TOOLS_TIMEOUT_SECONDS", "5")
    monkeypatch.delenv("MCP_TOOLS_WAKE_TIMEOUT_SECONDS", raising=False)
    app = _bare_app(OPENAI)
    app._bast_ha_verktyg = 4
    app.mcp_client = _Queue([RuntimeError("404")])
    connection, service = await _connect(app, phase="idle")
    app.mcp_client = _Hung()

    started = time.monotonic()
    try:
        await asyncio.wait_for(app.hamta_verktyg_vid_vakning(connection), timeout=2.5)
    except asyncio.TimeoutError:
        pytest.fail("wake fetch still running after 2.5 s; the default ceiling is not 1 s")
    elapsed = time.monotonic() - started

    assert 0.6 <= elapsed < 2.0
    assert connection.ha_verktyg_saknas is True
    assert app._bast_ha_verktyg == 4
    assert service is connection.openai_service


@pytest.mark.asyncio
async def test_anslutningens_hamtning_anvander_mcp_taket(monkeypatch):
    """create_service keeps MCP_TOOLS_TIMEOUT_SECONDS. The short ceiling is only the wake."""
    from app.device_registry import DeviceConnection
    from app.providers import OPENAI

    monkeypatch.setenv("MCP_TOOLS_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("MCP_TOOLS_WAKE_TIMEOUT_SECONDS", "5")
    app = _bare_app(OPENAI)
    app.mcp_client = _Hung()
    connection = DeviceConnection(device_id="office", websocket=object())
    connection.provider = OPENAI

    started = time.monotonic()
    service = await asyncio.wait_for(app.create_service(connection), timeout=1.0)
    elapsed = time.monotonic() - started

    assert service is not None
    assert elapsed < 0.8
    assert connection.ha_verktyg_saknas is True


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
