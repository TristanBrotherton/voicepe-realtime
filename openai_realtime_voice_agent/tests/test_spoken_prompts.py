"""Slow-tool acknowledgements and audible errors."""
import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from app.live_voice_prompts import LiveVoiceUnavailable
from app.main import SafeRealtimeLLMService
from app.phase_emitter import TurnLiveness
from app.progress_ack import GENERIC_CUES
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

    async def test_dynamic_text_is_memory_only(self):
        FakeHttp.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            tts = TTSCache("key", "alloy", cache_dir=tmp, client_factory=lambda: FakeHttp())
            text = "Adding cookies to the shopping list, Sam."
            pcm = await tts.get(text, persist=False)
            self.assertIsNotNone(pcm)
            self.assertEqual(os.listdir(tmp), [], "request text must not be persisted")
            self.assertEqual(await tts.get(text, persist=False), pcm)
            self.assertEqual(FakeHttp.calls, 1)

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


LIVE_PCM = b"\x02\x00" * 2400


def live_pcm(line):
    """The Live voice's rendering of one line: recognisably Live audio, and
    different for every line, so a test can tell which line was played."""
    return LIVE_PCM + line.encode("utf-8")


class FakeLiveRenderer:
    def __init__(self, fail=None):
        self.rendered = []
        self.fail = fail

    async def render(self, lines):
        if self.fail:
            raise LiveVoiceUnavailable(self.fail)
        self.rendered.extend(lines)
        return {line: live_pcm(line) for line in lines}


