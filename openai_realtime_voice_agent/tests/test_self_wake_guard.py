"""The Workshop regression of 2026-10-07: the device woke on its own voice.

What happened: two GetDateTime lookups answered correctly, but the lookup's
measured median had drifted past the slow-tool threshold, so the add-on played
"One moment." out of the device's speaker first. The Voice PE runs
micro_wake_word in firmware, heard that phrase, and sent ``{"type":"wake"}``.
The device silences playback on wake, and the new wake/interrupt cancelled the
answer the user was waiting for. The user heard an acknowledgement and then
nothing.

Two independent fixes, tested separately here because either alone leaves a
hole:

* the **cause** — a fast local read like GetDateTime never gets a spoken
  acknowledgement, whatever its measured median says
  (:class:`TestFastLocalReadsAreSilent`);
* the **mechanism** — an acoustic wake arriving while our own out-of-band audio
  is playing is dropped, so an echo can never cancel a turn
  (:class:`TestSelfWakeGuard`).

Deliberate stop and barge-in must keep working, so both are asserted too: a
button wake is never dropped, ``{"type":"interrupt"}`` is never dropped, and a
wake outside the echo window is accepted normally.
"""
import asyncio
import tempfile
import time
import unittest
from dataclasses import dataclass
from typing import Any

from app.main import SafeRealtimeLLMService
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import SELF_WAKE_TAIL_S, RawAudioSerializer
from app.spoken_prompts import (
    DEFAULT_SILENT_ACK_TOOLS,
    PROMPTS,
    SpokenPrompts,
    TTSCache,
    bare_tool_name,
)
from app.turn_timeline import TurnTimeline


# ---------------------------------------------------------------------------
# the cause: no acknowledgement for a fast local read
# ---------------------------------------------------------------------------

@dataclass
class Params:
    function_name: str
    tool_call_id: str
    arguments: Any
    llm: Any = None
    context: Any = None
    result_callback: Any = None


class TestSilentAckList(unittest.TestCase):
    def test_get_date_time_is_silent_by_default(self):
        self.assertIn("GetDateTime", DEFAULT_SILENT_ACK_TOOLS)

    def test_the_mcp_namespace_prefix_is_ignored(self):
        self.assertEqual(bare_tool_name("llm__GetDateTime"), "GetDateTime")
        self.assertEqual(bare_tool_name("GetDateTime"), "GetDateTime")
        self.assertEqual(bare_tool_name(""), "")

    def test_silent_wins_over_a_slow_measured_median(self):
        prompts = SpokenPrompts(TTSCache("", "alloy", cache_dir="/tmp"), play=None)
        self.assertFalse(prompts.is_slow("GetDateTime", 9.0))
        self.assertFalse(prompts.is_slow("llm__GetDateTime", 9.0))
        self.assertTrue(prompts.is_slow("HassListAddItem", 9.0),
                        "an actually-slow tool still qualifies")

    def test_silent_wins_over_an_explicit_slow_tools_entry(self):
        prompts = SpokenPrompts(TTSCache("", "alloy", cache_dir="/tmp"), play=None,
                                slow_tools=("GetDateTime",))
        self.assertFalse(prompts.is_slow("GetDateTime", None),
                         "the silent list is the stronger statement")

    def test_the_silent_list_is_configurable(self):
        prompts = SpokenPrompts(TTSCache("", "alloy", cache_dir="/tmp"), play=None,
                                silent_ack_tools=("script__my_fast_script",))
        self.assertTrue(prompts.is_silent("my_fast_script"))
        self.assertFalse(prompts.is_silent("GetDateTime"))


class TestFastLocalReadsAreSilent(unittest.IsolatedAsyncioTestCase):
    """The GetDateTime flow, end to end through the real tool guard."""

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
        service.turn_timeline = TurnTimeline("workshop")
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
            await params.result_callback("2026-10-07T15:04:00-07:00")
        self.service.register_function(name, handler)

    async def call(self, name):
        async def cb(_result):
            pass
        await self.registered[name](Params(name, "c1", {}, result_callback=cb))

    async def test_a_slow_get_date_time_still_plays_nothing(self):
        """The exact Workshop condition: p50 above the threshold, call slow."""
        for _ in range(5):
            self.service.turn_timeline.stats.add("tool.llm__GetDateTime", 2500)
        self.register("llm__GetDateTime", 0.15)
        await self.call("llm__GetDateTime")
        await asyncio.sleep(0.15)
        self.assertEqual(self.played, [], "no acknowledgement, so nothing to wake on")

    async def test_two_consecutive_lookups_stay_silent(self):
        for _ in range(5):
            self.service.turn_timeline.stats.add("tool.llm__GetDateTime", 2500)
        self.register("llm__GetDateTime", 0.15)
        await self.call("llm__GetDateTime")
        await self.call("llm__GetDateTime")
        await asyncio.sleep(0.15)
        self.assertEqual(self.played, [])

    async def test_the_answer_is_still_returned(self):
        """Silence is about the acknowledgement, not about the result."""
        results = []
        for _ in range(5):
            self.service.turn_timeline.stats.add("tool.llm__GetDateTime", 2500)

        async def handler(params):
            await params.result_callback("2026-10-07T15:04:00-07:00")
        self.service.register_function("llm__GetDateTime", handler)

        async def cb(result):
            results.append(result)
        await self.registered["llm__GetDateTime"](
            Params("llm__GetDateTime", "c1", {}, result_callback=cb))
        self.assertEqual(results, ["2026-10-07T15:04:00-07:00"])
        self.assertEqual(self.played, [])

    async def test_a_genuinely_slow_tool_is_unaffected(self):
        self.register("web_search", 0.15)
        await self.call("web_search")
        self.assertEqual(self.played, ["workshop"])


