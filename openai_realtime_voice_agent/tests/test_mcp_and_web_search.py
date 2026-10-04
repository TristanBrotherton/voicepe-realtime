"""Persistent MCP session reuse and the bounded web search tool."""
import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pipecat.services.mcp_service import StreamableHttpParameters

from app import web_search_tool
from app.mcp_service import PersistentMCPClient


class FakeSession:
    instances = 0

    def __init__(self, read, write, fail_calls=False):
        FakeSession.instances += 1
        self.calls = []
        self.fail_calls = fail_calls
        self.pings = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def send_ping(self):
        self.pings += 1

    async def call_tool(self, name, arguments=None):
        self.calls.append(name)
        if self.fail_calls:
            raise RuntimeError("connection reset after send")
        return SimpleNamespace(content=[SimpleNamespace(text=f"{name} ok")])


@contextlib.asynccontextmanager
async def fake_transport(**_kwargs):
    yield ("read", "write", None)


class Params:
    def __init__(self, name):
        self.function_name = name
        self.tool_call_id = "t"
        self.arguments = {}
        self.results = []

    async def result_callback(self, result):
        self.results.append(result)


def make_client(fail_calls=False):
    client = PersistentMCPClient(StreamableHttpParameters(url="http://example.invalid/mcp"))
    client._client = fake_transport
    client._session = lambda r, w: FakeSession(r, w, fail_calls=fail_calls)
    return client


class TestPersistentMCP(unittest.IsolatedAsyncioTestCase):
    async def test_tool_calls_reuse_one_session(self):
        FakeSession.instances = 0
        client = make_client()
        for name in ("HassTurnOn", "HassTurnOff", "HassLightSet"):
            params = Params(name)
            await client._streamable_http_tool_wrapper(params)
            self.assertEqual(params.results, [f"{name} ok"])
        self.assertEqual(FakeSession.instances, 1)
        self.assertEqual(client.session_opens, 1)
        await client.aclose()

    async def test_concurrent_calls_share_the_session(self):
        FakeSession.instances = 0
        client = make_client()
        params = [Params(f"HassTurnOn{i}") for i in range(4)]
        await asyncio.gather(*(client._streamable_http_tool_wrapper(p) for p in params))
        self.assertEqual(FakeSession.instances, 1)
        self.assertTrue(all(p.results for p in params))
        await client.aclose()

    async def test_idle_session_is_pinged_before_reuse(self):
        client = make_client()
        await client._streamable_http_tool_wrapper(Params("HassTurnOn"))
        client._last_used -= client.IDLE_PING_S + 1
        await client._streamable_http_tool_wrapper(Params("HassTurnOn"))
        self.assertEqual(client._persistent_session.pings, 1)
        await client.aclose()

    async def test_failure_after_sending_is_not_retried(self):
        client = make_client(fail_calls=True)
        params = Params("HassListAddItem")
        await client._streamable_http_tool_wrapper(params)
        self.assertEqual(len(params.results), 1)
        self.assertIn("may or may not have completed", params.results[0])
        await client.aclose()

    async def test_unreachable_home_assistant_reports_cleanly(self):
        client = make_client()

        @contextlib.asynccontextmanager
        async def broken(**_kwargs):
            raise OSError("connection refused")
            yield  # pragma: no cover

        client._client = broken
        params = Params("HassTurnOn")
        await client._streamable_http_tool_wrapper(params)
        self.assertIn("not reachable", params.results[0])


class FakeResponses:
    def __init__(self, delay=0.0, text="Sunny, 21 degrees."):
        self.delay = delay
        self.text = text

    async def create(self, **_kwargs):
        await asyncio.sleep(self.delay)
        return SimpleNamespace(output_text=self.text)


class TestWebSearch(unittest.IsolatedAsyncioTestCase):
    async def run_search(self, query, responses, timeout="0.05"):
        with patch.dict("os.environ", {"WEB_SEARCH_TIMEOUT_S": timeout}), \
             patch.object(web_search_tool, "AsyncOpenAI") as openai_cls:
            openai_cls.return_value = SimpleNamespace(responses=responses)
            handler = web_search_tool.create_web_search_tool_handler("key", "gpt-5.5")
            out = []

            async def cb(result):
                out.append(result)

            await handler(SimpleNamespace(arguments={"query": query}, result_callback=cb))
            return out[0]

    async def test_answer_passes_through(self):
        self.assertEqual(await self.run_search("weather", FakeResponses()), "Sunny, 21 degrees.")

    async def test_timeout_is_bounded_and_english(self):
        self.assertEqual(await self.run_search("weather", FakeResponses(delay=3)), web_search_tool.TIMED_OUT)

    async def test_empty_query_and_empty_answer_in_english(self):
        self.assertEqual(await self.run_search("", FakeResponses()), web_search_tool.NO_QUERY)
        self.assertEqual(await self.run_search("x", FakeResponses(text="")), web_search_tool.NO_RESULT)


if __name__ == "__main__":
    unittest.main()
