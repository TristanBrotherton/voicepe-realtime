"""MCP session policy (shared session and session per call) and the bounded web search tool."""
import asyncio
import contextlib
import dataclasses
import os
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pipecat.services.mcp_service as mcp_module
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.mcp_service import StreamableHttpParameters

from app import web_search_tool
from app.mcp_service import HomeAssistantMCPService, PerCallMCPClient, PersistentMCPClient
from tests.test_runtime_selection import make_app
from app.voice_runtime import REALTIME

TOOL_NAMES = ("HassTurnOn", "HassTurnOff", "HassLightSet", "HassListAddItem",
              "HassTurnOn0", "HassTurnOn1", "HassTurnOn2", "HassTurnOn3")


class FakeSession:
    """Enough of an MCP ClientSession for pipecat's client: opened by its
    owner task as an async context manager, initialised, then asked for tool
    listings, pings and tool calls. Like the real one, a request that
    finishes after the session was closed fails."""

    instances = 0
    fail_calls = False      # every call raises after "sending"
    fail_names = ()         # these calls raise after "sending"
    blocked = {}            # name -> asyncio.Event the call waits for before answering
    list_delay = 0.0        # seconds a tool listing takes
    fail_lists = False      # a tool listing raises

    def __init__(self, read, write):
        FakeSession.instances += 1
        self.calls = []
        self.lists = 0
        self.pings = 0
        self.closed = False
        self.terminated = False  # Home Assistant restarted: every request is refused

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def initialize(self):
        return None

    async def send_ping(self):
        self.pings += 1
        if self.terminated:
            raise mcp_module.MCPError(-32600, "Session terminated")
        return None

    async def list_tools(self):
        self.lists += 1
        if FakeSession.fail_lists:
            raise RuntimeError("listing failed")
        if FakeSession.list_delay:
            await asyncio.sleep(FakeSession.list_delay)
        if self.closed:
            raise RuntimeError("session closed under the listing")
        return SimpleNamespace(tools=[
            SimpleNamespace(name=name, description=f"{name} tool",
                            input_schema={"type": "object", "properties": {}, "required": []})
            for name in TOOL_NAMES
        ])

    async def call_tool(self, name, arguments=None):
        self.calls.append(name)
        if self.terminated:
            # pipecat treats this as an error of the call, not a lost
            # connection (not CONNECTION_CLOSED), so it does not drop the session
            raise mcp_module.MCPError(-32600, "Session terminated")
        if FakeSession.fail_calls or name in FakeSession.fail_names:
            raise RuntimeError("connection reset after send")
        gate = FakeSession.blocked.get(name)
        if gate is not None:
            await gate.wait()
        if self.closed:
            raise RuntimeError("session closed under the call")
        return SimpleNamespace(content=[mcp_module.TextContent(type="text", text=f"{name} ok")])


@contextlib.asynccontextmanager
async def fake_transport(_params):
    yield ("read", "write")


@dataclasses.dataclass
class Params:
    """The FunctionCallParams fields the guarded handler reads and replaces."""

    function_name: str
    result_callback: Any
    tool_call_id: str = "t"
    arguments: dict = dataclasses.field(default_factory=dict)


def make_params(name):
    results = []

    async def result_callback(result, *, properties=None):
        results.append(result)

    params = Params(name, result_callback)
    params.results = results
    return params


