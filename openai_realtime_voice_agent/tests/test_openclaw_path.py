"""The OpenClaw request shape (candidate-v35): answer-only prefix, a fresh
session key per request, and never two in-flight requests on one key."""
import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import openclaw_tool

BRIDGE = Path(__file__).resolve().parents[2] / "examples" / "openclaw-bridge" / "bridge.mjs"


class FakeLLM:
    def __init__(self):
        self.handlers = {}

    def register_function(self, name, handler):
        self.handlers[name] = handler


class RecordingClient:
    """httpx.AsyncClient stand-in. ``hold`` keeps every post in flight until
    released, so overlapping requests are observable."""
    payloads = []
    in_flight = []
    max_overlap_on_one_key = 0
    hold = None

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        cls = RecordingClient
        cls.payloads.append(json)
        key = json.get("session_key")
        cls.in_flight.append(key)
        cls.max_overlap_on_one_key = max(cls.max_overlap_on_one_key, cls.in_flight.count(key))
        try:
            if cls.hold is not None:
                await cls.hold.wait()
        finally:
            cls.in_flight.remove(key)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"answer": "ok"})


def _reset():
    RecordingClient.payloads = []
    RecordingClient.in_flight = []
    RecordingClient.max_overlap_on_one_key = 0
    RecordingClient.hold = None


async def _ask(llm, question, call_id):
    results = []

    async def cb(result):
        results.append(result)

    await llm.handlers["ask_openclaw"](SimpleNamespace(
        arguments={"question": question}, tool_call_id=call_id, result_callback=cb))
    return results


ENV = {"OPENCLAW_URL": "http://agent.invalid/ask", "INSTANCE_NAME": "workshop"}


class TestAnswerOnlyPrefix(unittest.IsolatedAsyncioTestCase):
    async def test_question_sent_to_the_bridge_carries_the_prefix(self):
        _reset()
        llm = FakeLLM()
        with patch.dict("os.environ", ENV), \
             patch.object(openclaw_tool.httpx, "AsyncClient", RecordingClient):
            openclaw_tool.register_openclaw_tool(llm, device_id="office")
            results = await _ask(llm, "what is on the calendar tomorrow", "call-1")
        sent = RecordingClient.payloads[0]
        self.assertTrue(sent["question"].startswith(openclaw_tool.ANSWER_ONLY_PREFIX))
        self.assertTrue(sent["question"].endswith("\nwhat is on the calendar tomorrow"))
        self.assertIs(sent["answer_only"], True)
        self.assertEqual(results, [{"answer": "ok"}])

    def test_prefix_asks_for_few_lookups_and_a_short_spoken_reply(self):
        prefix = openclaw_tool.ANSWER_ONLY_PREFIX.lower()
        self.assertIn("fewest", prefix)
        self.assertIn("one to three short sentences", prefix)
        self.assertIn("only when it actually succeeded", prefix)
        # Voice PE hands over real tasks (messages, calls): no read-only clause.
        self.assertNotIn("take no action", prefix)
        self.assertLess(len(openclaw_tool.ANSWER_ONLY_PREFIX), 800)

    def test_an_oversized_question_is_sent_whole_without_the_prefix(self):
        long_question = "x" * (openclaw_tool.BRIDGE_MAX_QUESTION_CHARS - 10)
        fields = openclaw_tool.bridge_request_fields(long_question, "office", "call-2")
        self.assertEqual(fields["question"], long_question)
        self.assertIs(fields["answer_only"], False)
        short = openclaw_tool.bridge_request_fields("hi", "office", "call-3")
        self.assertLessEqual(len(short["question"]), openclaw_tool.BRIDGE_MAX_QUESTION_CHARS)


class TestPerRequestSessionKeys(unittest.IsolatedAsyncioTestCase):
    async def test_every_request_gets_its_own_voicepe_key(self):
        _reset()
        llm = FakeLLM()
        with patch.dict("os.environ", ENV), \
             patch.object(openclaw_tool.httpx, "AsyncClient", RecordingClient):
            openclaw_tool.register_openclaw_tool(llm, device_id="office")
            for n in range(5):
                await _ask(llm, "follow up", f"call-{n}")
            # A repeated tool-call id still gets a new session.
            await _ask(llm, "follow up", "call-0")
        keys = [p["session_key"] for p in RecordingClient.payloads]
        self.assertEqual(len(set(keys)), len(keys))
        for n, key in enumerate(keys[:5]):
            self.assertTrue(key.startswith(f"voicepe-office-call-{n}-"), key)

    def test_keys_have_the_shape_the_bridge_accepts(self):
        source = BRIDGE.read_text(encoding="utf-8")
        shape = re.search(r"const SESSION_KEY_SHAPE = /(.+)/;", source).group(1)
        odd = [("Kitchen Voice PE", "call_AbC/12:3"), ("", ""), ("ö" * 50, "x" * 200),
               ("office", "")]
        for origin, request_id in odd:
            key = openclaw_tool.request_session_key(origin, request_id)
            self.assertRegex(key, shape)
            self.assertLessEqual(len(key), openclaw_tool.SESSION_KEY_MAX_CHARS)
            self.assertTrue(key.startswith("voicepe-"))

    async def test_room_is_the_origin_when_no_device_is_named(self):
        _reset()
        llm = FakeLLM()
        with patch.dict("os.environ", ENV), \
             patch.object(openclaw_tool.httpx, "AsyncClient", RecordingClient):
            openclaw_tool.register_openclaw_tool(llm)
            await _ask(llm, "anything", "call-9")
        self.assertTrue(RecordingClient.payloads[0]["session_key"].startswith("voicepe-workshop-call-9-"))


class TestNoParallelRequestsOnOneKey(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_requests_never_share_a_session_key(self):
        _reset()
        RecordingClient.hold = asyncio.Event()
        llm = FakeLLM()
        with patch.dict("os.environ", ENV), \
             patch.object(openclaw_tool.httpx, "AsyncClient", RecordingClient):
            openclaw_tool.register_openclaw_tool(llm, device_id="office")
            # Same tool-call id on purpose: the worst case (a retried call).
            tasks = [asyncio.create_task(_ask(llm, "same question", "call-dup")) for _ in range(8)]
            for _ in range(50):
                await asyncio.sleep(0)
                if len(RecordingClient.in_flight) == 8:
                    break
            self.assertEqual(len(RecordingClient.in_flight), 8)
            self.assertEqual(len(set(RecordingClient.in_flight)), 8)
            RecordingClient.hold.set()
            await asyncio.gather(*tasks)
        self.assertEqual(RecordingClient.max_overlap_on_one_key, 1)
        # The request id stays the same, so the bridge's deduper can still
        # join a true duplicate and run it once.
        self.assertEqual({p["request_id"] for p in RecordingClient.payloads}, {"call-dup"})


if __name__ == "__main__":
    unittest.main()
