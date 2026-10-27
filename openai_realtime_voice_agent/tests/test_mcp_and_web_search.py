"""Persistent MCP session reuse (and the per-call mode) and the bounded web search tool."""
import asyncio
import contextlib
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp.shared.exceptions import MCPError

from app import mcp_service, web_search_tool
from app.mcp_service import HomeAssistantMCPService, PerCallMCPClient, PersistentMCPClient
from app.voice_runtime import REALTIME
from tests.test_runtime_selection import make_app

TOOL_NAMES = ("HassTurnOn", "HassTurnOff", "HassLightSet", "HassListAddItem",
              "HassTurnOn0", "HassTurnOn1", "HassTurnOn2", "HassTurnOn3")


class FakeSession:
    """Enough of the MCP SDK's ClientSession: entered and exited by the
    session owner, initialised, then pinged, asked for a listing or a call.
    A terminated session answers every request the way Home Assistant does
    after a restart."""

    instances = 0
    fail_calls = False
    list_delay = 0.0
    fail_lists = False

    def __init__(self, read, write):
        FakeSession.instances += 1
        self.calls = []
        self.lists = 0
        self.pings = 0
        self.closed = False
        self.terminated = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def initialize(self):
        return None

    def _refuse(self):
        if self.terminated:
            raise MCPError(-32600, "Session terminated")

    async def send_ping(self):
        self.pings += 1
        self._refuse()

    async def list_tools(self):
        self.lists += 1
        self._refuse()
        if FakeSession.fail_lists:
            raise RuntimeError("listing failed")
        if FakeSession.list_delay:
            await asyncio.sleep(FakeSession.list_delay)
        return SimpleNamespace(tools=[
            SimpleNamespace(name=name, description=f"{name} tool",
                            input_schema={"type": "object", "properties": {}, "required": []})
            for name in TOOL_NAMES
        ])

    async def call_tool(self, name, arguments=None):
        self.calls.append(name)
        self._refuse()
        if FakeSession.fail_calls:
            raise RuntimeError("connection reset after send")
        return SimpleNamespace(content=[SimpleNamespace(text=f"{name} ok")])


@contextlib.asynccontextmanager
async def fake_transport(url, **_kwargs):
    yield ("read", "write")


class Params:
    def __init__(self, name):
        self.function_name = name
        self.tool_call_id = "t"
        self.arguments = {}
        self.results = []

    async def result_callback(self, result):
        self.results.append(result)


