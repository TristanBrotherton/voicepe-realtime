"""Application picks the runtime from VOICE_RUNTIME; both get the same tools.

Legacy regression: with the selector unset the Realtime service is built with
the same session properties as before; the GPT-Live service receives the
identical tool list and the split prompts.
"""
import os
import unittest
from unittest.mock import patch

from app.device_registry import DeviceConnection
from app.main import (
    Application,
    SafeRealtimeLLMService,
    live_backend_instructions,
    live_conversation_instructions,
)
from app.live_service import OpenAILiveLLMService
from app.session_manager import SessionManager
from app.timers import TimerRegistry
from app.turn_timeline import TurnTimeline
from app.voice_runtime import LIVE, REALTIME, LiveConfig, resolve_voice_runtime


def make_app(runtime):
    app = Application()
    app.openai_api_key = "sk-test"
    app.session_manager = SessionManager(reuse_timeout=300, max_restored_messages=12)
    app.voice_runtime = runtime
    app.live_config = LiveConfig(voice="marin", acknowledge_gaps=True) if runtime == LIVE else None
    app.enable_disconnect_tool = False
    app.enable_web_search = True
    app.web_search_model = "gpt-5.5"
    app.mcp_client = None
    app.mcp_tool_allowlist = []
    app.action_gate = None
    app.spoken_prompts = None
    app.enrollment_conductor = None
    app.wake_events = None
    app.timer_registry = TimerRegistry()
    app.speaker_male_name = ""
    app.speaker_female_name = ""
    app.male_only_tools = set()
    app.instructions = "You are the house."
    app.model = "gpt-realtime-2"
    app.voice = "marin"
    app.openai_speed = 1.0
    app.max_output_tokens = None
    app.noise_reduction = ""
    app.transcription_language = ""
    app.transcription_model = "gpt-4o-transcribe"
    app.turn_detection_type = "semantic_vad"
    app.vad_eagerness = "low"
    app.vad_threshold = 0.5
    app.vad_prefix_padding_ms = 300
    app.vad_silence_duration_ms = 800
    app.interrupt_response = False
    app.semantic_vad_create_response = True
    return app


def connection():
    return DeviceConnection("kitchen", object(), turn_timeline=TurnTimeline("kitchen"))


EXPECTED_TOOLS = ["web_search", "voice_enrollment", "mark_false_wake", "set_timer", "cancel_timer",
                  "list_timers", "remember", "forget", "list_memories"]