class MCPTests:
    """Cases both modes must pass. pipecat's client is exercised for real;
    only the MCP SDK underneath it (the streamable-HTTP transport and the
    ClientSession) is faked."""

    async def asyncSetUp(self):
        FakeSession.instances = 0
        FakeSession.fail_calls = False
        FakeSession.fail_names = ()
        FakeSession.blocked = {}
        FakeSession.list_delay = 0.0
        FakeSession.fail_lists = False
        self.sessions = []

        def session_factory(read, write):
            session = FakeSession(read, write)
            self.sessions.append(session)
            return session

        self._patches = [
            patch.object(mcp_module, "_streamable_http_transport", fake_transport),
            patch.object(mcp_module, "ClientSession", session_factory),
        ]
        for p in self._patches:
            p.start()
        self.params = StreamableHttpParameters(url="http://example.invalid/mcp")
        self.client = self.make_client()
        self.handlers = {}

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self._patches:
            p.stop()

    async def register(self):
        schema = await self.client.cached_tools()
        for function_schema in schema.standard_tools:
            self.handlers[function_schema.name] = self.client.guarded_handler(function_schema)
        return schema

    async def call(self, name):
        if name not in self.handlers:
            await self.register()
        params = make_params(name)
        await self.handlers[name](params)
        return params

    async def start_blocked_call(self, name):
        """A call that has been sent and is waiting for its answer."""
        FakeSession.blocked[name] = asyncio.Event()
        task = asyncio.create_task(self.call(name))
        while not any(name in session.calls for session in self.sessions):
            await asyncio.sleep(0.005)
        return task

    def sent(self):
        return [name for session in self.sessions for name in session.calls]

    async def test_failure_after_sending_is_not_retried(self):
        await self.call("HassTurnOn")  # registers the handlers
        FakeSession.fail_calls = True
        params = await self.call("HassListAddItem")
        self.assertEqual(len(params.results), 1)
        self.assertIn("may or may not have completed", params.results[0])
        self.assertEqual(self.sent(), ["HassTurnOn", "HassListAddItem"], "sent exactly once")

    async def test_a_result_reaches_the_model_once(self):
        # pipecat delivered the result, then the call raised anyway: the
        # honest "not confirmed" wording must not follow a result already
        # reported for the same tool_call_id.
        schema = (await self.register()).standard_tools[0]
        params = make_params(schema.name)
        with self.wrapper_delivers_then_fails():
            await self.client.guarded_handler(schema)(params)
        self.assertEqual(params.results, [f"{schema.name} ok"])

    def wrapper_delivers_then_fails(self):
        """pipecat delivered the result, then its wrapper failed anyway
        (a timeout landing late): patched where this mode reaches it."""
        raise NotImplementedError

    async def test_a_cancelled_delivery_still_gets_the_fallback_result(self):
        # The model's callback hangs and the call bound cancels it mid-delivery:
        # the call must not stay IN_PROGRESS forever, so the fallback result
        # is still delivered once the callback answers again.
        schema = (await self.register()).standard_tools[0]
        self.client.CALL_TIMEOUT_S = 0.05
        results = []

        async def hangs_once(result, *, properties=None):
            if not results:
                results.append("hung")
                await asyncio.Event().wait()
            results.append(result)

        await self.client.guarded_handler(schema)(Params(schema.name, hangs_once))
        self.assertEqual(len(results), 2, results)
        self.assertIn("may or may not have completed", results[1])

    async def test_unreachable_home_assistant_reports_cleanly(self):
        await self.call("HassTurnOn")  # registers the handlers
        await self.client.aclose()

        @contextlib.asynccontextmanager
        async def broken(_params):
            raise OSError("connection refused")
            yield  # pragma: no cover

        with patch.object(mcp_module, "_streamable_http_transport", broken):
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
        second = await self.client.cached_tools()
        self.assertIs(second, first)
        self.assertEqual(sum(session.lists for session in self.sessions), listings,
                         "no second listing while the first is fresh")
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        await self.client.cached_tools()
        self.assertEqual(sum(session.lists for session in self.sessions), listings + 1,
                         "a stale list is fetched again")

    async def test_failed_refresh_keeps_the_previous_tool_list(self):
        first = await self.client.cached_tools()
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        FakeSession.fail_lists = True
        self.assertIs(await self.client.cached_tools(), first)
        FakeSession.fail_lists = False
        self.client.OPEN_TIMEOUT_S = 0.05
        FakeSession.list_delay = 5.0
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        self.assertIs(await self.client.cached_tools(), first, "a timed-out refresh keeps it too")

    async def test_failed_refresh_is_not_retried_on_every_connect(self):
        first = await self.client.cached_tools()
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        FakeSession.fail_lists = True
        self.assertIs(await self.client.cached_tools(), first)
        attempts = sum(session.lists for session in self.sessions)
        self.assertIs(await self.client.cached_tools(), first)
        self.assertEqual(sum(session.lists for session in self.sessions), attempts,
                         "no new attempt within the back-off")
        self.client._tools_cached_at -= self.client.TOOLS_RETRY_S + 1
        FakeSession.fail_lists = False
        self.assertIsNot(await self.client.cached_tools(), first, "tried again after the back-off")

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
    """The default: one session shared by every device, never closed under a call."""

    def make_client(self):
        return PersistentMCPClient(self.params)

    def wrapper_delivers_then_fails(self):
        # The shared client's handler is the wrapper object built at its
        # construction, so the patch goes on the wrapper class.
        deliver = mcp_module._ToolWrapperWithCleanup.__call__

        async def deliver_then_fail(wrapper, call_params):
            await deliver(wrapper, call_params)
            raise RuntimeError("after result")

        return patch.object(mcp_module._ToolWrapperWithCleanup, "__call__", deliver_then_fail)

    async def test_tool_calls_reuse_one_session(self):
        for name in ("HassTurnOn", "HassTurnOff", "HassLightSet"):
            params = await self.call(name)
            self.assertEqual(params.results, [f"{name} ok"])
        self.assertEqual(FakeSession.instances, 1)
        self.assertEqual(self.sessions[0].calls, ["HassTurnOn", "HassTurnOff", "HassLightSet"])

    async def test_home_assistant_restart_costs_one_call_not_a_minute(self):
        # After a Home Assistant restart the old session answers every
        # request with "Session terminated", which pipecat does not treat as
        # a lost connection, so it keeps the session. The failed call makes
        # it suspect; the next call pings it, finds it dead and replaces it.
        await self.call("HassTurnOn")  # registers the handlers
        self.sessions[0].terminated = True
        failed = await self.call("HassTurnOff")
        self.assertIn("may or may not have completed", failed.results[0])
        self.assertEqual((await self.call("HassLightSet")).results, ["HassLightSet ok"])
        self.assertEqual(FakeSession.instances, 2)
        self.assertEqual(self.sessions[1].calls, ["HassLightSet"])

    async def test_call_timeout_makes_the_session_suspect(self):
        await self.call("HassTurnOn")  # registers the handlers
        self.client.CALL_TIMEOUT_S = 0.05
        FakeSession.blocked["HassTurnOff"] = asyncio.Event()  # never answers
        timed_out = await self.call("HassTurnOff")
        self.assertIn("may or may not have completed", timed_out.results[0])
        self.client.CALL_TIMEOUT_S = 45.0
        await self.call("HassLightSet")
        self.assertEqual(self.sessions[0].pings, 1, "pinged before the next call, idle or not")

    async def test_dead_session_replacement_waits_for_the_calls_on_it(self):
        await self.call("HassTurnOn")  # registers the handlers
        other = await self.start_blocked_call("HassTurnOn1")
        session = self.sessions[0]
        session.terminated = True
        self.client._last_used = 0.0
        replacement = asyncio.create_task(self.call("HassTurnOn2"))
        await asyncio.sleep(0.1)
        self.assertFalse(session.closed, "waits for the call still on the old session")
        FakeSession.blocked["HassTurnOn1"].set()
        self.assertEqual((await other).results, ["HassTurnOn1 ok"])
        self.assertEqual((await replacement).results, ["HassTurnOn2 ok"])
        self.assertTrue(session.closed)
        self.assertEqual(FakeSession.instances, 2)

    async def test_dead_session_replacement_gives_up_waiting_after_the_bound(self):
        await self.call("HassTurnOn")  # registers the handlers
        other = await self.start_blocked_call("HassTurnOn1")  # stuck on the old session
        self.sessions[0].terminated = True
        self.client._last_used = 0.0
        self.client.DRAIN_TIMEOUT_S = 0.1
        self.assertEqual((await self.call("HassTurnOn2")).results, ["HassTurnOn2 ok"])
        self.assertEqual(FakeSession.instances, 2)
        FakeSession.blocked["HassTurnOn1"].set()
        self.assertIn("may or may not have completed", (await other).results[0])

    async def test_concurrent_calls_share_the_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        results = await asyncio.gather(*(self.call(f"HassTurnOn{i}") for i in range(4)))
        self.assertEqual(FakeSession.instances, 1)
        self.assertTrue(all(p.results for p in results))

    async def test_idle_session_is_pinged_before_reuse(self):
        await self.call("HassTurnOn")
        listings = self.sessions[0].lists
        self.client._last_used -= self.client.IDLE_PING_S + 1
        await self.call("HassTurnOn")
        self.assertEqual(self.sessions[0].pings, 1, "one ping before reuse")
        self.assertEqual(self.sessions[0].lists, listings, "the probe is not a tool listing")
        self.assertEqual(FakeSession.instances, 1, "a healthy idle session is kept")

    async def test_idle_time_counts_from_the_end_of_the_last_call(self):
        await self.call("HassTurnOn")  # registers the handlers
        FakeSession.blocked["HassTurnOn1"] = asyncio.Event()
        task = asyncio.create_task(self.call("HassTurnOn1"))
        await asyncio.sleep(0.02)
        self.client._last_used -= self.client.IDLE_PING_S + 1  # the call started long ago
        FakeSession.blocked["HassTurnOn1"].set()
        await task
        await self.call("HassTurnOn2")
        self.assertEqual(self.sessions[0].pings, 0, "a session that just answered is not idle")

    async def test_dead_idle_session_is_reopened_before_sending(self):
        await self.call("HassTurnOn")
        original = self.sessions[0]

        async def broken_ping():
            raise RuntimeError("session gone")

        original.send_ping = broken_ping
        self.client._last_used -= self.client.IDLE_PING_S + 1
        params = await self.call("HassTurnOn")
        self.assertEqual(params.results, ["HassTurnOn ok"])
        self.assertEqual(FakeSession.instances, 2)
        self.assertEqual(original.calls, ["HassTurnOn"], "nothing was sent on the dead session")

    async def test_failed_ping_waits_for_the_listing_on_the_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        session = self.sessions[0]
        self.client._tools_cached_at -= self.client.TOOLS_CACHE_S + 1
        FakeSession.list_delay = 0.1
        listing = asyncio.create_task(self.client.cached_tools())
        await asyncio.sleep(0.02)

        async def broken_ping():
            raise RuntimeError("no answer")

        session.send_ping = broken_ping
        self.client._last_used -= self.client.IDLE_PING_S + 1
        await self.call("HassTurnOn2")  # the ping fails while the listing is in flight
        self.assertEqual(len((await listing).standard_tools), len(TOOL_NAMES), "the listing completed")
        self.assertEqual(FakeSession.instances, 2)

    async def test_failure_after_sending_does_not_abort_other_calls(self):
        await self.call("HassTurnOn")  # registers the handlers
        other = await self.start_blocked_call("HassTurnOn1")
        FakeSession.fail_names = ("HassListAddItem",)
        failed = await self.call("HassListAddItem")
        self.assertIn("may or may not have completed", failed.results[0])
        self.assertFalse(self.sessions[0].closed, "not closed under the call still in flight")
        FakeSession.blocked["HassTurnOn1"].set()
        self.assertEqual((await other).results, ["HassTurnOn1 ok"])
        self.assertEqual(FakeSession.instances, 1)

    async def test_session_closed_before_sending_is_reopened_not_reported_as_uncertain(self):
        # pipecat raises _NotConnectedError when a call reaches it after the
        # session was closed (its own drop of a dead session). Nothing was
        # sent, so the call is retried on a new session, never answered with
        # "may or may not have completed".
        schema = (await self.register()).standard_tools[0]
        closed_once = []

        async def closes_then_calls(params):
            if not closed_once:
                closed_once.append(True)
                await self.client.close()
            await schema.handler(params)

        handler = self.client.guarded_handler(FunctionSchema(
            name=schema.name, description=schema.description, properties=schema.properties,
            required=schema.required, handler=closes_then_calls,
        ))
        params = make_params(schema.name)
        await handler(params)
        self.assertEqual(params.results, [f"{schema.name} ok"])
        self.assertEqual(self.sessions[0].calls, [], "nothing was sent on the closed session")
        self.assertEqual(self.sessions[1].calls, [schema.name])

    async def test_in_flight_count_survives_a_cancelled_result_delivery(self):
        # The call raised after sending and the honest result's delivery is
        # cancelled (pipeline teardown): the in-flight count must still
        # come back down, or the session is never reopened again.
        await self.call("HassTurnOn")  # registers the handlers

        async def transport_gone(wrapper, call_params):
            raise RuntimeError("transport gone")

        async def cancelled_callback(result, *, properties=None):
            raise asyncio.CancelledError()

        with patch.object(mcp_module._ToolWrapperWithCleanup, "__call__", transport_gone):
            with self.assertRaises(asyncio.CancelledError):
                await self.handlers["HassTurnOff"](Params("HassTurnOff", cancelled_callback))
        self.assertEqual(self.client._in_flight, 0)

    async def test_in_flight_count_survives_a_cancelled_call(self):
        await self.call("HassTurnOn")  # registers the handlers
        task = await self.start_blocked_call("HassTurnOn1")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client._in_flight, 0)


