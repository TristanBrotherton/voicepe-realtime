"""Persistent MCP session reuse and the bounded web search tool."""
import asyncio
import contextlib
import dataclasses
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pipecat.services.mcp_service as mcp_module
from pipecat.services.mcp_service import StreamableHttpParameters

from app import web_search_tool
from app.mcp_service import PersistentMCPClient

TOOL_NAMES = ("HassTurnOn", "HassTurnOff", "HassLightSet", "HassListAddItem",
              "HassTurnOn0", "HassTurnOn1", "HassTurnOn2", "HassTurnOn3")


class FakeSession:
    """Enough of an MCP ClientSession for pipecat's client: opened by its
    owner task as an async context manager, initialised, then asked for tool
    listings and tool calls."""

    instances = 0
    fail_calls = False

    def __init__(self, read, write):
        FakeSession.instances += 1
        self.calls = []
        self.lists = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        self.lists += 1
        return SimpleNamespace(tools=[
            SimpleNamespace(name=name, description=f"{name} tool",
                            input_schema={"type": "object", "properties": {}, "required": []})
            for name in TOOL_NAMES
        ])

    async def call_tool(self, name, arguments=None):
        self.calls.append(name)
        if FakeSession.fail_calls:
            raise RuntimeError("connection reset after send")
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


class TestPersistentMCP(unittest.IsolatedAsyncioTestCase):
    """The session policy on top of pipecat 1.x's own persistent MCPClient.

    pipecat's client is exercised for real; only the MCP SDK underneath it
    (the streamable-HTTP transport and the ClientSession) is faked.
    """

    async def asyncSetUp(self):
        FakeSession.instances = 0
        FakeSession.fail_calls = False
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
        self.client = PersistentMCPClient(StreamableHttpParameters(url="http://example.invalid/mcp"))
        self.handlers = {}

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self._patches:
            p.stop()

    async def call(self, name):
        if name not in self.handlers:
            schema = await self.client.tools()
            for function_schema in schema.standard_tools:
                self.handlers[function_schema.name] = self.client.guarded_handler(function_schema)
        params = make_params(name)
        await self.handlers[name](params)
        return params

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

    async def test_idle_session_is_probed_before_reuse(self):
        await self.call("HassTurnOn")
        listings = self.sessions[0].lists
        self.client._last_used -= self.client.IDLE_PING_S + 1
        await self.call("HassTurnOn")
        self.assertEqual(self.sessions[0].lists, listings + 1, "one probe round trip before reuse")
        self.assertEqual(FakeSession.instances, 1, "a healthy idle session is kept")

    async def test_dead_idle_session_is_reopened_before_sending(self):
        await self.call("HassTurnOn")
        original = self.sessions[0]

        async def broken_list_tools():
            raise RuntimeError("session gone")

        original.list_tools = broken_list_tools
        self.client._last_used -= self.client.IDLE_PING_S + 1
        params = await self.call("HassTurnOn")
        self.assertEqual(params.results, ["HassTurnOn ok"])
        self.assertEqual(FakeSession.instances, 2)
        self.assertEqual(original.calls, ["HassTurnOn"], "nothing was sent on the dead session")

    async def test_failure_after_sending_is_not_retried(self):
        await self.call("HassTurnOn")  # registers the handlers on a healthy session
        FakeSession.fail_calls = True
        params = await self.call("HassListAddItem")
        self.assertEqual(len(params.results), 1)
        self.assertIn("may or may not have completed", params.results[0])
        self.assertEqual(self.sessions[0].calls, ["HassTurnOn", "HassListAddItem"], "sent exactly once")

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