class MCPTests:
    """Cases both modes must pass; only the MCP SDK underneath is faked."""

    async def asyncSetUp(self):
        FakeSession.instances = 0
        FakeSession.fail_calls = False
        FakeSession.list_delay = 0.0
        FakeSession.fail_lists = False
        self.sessions = []

        def session_factory(read, write):
            session = FakeSession(read, write)
            self.sessions.append(session)
            return session

        self._patches = [
            patch.object(mcp_service, "streamable_http_client", fake_transport),
            patch.object(mcp_service, "ClientSession", session_factory),
        ]
        for p in self._patches:
            p.start()
        self.client = self.make_client()
        self.handlers = {}

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self._patches:
            p.stop()

    def make_client(self):
        raise NotImplementedError

    async def register(self):
        schema = await self.client.cached_tools()
        for function_schema in schema.standard_tools:
            self.handlers[function_schema.name] = self.client.guarded_handler(function_schema)
        return schema

    async def call(self, name):
        if name not in self.handlers:
            await self.register()
        params = Params(name)
        await self.handlers[name](params)
        return params

    def sent(self):
        return [name for session in self.sessions for name in session.calls]

    async def test_listing_builds_pipecat_schemas(self):
        schema = await self.register()
        self.assertEqual([t.name for t in schema.standard_tools], list(TOOL_NAMES))
        self.assertEqual(schema.standard_tools[0].description, "HassTurnOn tool")
        self.assertEqual(schema.standard_tools[0].properties, {})

    async def test_failure_after_sending_is_not_retried(self):
        await self.call("HassTurnOn")  # registers the handlers
        FakeSession.fail_calls = True
        params = await self.call("HassListAddItem")
        self.assertEqual(len(params.results), 1)
        self.assertIn("may or may not have completed", params.results[0])
        self.assertEqual(self.sent(), ["HassTurnOn", "HassListAddItem"], "sent exactly once")

    async def test_unreachable_home_assistant_reports_cleanly(self):
        await self.call("HassTurnOn")  # registers the handlers
        await self.client.aclose()

        @contextlib.asynccontextmanager
        async def broken(url, **_kwargs):
            raise OSError("connection refused")
            yield  # pragma: no cover

        with patch.object(mcp_service, "streamable_http_client", broken):
            params = await self.call("HassTurnOn")
        self.assertIn("not reachable", params.results[0])

    async def test_tool_list_for_a_device_session_is_bounded(self):
        self.client.OPEN_TIMEOUT_S = 0.05
        FakeSession.list_delay = 5.0
        started = time.monotonic()
        with self.assertRaises(asyncio.TimeoutError):
            await self.client.cached_tools()
        self.assertLess(time.monotonic() - started, 1.0)

    async def test_tool_list_is_reused_by_reconnecting_devices(self):
        first = await self.client.cached_tools()
        listings = sum(session.lists for session in self.sessions)
        self.assertIs(await self.client.cached_tools(), first)
        self.assertEqual(sum(session.lists for session in self.sessions), listings,
                         "no second listing while the first is fresh")
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        await self.client.cached_tools()
        self.assertEqual(sum(session.lists for session in self.sessions), listings + 1,
                         "a stale list is fetched again")

    async def test_failed_refresh_keeps_the_previous_list_and_backs_off(self):
        first = await self.client.cached_tools()
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        FakeSession.fail_lists = True
        self.assertIs(await self.client.cached_tools(), first)
        attempts = sum(session.lists for session in self.sessions)
        self.assertIs(await self.client.cached_tools(), first)
        self.assertEqual(sum(session.lists for session in self.sessions), attempts,
                         "no new attempt within the back-off")
        self.client._tools_failed_at -= self.client.TOOLS_RETRY_S + 1
        FakeSession.fail_lists = False
        self.assertIsNot(await self.client.cached_tools(), first, "tried again after the back-off")

    async def test_failed_first_listing_backs_off_too(self):
        FakeSession.fail_lists = True
        with self.assertRaises(RuntimeError):
            await self.client.cached_tools()
        attempts = sum(session.lists for session in self.sessions)
        with self.assertRaises(ConnectionError):
            await self.client.cached_tools()
        self.assertEqual(sum(session.lists for session in self.sessions), attempts,
                         "no new attempt within the back-off")

    async def test_slow_tool_listing_does_not_stall_session_creation(self):
        # main.py fetches the Home Assistant tools for every new device
        # session; a slow Home Assistant must not hold that up for longer
        # than the open bound.
        self.client.OPEN_TIMEOUT_S = 0.05
        FakeSession.list_delay = 5.0
        app = make_app(REALTIME)
        app.mcp_client = self.client
        tools, schema = await asyncio.wait_for(app._assemble_tools(), 1.0)
        self.assertIsNone(schema)
        self.assertIn("web_search", [tool["name"] for tool in tools])