class TestRuntimeSelection(unittest.IsolatedAsyncioTestCase):
    async def test_unset_selector_builds_the_realtime_service_as_before(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOICE_RUNTIME", None)
            self.assertEqual(resolve_voice_runtime(), REALTIME)
        app = make_app(REALTIME)
        service = await app.create_openai_service(connection())
        self.assertIsInstance(service, SafeRealtimeLLMService)
        props = service._session_properties
        self.assertEqual([t["name"] for t in props.tools], EXPECTED_TOOLS)
        self.assertEqual(props.audio.output.voice, "marin")
        self.assertEqual(props.audio.input.turn_detection.type, "semantic_vad")
        self.assertIn("TURN ADMISSION", props.instructions)
        self.assertNotIn("Delegation policy", props.instructions)
        self.assertIs(app.session_manager.get_current_service("kitchen"), service)
        self.assertIsNotNone(getattr(service, "_context", None), "Realtime pre-seeds the context")

    async def test_live_selector_builds_the_live_service_with_the_same_tools(self):
        app = make_app(LIVE)
        service = await app.create_openai_service(connection())
        self.assertIsInstance(service, OpenAILiveLLMService)
        self.assertEqual([t["name"] for t in service.tools], EXPECTED_TOOLS)
        self.assertIn("Delegation policy", service.instructions)
        self.assertIn("TURN ADMISSION", service.instructions)
        self.assertIn("Web search", service.instructions)
        self.assertIn("Timers", service.instructions)
        self.assertIn("Return the result", service.backend_instructions)
        self.assertIn("You are the house.", service.backend_instructions)
        self.assertEqual(service.config.voice, "marin")
        self.assertIs(app.session_manager.get_current_service("kitchen"), service)
        # Every tool handler is registered on the Live service through the shared guards.
        for name in EXPECTED_TOOLS:
            self.assertTrue(service.has_function(name), name)
        self.assertIs(service.turn_timeline, app.session_manager.get_current_service("kitchen").turn_timeline)

    async def test_live_service_is_seeded_from_the_cached_context(self):
        from pipecat.processors.aggregators.llm_context import LLMContext

        app = make_app(LIVE)
        cached = LLMContext(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
        app.session_manager.context_caches["kitchen"] = type("E", (), {})()
        app.session_manager.context_caches["kitchen"].context = cached
        import time
        app.session_manager.context_caches["kitchen"].timestamp = time.time()
        service = await app.create_openai_service(connection())
        self.assertEqual([i["role"] for i in service._seed_history], ["user", "assistant"])
        self.assertIn("kitchen", app.session_manager.context_caches, "the cache is still there for the aggregator")


class TestPromptSplit(unittest.TestCase):
    def test_backend_prompt_default_and_override(self):
        default = live_backend_instructions("", confirmations=True)
        self.assertIn("Voice conversation context", default)
        self.assertIn("confirm_action", default)
        custom = live_backend_instructions("Only do timers.", confirmations=False)
        self.assertTrue(custom.startswith("Only do timers."))
        self.assertNotIn("confirm_action", custom)
        self.assertIn("Return the result", custom)
        with_context = live_backend_instructions(
            "Only do timers.", confirmations=False, trusted_context="Prefer the kitchen."
        )
        self.assertIn("Trusted assistant context", with_context)
        self.assertIn("Prefer the kitchen.", with_context)

    def test_conversation_prompt_lists_backend_capabilities(self):
        tools = [{"name": "HassTurnOn"}, {"name": "web_search"}, {"name": "ask_openclaw"}, {"name": "custom_tool"}]
        prompt = live_conversation_instructions("Base.", tools)
        self.assertTrue(prompt.startswith("Base."))
        for needle in ("Smart home", "Web search", "Personal assistant", "custom_tool", "Interruption policy",
                       "Smart-home actions and state are backend-only", "MUST be delegated",
                       "Do not simulate a successful tool result"):
            self.assertIn(needle, prompt)
        self.assertIn("(no backend tools are configured)", live_conversation_instructions("x", []))

    def test_backend_only_rule_follows_conflicting_style_examples(self):
        style = 'For routine confirmations say "Done." or "Of course."'
        prompt = live_conversation_instructions(style, [{"name": "HassTurnOn"}])
        self.assertGreater(prompt.index("backend-only"), prompt.index('say "Done."'))
        self.assertGreater(prompt.index("Do not simulate a successful tool result"),
                           prompt.index('say "Done."'))


class TestSessionManagerRestorable(unittest.TestCase):
    def test_restorable_messages_trims_and_keeps_the_cache(self):
        from pipecat.processors.aggregators.llm_context import LLMContext
        import time

        manager = SessionManager(reuse_timeout=300, max_restored_messages=2)
        self.assertEqual(manager.restorable_messages("kitchen"), [])
        messages = [{"role": "system", "content": "s"}] + [{"role": "user", "content": f"m{i}"} for i in range(5)]
        manager.context_caches["kitchen"] = type("E", (), {"context": LLMContext(messages=messages),
                                                           "timestamp": time.time()})()
        restored = manager.restorable_messages("kitchen")
        self.assertEqual([m["content"] for m in restored], ["s", "m3", "m4"])
        self.assertIn("kitchen", manager.context_caches)
        # The aggregator path trims identically.
        context = manager.create_context_for_new_session("kitchen")
        self.assertEqual([m["content"] for m in context.get_messages()], ["s", "m3", "m4"])


if __name__ == "__main__":
    unittest.main()