class TestLiveModePromptsUseTheAssistantVoice(unittest.IsolatedAsyncioTestCase):
    """Decision of 2026-10-11: every spoken sound from the device uses the
    assistant's own voice. The speech endpoint does not offer the Live-only
    voices, so a Live-voice install renders its prompts through a Live
    session; this fails if any spoken prompt reaches the speech-endpoint
    path while the runtime speaks in a Live voice."""

    def setUp(self):
        FakeHttp.calls = 0
        self.tmp = tempfile.mkdtemp()
        self.renderer = FakeLiveRenderer()
        self.tts = TTSCache("key", "alloy", cache_dir=self.tmp,
                            client_factory=lambda: FakeHttp(),
                            live_voice="vesper", live_renderer=self.renderer)
        self.played = []

        async def play(pcm, device_id):
            self.played.append((pcm, device_id))
            return True

        self.prompts = SpokenPrompts(self.tts, play)

    async def test_warm_renders_every_prompt_in_the_live_voice_and_never_the_endpoint(self):
        await self.prompts.warm()
        self.assertEqual(set(self.renderer.rendered), set(PROMPTS.values()))
        self.assertEqual(FakeHttp.calls, 0)
        for text in PROMPTS.values():
            self.assertEqual(self.tts.cached(text), live_pcm(text))
        self.assertTrue(set(GENERIC_CUES.values()) <= set(PROMPTS.values()),
                        "every finite progress cue is pre-rendered")

    async def test_every_spoken_path_plays_live_voice_audio_only(self):
        await self.prompts.warm()
        for key in PROMPTS:
            self.assertTrue(await self.prompts.say(key, "kitchen"), key)
        for tool in ("intent__HassTurnOff", "todo__HassListAddItem", "web_search",
                     "script__ask_openclaw", "set_timer", "", "script__something_unknown"):
            self.assertTrue(await self.prompts.say_progress(
                tool, {"name": "bedside lamp", "item": "cookies"}, "Sam",
                "turn off the bedside lamp", "kitchen"), tool)
        self.assertTrue(await self.prompts.say_text(
            "Adding cookies to the shopping list, Sam.", "kitchen"))
        self.assertTrue(self.played)
        self.assertTrue(all(pcm.startswith(LIVE_PCM) for pcm, _ in self.played),
                        "only Live-voice audio reaches the device")
        self.assertEqual(FakeHttp.calls, 0,
                         "no spoken prompt reached the speech endpoint in Live mode")
        self.assertNotIn("Adding cookies to the shopping list, Sam.", self.renderer.rendered,
                         "a dynamic filler is never rendered while the user waits")

    async def test_the_progress_cue_is_the_contextual_pre_rendered_line_not_the_generic_ack(self):
        await self.prompts.warm()
        rendered_before = list(self.renderer.rendered)
        expectations = {
            ("intent__HassTurnOff", "turn off the bedside lamp"): GENERIC_CUES["progress_turn_off"],
            ("intent__HassTurnOn", "turn on the bedside lamp"): GENERIC_CUES["progress_turn_on"],
            ("todo__HassListAddItem", "add cookies to the shopping list"): GENERIC_CUES["progress_list_add"],
            ("set_timer", "set a timer for ten minutes"): GENERIC_CUES["progress_timer_set"],
            ("", "turn off the bedside lamp"): GENERIC_CUES["progress_turn_off"],
            ("web_search", "what is the tallest mountain"): GENERIC_CUES["progress_check_that"],
        }
        for (tool, transcript), cue in expectations.items():
            self.played.clear()
            self.assertTrue(await self.prompts.say_progress(
                tool, {"name": "bedside lamp", "item": "cookies"}, "Sam", transcript, "kitchen"))
            self.assertEqual(len(self.played), 1)
            pcm, device = self.played[0]
            self.assertEqual(device, "kitchen")
            self.assertEqual(pcm, live_pcm(cue),
                             f"{tool or 'pre-tool'}: the cue chosen from the request's shape is played")
            self.assertNotEqual(pcm, live_pcm(PROMPTS["ack"]),
                                f"{tool or 'pre-tool'}: not the generic acknowledgement")
        self.assertEqual(self.renderer.rendered, rendered_before,
                         "nothing is rendered while the user waits; every cue was pre-rendered")
        self.assertEqual(FakeHttp.calls, 0)

    async def test_a_filler_stays_silent_rather_than_speaking_in_another_voice(self):
        self.renderer.fail = "unreachable"
        with self.assertLogs("app.spoken_prompts", level="WARNING") as logs:
            self.assertFalse(await self.prompts.say("ack", "kitchen"))
            self.assertFalse(await self.prompts.say_progress(
                "intent__HassTurnOn", {}, None, "turn on the lamp", "kitchen"))
        self.assertEqual(FakeHttp.calls, 0)
        self.assertEqual(self.played, [])
        self.assertTrue(any("prompt voice unavailable" in m and "unreachable" in m
                            for m in logs.output))

    async def test_an_error_falls_back_to_the_endpoint_only_when_the_live_voice_is_unreachable(self):
        self.renderer.fail = "unreachable"
        with self.assertLogs("app.spoken_prompts", level="WARNING") as logs:
            self.assertTrue(await self.prompts.say("error_budget", "kitchen"))
        self.assertEqual(FakeHttp.calls, 1)
        self.assertTrue(any("prompt voice fallback" in m for m in logs.output))
        self.assertEqual(self.played[-1][0], FakeResponse.content)

    async def test_endpoint_audio_is_never_mistaken_for_the_live_voice(self):
        plain = TTSCache("key", "alloy", cache_dir=self.tmp, client_factory=lambda: FakeHttp())
        await plain.get(PROMPTS["ack"])
        self.assertIsNone(self.tts.cached(PROMPTS["ack"]),
                          "an older endpoint rendering on disk is not the assistant's voice")

    async def test_a_dynamic_line_renders_on_demand_in_the_live_voice(self):
        pcm = await self.tts.get("Your timer is done.")
        self.assertEqual(pcm, live_pcm("Your timer is done."))
        self.assertIn("Your timer is done.", self.renderer.rendered)
        self.assertEqual(FakeHttp.calls, 0)

    async def test_without_a_live_voice_the_endpoint_path_is_unchanged(self):
        plain = TTSCache("key", "alloy", cache_dir=self.tmp, client_factory=lambda: FakeHttp())
        prompts = SpokenPrompts(plain, self.prompts._play)
        self.assertFalse(plain.live)
        self.assertTrue(await prompts.say_progress(
            "todo__HassListAddItem", {"item": "cookies", "list": "shopping list"}, "Sam",
            "add cookies to the shopping list", "kitchen"))
        self.assertEqual(FakeHttp.calls, 1)


if __name__ == "__main__":
    unittest.main()