# ---------------------------------------------------------------------------
# the mechanism: an echo wake cannot cancel a turn
# ---------------------------------------------------------------------------

class TestSelfWakeGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.serializer = RawAudioSerializer("workshop")
        self.wakes = []
        self.interrupts = []

        async def on_wake(meta):
            self.wakes.append(meta)

        async def on_interrupt():
            self.interrupts.append(True)

        self.serializer.set_wake_handler(on_wake)
        self.serializer.set_interrupt_handler(on_interrupt)

    def play_announcement(self, seconds=1.0):
        """What _play_prompt / the timer announcer do before speaking."""
        self.serializer.suppress_inbound_until = time.monotonic() + seconds

    async def wake(self, **meta):
        await self.serializer._handle_control({"type": "wake", "turn": "t1", **meta})

    async def test_an_acoustic_wake_during_our_own_audio_is_dropped(self):
        self.play_announcement()
        await self.wake(src="wake_word")
        self.assertEqual(self.wakes, [], "that was our own voice coming back")
        self.assertEqual(self.serializer.self_wakes_suppressed, 1)

    async def test_firmware_without_src_is_treated_as_acoustic(self):
        self.play_announcement()
        await self.wake()
        self.assertEqual(self.wakes, [])

    async def test_the_guard_outlives_the_audio_by_a_short_tail(self):
        """micro_wake_word scores a sliding window, so the report lags the audio."""
        self.serializer.suppress_inbound_until = time.monotonic() + 0.01
        await asyncio.sleep(0.05)
        self.assertTrue(self.serializer.is_self_wake({"src": "wake_word"}))
        self.assertGreater(SELF_WAKE_TAIL_S, 0.0)

    async def test_a_button_wake_is_never_dropped(self):
        self.play_announcement()
        await self.wake(src="button")
        self.assertEqual(len(self.wakes), 1, "a human finger always gets through")
        self.assertEqual(self.serializer.self_wakes_suppressed, 0)

    async def test_stop_is_never_dropped(self):
        """Barge-in must survive: interrupt is a different control frame."""
        self.play_announcement()
        await self.serializer._handle_control({"type": "interrupt"})
        self.assertEqual(self.interrupts, [True])

    async def test_a_wake_outside_the_window_is_accepted(self):
        self.serializer.suppress_inbound_until = time.monotonic() - 10.0
        await self.wake(src="wake_word")
        self.assertEqual(len(self.wakes), 1)
        self.assertEqual(self.serializer.self_wakes_suppressed, 0)

    async def test_a_wake_with_no_announcement_at_all_is_accepted(self):
        await self.wake(src="wake_word")
        self.assertEqual(len(self.wakes), 1)

    async def test_reply_audio_does_not_arm_the_guard(self):
        """Barge-in on the assistant's own reply is intentional and preserved.

        Only out-of-band prompts and announcements set suppress_inbound_until;
        normal reply audio never does, so a wake during a reply is a real
        interruption and reaches the handler.
        """
        self.assertEqual(self.serializer.suppress_inbound_until, 0.0)
        await self.wake(src="wake_word")
        self.assertEqual(len(self.wakes), 1)

    async def test_a_dropped_wake_leaves_no_turn_state_behind(self):
        """It must be as if the frame never arrived.

        A self-wake that updated the turn id, cleared reply-audio tracking or
        armed the first-audio ack would corrupt the turn in progress even
        without reaching the wake handler.
        """
        self.serializer.current_turn_id = "real-turn"
        self.serializer._reply_audio_since_wake = True
        self.serializer._ack_pending = False
        last_wake = self.serializer._last_wake_mono
        self.play_announcement()
        await self.wake(src="wake_word", turn="echo-turn")
        self.assertEqual(self.serializer.current_turn_id, "real-turn")
        self.assertTrue(self.serializer._reply_audio_since_wake)
        self.assertFalse(self.serializer._ack_pending)
        self.assertEqual(self.serializer._last_wake_mono, last_wake)


if __name__ == "__main__":
    unittest.main()
