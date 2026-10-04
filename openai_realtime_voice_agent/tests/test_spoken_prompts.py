"""Slow-tool acknowledgements and audible errors."""
import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from app.main import SafeRealtimeLLMService
from app.phase_emitter import TurnLiveness
from app.spoken_prompts import PROMPTS, SpokenPrompts, TTSCache, classify_error, pick_voice
from app.turn_timeline import TurnTimeline


class FakeResponse:
    content = b"\x01\x00" * 2400

    def raise_for_status(self):
        pass


class FakeHttp:
    calls = 0

    def __init__(self, fail=False):
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        FakeHttp.calls += 1
        if self.fail:
            raise RuntimeError("rate limited")
        return FakeResponse()


class TestTTSCache(unittest.IsolatedAsyncioTestCase):
    async def test_synthesizes_once_and_caches_to_disk(self):
        FakeHttp.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            tts = TTSCache("key", "alloy", cache_dir=tmp, client_factory=lambda: FakeHttp())
            first = await tts.get("One moment.")
            second = await tts.get("One moment.")
            self.assertEqual(first, second)
            self.assertEqual(FakeHttp.calls, 1)
            fresh = TTSCache("key", "alloy", cache_dir=tmp, client_factory=lambda: FakeHttp(fail=True))
            self.assertEqual(fresh.cached("One moment."), first, "survives restarts")

    async def test_rejected_voice_falls_back_once(self):
        voices = []

        class VoiceHttp(FakeHttp):
            async def post(self, url, headers=None, json=None):
                voices.append(json["voice"])
                response = FakeResponse()
                response.status_code = 400 if json["voice"] == "marin" else 200
                return response

        with tempfile.TemporaryDirectory() as tmp:
            tts = TTSCache("key", "marin", cache_dir=tmp, client_factory=lambda: VoiceHttp())
            self.assertIsNotNone(await tts.get("One moment."))
            self.assertIsNotNone(await tts.get("Sorry."))
        self.assertEqual(voices, ["marin", "alloy", "alloy"])

    async def test_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tts = TTSCache("key", "alloy", cache_dir=tmp, client_factory=lambda: FakeHttp(fail=True))
            self.assertIsNone(await tts.get("x"))

    def test_voice_and_error_helpers(self):
        self.assertEqual(pick_voice("", "marin"), "marin")
        self.assertEqual(pick_voice("custom-voice", "unknown"), "alloy")
        self.assertEqual(classify_error("Rate limit reached for gpt-realtime"), "error_rate_limit")
        self.assertEqual(classify_error("response failed"), "error_generic")


@dataclass
class Params:
    function_name: str
    tool_call_id: str
    arguments: Any
    llm: Any = None
    context: Any = None
    result_callback: Any = None


class TestSlowToolAck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.played = []

        async def play(pcm, device_id):
            self.played.append(device_id)
            return True

        tts = TTSCache("", "alloy", cache_dir=tempfile.mkdtemp())
        tts._memory[tts._key(PROMPTS["ack"])] = b"\x00\x00" * 100
        self.prompts = SpokenPrompts(tts, play, ack_delay_s=0.05)
        service = object.__new__(SafeRealtimeLLMService)
        service.male_only_tools = set()
        service.speaker_probe = None
        service.turn_liveness = TurnLiveness()
        service.turn_timeline = TurnTimeline("kitchen")
        service.turn_timeline.begin("w1")
        service.action_gate = None
        service.spoken_prompts = self.prompts
        self.service = service
        self.registered = {}
        import pipecat.services.openai.realtime.llm as llm_mod
        self._orig = llm_mod.OpenAIRealtimeLLMService.register_function

        def fake_register(_self, name, handler, start_callback=None, cancel_on_interruption=True):
            self.registered[name] = handler

        llm_mod.OpenAIRealtimeLLMService.register_function = fake_register

    async def asyncTearDown(self):
        import pipecat.services.openai.realtime.llm as llm_mod
        llm_mod.OpenAIRealtimeLLMService.register_function = self._orig

    def register(self, name, duration):
        async def handler(params):
            await asyncio.sleep(duration)
            await params.result_callback("ok")
        self.service.register_function(name, handler)

    async def call(self, name):
        async def cb(_result):
            pass
        await self.registered[name](Params(name, "c1", {}, result_callback=cb))

    async def test_slow_tool_gets_one_ack(self):
        self.register("web_search", 0.15)
        await self.call("web_search")
        await self.call("web_search")
        self.assertEqual(self.played, ["kitchen"], "at most one acknowledgement per turn")

    async def test_fast_call_of_slow_tool_is_silent(self):
        self.register("web_search", 0.0)
        await self.call("web_search")
        await asyncio.sleep(0.1)
        self.assertEqual(self.played, [])

    async def test_fast_home_assistant_tools_never_ack(self):
        self.register("HassTurnOn", 0.15)
        await self.call("HassTurnOn")
        self.assertEqual(self.played, [])

    async def test_measured_slow_tool_qualifies(self):
        for _ in range(5):
            self.service.turn_timeline.stats.add("tool.HassListAddItem", 2500)
        self.register("HassListAddItem", 0.15)
        await self.call("HassListAddItem")
        self.assertEqual(self.played, ["kitchen"])

    async def test_disabled_prompts_never_ack(self):
        self.prompts.enabled = False
        self.register("web_search", 0.15)
        await self.call("web_search")
        self.assertEqual(self.played, [])


if __name__ == "__main__":
    unittest.main()