class TestPerCallMCP(MCPTests, unittest.IsolatedAsyncioTestCase):
    """MCP_PERSISTENT_SESSION=false: a client and session per call, nothing shared."""

    def make_client(self):
        return PerCallMCPClient(self.params)

    def wrapper_delivers_then_fails(self):
        # A fresh client per call resolves its wrapper at call time.
        deliver = mcp_module.MCPClient._tool_wrapper

        async def deliver_then_fail(client, call_params):
            await deliver(client, call_params)
            raise RuntimeError("after result")

        return patch.object(mcp_module.MCPClient, "_tool_wrapper", deliver_then_fail)

    async def test_a_call_does_not_list_the_tools_first(self):
        await self.register()
        params = await self.call("HassTurnOn")
        self.assertEqual(params.results, ["HassTurnOn ok"])
        self.assertEqual(self.sessions[-1].lists, 0, "the call went straight to the tool")
        self.assertEqual(self.sessions[-1].calls, ["HassTurnOn"])

    async def test_each_call_has_its_own_session_closed_afterwards(self):
        for name in ("HassTurnOn", "HassTurnOff"):
            params = await self.call(name)
            self.assertEqual(params.results, [f"{name} ok"])
        self.assertEqual(FakeSession.instances, 3, "one for the listing, one per call")
        self.assertTrue(all(session.closed for session in self.sessions))

    async def test_concurrent_calls_never_share_a_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        before = FakeSession.instances
        results = await asyncio.gather(*(self.call(f"HassTurnOn{i}") for i in range(4)))
        self.assertTrue(all(p.results == [f"{p.function_name} ok"] for p in results))
        self.assertEqual(FakeSession.instances, before + 4)
        self.assertTrue(all(session.closed for session in self.sessions))

    async def test_a_call_finishing_never_closes_another_calls_session(self):
        await self.call("HassTurnOn")  # registers the handlers
        other = await self.start_blocked_call("HassTurnOn1")
        FakeSession.fail_names = ("HassListAddItem",)
        failed = await self.call("HassListAddItem")
        self.assertIn("may or may not have completed", failed.results[0])
        FakeSession.blocked["HassTurnOn1"].set()
        self.assertEqual((await other).results, ["HassTurnOn1 ok"])


class TestMCPStartupLog(unittest.IsolatedAsyncioTestCase):
    async def test_startup_log_says_what_each_mode_does(self):
        service = HomeAssistantMCPService("http://example.invalid/mcp", "token")
        with patch.dict(os.environ, {"MCP_PERSISTENT_SESSION": "false"}):
            with self.assertLogs("app.mcp_service", level="INFO") as per_call:
                self.assertIsInstance(await service.initialize(), PerCallMCPClient)
        with patch.dict(os.environ, {"MCP_PERSISTENT_SESSION": "true"}):
            with self.assertLogs("app.mcp_service", level="INFO") as persistent:
                self.assertIsInstance(await service.initialize(), PersistentMCPClient)
        self.assertTrue(any("a session per call" in line for line in per_call.output), per_call.output)
        self.assertTrue(any("kept open" in line for line in persistent.output), persistent.output)


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