class TestPersistentMCP(MCPTests, unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        return PersistentMCPClient("http://example.invalid/mcp", {"Authorization": "Bearer t"})

    async def test_tool_calls_reuse_one_session(self):
        for name in ("HassTurnOn", "HassTurnOff", "HassLightSet"):
            params = await self.call(name)
            self.assertEqual(params.results, [f"{name} ok"])
        self.assertEqual(FakeSession.instances, 1)
        self.assertEqual(self.client.session_opens, 1)
        self.assertEqual(self.sessions[0].calls, ["HassTurnOn", "HassTurnOff", "HassLightSet"])

    async def test_concurrent_calls_share_the_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        results = await asyncio.gather(*(self.call(f"HassTurnOn{i}") for i in range(4)))
        self.assertEqual(FakeSession.instances, 1)
        self.assertTrue(all(p.results for p in results))

    async def test_idle_session_is_pinged_before_reuse(self):
        await self.call("HassTurnOn")
        self.client._last_used -= self.client.IDLE_PING_S + 1
        await self.call("HassTurnOn")
        self.assertEqual(self.sessions[0].pings, 1)
        self.assertEqual(FakeSession.instances, 1, "a healthy idle session is kept")

    async def test_dead_idle_session_is_reopened_before_sending(self):
        await self.call("HassTurnOn")
        original = self.sessions[0]
        original.terminated = True
        self.client._last_used -= self.client.IDLE_PING_S + 1
        params = await self.call("HassTurnOn")
        self.assertEqual(params.results, ["HassTurnOn ok"])
        self.assertEqual(FakeSession.instances, 2)
        self.assertEqual(original.calls, ["HassTurnOn"], "nothing was sent on the dead session")

    async def test_failure_after_sending_closes_the_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        FakeSession.fail_calls = True
        await self.call("HassListAddItem")
        self.assertTrue(self.sessions[0].closed)
        FakeSession.fail_calls = False
        self.assertEqual((await self.call("HassTurnOff")).results, ["HassTurnOff ok"])
        self.assertEqual(FakeSession.instances, 2)

    async def test_home_assistant_restart_costs_one_call(self):
        # After a Home Assistant restart the old session answers every
        # request with "Session terminated". That call is reported honestly
        # and the session closed; the next call succeeds on a new one.
        await self.call("HassTurnOn")  # registers the handlers
        self.sessions[0].terminated = True
        failed = await self.call("HassTurnOff")
        self.assertIn("may or may not have completed", failed.results[0])
        self.assertEqual((await self.call("HassLightSet")).results, ["HassLightSet ok"])
        self.assertEqual(FakeSession.instances, 2)
        self.assertEqual(self.sessions[1].calls, ["HassLightSet"])

    async def test_failed_listing_closes_the_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        self.sessions[0].terminated = True
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        await self.client.cached_tools()  # keeps the previous list
        self.assertTrue(self.sessions[0].closed)
        self.assertEqual((await self.call("HassTurnOff")).results, ["HassTurnOff ok"])


class TestPerCallMCP(MCPTests, unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        return PerCallMCPClient("http://example.invalid/mcp", {"Authorization": "Bearer t"})

    async def test_each_call_has_its_own_session_closed_afterwards(self):
        for name in ("HassTurnOn", "HassTurnOff"):
            params = await self.call(name)
            self.assertEqual(params.results, [f"{name} ok"])
        self.assertEqual(FakeSession.instances, 3, "one for the listing, one per call")
        self.assertTrue(all(session.closed for session in self.sessions))
        self.assertEqual([session.lists for session in self.sessions], [1, 0, 0])

    async def test_concurrent_calls_never_share_a_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        before = FakeSession.instances
        results = await asyncio.gather(*(self.call(f"HassTurnOn{i}") for i in range(4)))
        self.assertTrue(all(p.results == [f"{p.function_name} ok"] for p in results))
        self.assertEqual(FakeSession.instances, before + 4)


class TestMCPStartupLog(unittest.IsolatedAsyncioTestCase):
    async def test_startup_log_names_the_mode(self):
        service = HomeAssistantMCPService("http://example.invalid/mcp", "token")
        with patch.dict(os.environ, {"MCP_PERSISTENT_SESSION": "false"}):
            with self.assertLogs("app.mcp_service", level="INFO") as per_call:
                self.assertIsInstance(await service.initialize(), PerCallMCPClient)
        with patch.dict(os.environ, {"MCP_PERSISTENT_SESSION": "true"}):
            with self.assertLogs("app.mcp_service", level="INFO") as persistent:
                self.assertIsInstance(await service.initialize(), PersistentMCPClient)
        self.assertTrue(any("session per call" in line for line in per_call.output), per_call.output)
        self.assertTrue(any("persistent session" in line for line in persistent.output), persistent.output)


class FakeResponses:
    def __init__(self, delay=0.0, text="Sunny, 21 degrees."):
        self.delay = delay
        self.text = text

    async def create(self, **kwargs):
        self.kwargs = kwargs
        await asyncio.sleep(self.delay)
        return SimpleNamespace(output_text=self.text)


class TestWebSearch(unittest.IsolatedAsyncioTestCase):
    async def run_search(self, query, responses, timeout="0.05", model="gpt-5.5"):
        with patch.dict("os.environ", {"WEB_SEARCH_TIMEOUT_S": timeout}), \
             patch.object(web_search_tool, "AsyncOpenAI") as openai_cls:
            openai_cls.return_value = SimpleNamespace(responses=responses)
            handler = web_search_tool.create_web_search_tool_handler("key", model)
            out = []

            async def cb(result):
                out.append(result)

            await handler(SimpleNamespace(arguments={"query": query}, result_callback=cb))
            return out[0]

    async def test_answer_passes_through(self):
        self.assertEqual(await self.run_search("weather", FakeResponses()), "Sunny, 21 degrees.")

    async def test_low_reasoning_only_for_reasoning_models(self):
        responses = FakeResponses()
        await self.run_search("weather", responses)
        self.assertEqual(responses.kwargs.get("reasoning"), {"effort": "low"})
        await self.run_search("weather", responses, model="gpt-4o")
        self.assertNotIn("reasoning", responses.kwargs)

    async def test_timeout_is_bounded_and_english(self):
        self.assertEqual(await self.run_search("weather", FakeResponses(delay=3)), web_search_tool.TIMED_OUT)

    async def test_empty_query_and_empty_answer_in_english(self):
        self.assertEqual(await self.run_search("", FakeResponses()), web_search_tool.NO_QUERY)
        self.assertEqual(await self.run_search("x", FakeResponses(text="")), web_search_tool.NO_RESULT)


if __name__ == "__main__":
    unittest.main()
