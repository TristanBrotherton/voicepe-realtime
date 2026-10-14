"""Background tasks (app/live_tasks.py): a task list, not a slot.

Every test here pins one promise of the long-task design (audit 2026-10-12,
improvement 1; owner's definition of the two kinds of long task) against the
real ``OpenAILiveLLMService`` in a real pipeline on a fake socket, with a
scripted backend:

* a started tool is never cancelled and its result is never dropped;
* an exact repeat of a pending request joins it instead of running again;
* a slow request becomes a background task: the voice says so, the gate opens
  and the conversation continues; progress is mentioned only when the user is
  not talking;
* COST RULE: a lookup inside a conversation keeps the session only up to the
  hard bound; a deferred watch never holds one; past the bound the session
  closes on its ordinary rules and the result is delivered afterwards by
  voice on the same device (a short new session through the spend gate, or
  an announcement), a text only when the user asked for one or nothing else
  reaches the device; quiet hours hold a deferred result;
* "where is X" goes to the home's direct tool, not the slow general
  assistant;
* an agent hand-off (``ask_openclaw`` accepted) is a PENDING result whose
  commentary is truthful, completed later by ``/task-result`` or ``/announce``.
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from aiohttp import ClientSession
from pipecat.frames.frames import TTSAudioRawFrame

import app.live_service as live_service_module
from app import announce_http, live_tasks
from app.live_router import (
    ACTION, CONFIRM, FAILED, LOOKUP, PENDING, BackendRouter, TurnRouter, person_tool,
)
from app.live_session_gate import CLOSED, OPEN, LiveSessionGate, LiveSessionPolicy
from app.live_cost import LiveSpendLedger
from app.live_tasks import (
    ANNOUNCE, IMESSAGE, REOPEN, SESSION, TaskPolicy, in_quiet_hours, is_deferred_request,
    normalize_request, parse_order, promotion_commentary, quiet_hours_end, result_commentary,
    short_request, spoken_result, wants_text,
)
from app.tool_results import classify_result

from tests.test_live_router import (
    ON, TOOLS, FakeBackend, FirstCallSlow, Patched, RouterHarness, call, message, response,
)

# Longer than USER_FRAGMENT_MERGE_S: a second utterance this long after the
# first is a new request, not a fragment of it.
NEW_UTTERANCE_S = live_service_module.USER_FRAGMENT_MERGE_S + 0.2


# ---------------------------------------------------------------------------
# pure logic
# ---------------------------------------------------------------------------

class TaskPolicyLogic(unittest.TestCase):
    def test_the_order_is_configurable_and_unknown_names_are_ignored(self):
        self.assertEqual(parse_order("announce, imessage,bogus,announce"), (ANNOUNCE, IMESSAGE))
        self.assertEqual(parse_order(""), live_tasks.DEFAULT_ORDER)
        policy = TaskPolicy.from_env({"LIVE_TASK_DELIVERY": "imessage,announce",
                                      "LIVE_TASK_DEFERRED_DELIVERY": "announce",
                                      "LIVE_TASK_PROMOTE_S": "0", "LIVE_TASK_HOLD_SESSION_S": "120",
                                      "LIVE_TASK_IMESSAGE_TO": "owner", "LIVE_TASK_QUIET_HOURS": "22:00-07:00"})
        self.assertEqual(policy.order, (IMESSAGE, ANNOUNCE))
        self.assertEqual(policy.deferred_order, (ANNOUNCE,))
        self.assertGreaterEqual(policy.promote_s, 1.0, "promotion can be delayed, never disabled")
        self.assertEqual(policy.hold_session_s, 120.0)
        self.assertEqual(policy.imessage_to, "owner")
        self.assertEqual(policy.quiet_hours, "22:00-07:00")
        self.assertEqual(TaskPolicy().hold_session_s, 90.0, "the owner's default hard bound")
        self.assertEqual(TaskPolicy().imessage_after_idle_s, 0.0, "a text is not a core path")

    def test_the_plan_uses_the_session_only_while_one_is_open(self):
        policy = TaskPolicy()
        everything = lambda channel: True  # noqa: E731
        self.assertEqual(policy.plan(session_ready=True, idle_s=0, available=everything),
                         [SESSION, REOPEN, ANNOUNCE, IMESSAGE])
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=everything),
                         [REOPEN, ANNOUNCE, IMESSAGE])
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=everything, deferred=True),
                         [ANNOUNCE, REOPEN, IMESSAGE], "a deferred result is an announcement first")
        self.assertEqual(policy.plan(session_ready=False, idle_s=0,
                                     available=lambda c: c in (ANNOUNCE,)), [ANNOUNCE])

    def test_a_text_comes_first_only_when_asked_for(self):
        policy = TaskPolicy()
        everything = lambda channel: True  # noqa: E731
        self.assertEqual(policy.plan(session_ready=False, idle_s=100_000, available=everything),
                         [REOPEN, ANNOUNCE, IMESSAGE], "idle alone does not make a text first")
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=everything, wants_text=True),
                         [IMESSAGE, REOPEN, ANNOUNCE])
        optional = TaskPolicy(imessage_after_idle_s=600)
        self.assertEqual(optional.plan(session_ready=False, idle_s=601, available=everything),
                         [IMESSAGE, REOPEN, ANNOUNCE], "the optional idle rule, when enabled")
        self.assertTrue(wants_text("Text me when the car leaves work"))
        self.assertTrue(wants_text("Send me a message with the result"))
        self.assertFalse(wants_text("Tell me when the car leaves work"))

    def test_quiet_hours_leave_only_the_session_and_a_text_for_a_deferred_result(self):
        policy = TaskPolicy()
        everything = lambda channel: True  # noqa: E731
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=everything,
                                     deferred=True, quiet=True), [IMESSAGE])
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=lambda c: c != IMESSAGE,
                                     deferred=True, quiet=True), [], "nothing: wait for the morning")
        self.assertEqual(policy.plan(session_ready=True, idle_s=0, available=everything,
                                     deferred=True, quiet=True), [SESSION, IMESSAGE],
                         "an open conversation may hear it")
        # Longtask review (iii): quiet hours hold every result from a new
        # voice route, not only a deferred one.
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=everything, quiet=True),
                         [IMESSAGE], "no reopen or announcement for a lookup either")
        self.assertEqual(policy.plan(session_ready=False, idle_s=0, available=lambda c: c != IMESSAGE,
                                     quiet=True), [], "a lookup waits for the morning too")

    def test_quiet_hours_parse_and_cross_midnight(self):
        self.assertTrue(in_quiet_hours("22:00-07:00", datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(in_quiet_hours("22:00-07:00", datetime(2026, 1, 1, 6, 59)))
        self.assertFalse(in_quiet_hours("22:00-07:00", datetime(2026, 1, 1, 7, 0)))
        self.assertFalse(in_quiet_hours("22:00-07:00", datetime(2026, 1, 1, 12, 0)))
        self.assertTrue(in_quiet_hours("13-14", datetime(2026, 1, 1, 13, 15)))
        self.assertFalse(in_quiet_hours("", datetime(2026, 1, 1, 3, 0)))
        self.assertFalse(in_quiet_hours("nonsense", datetime(2026, 1, 1, 3, 0)))
        self.assertAlmostEqual(quiet_hours_end("22:00-07:00", datetime(2026, 1, 1, 6, 0, 0)), 3600.0)
        self.assertEqual(quiet_hours_end("22:00-07:00", datetime(2026, 1, 1, 12, 0)), 0.0)

    def test_a_deferred_request_is_recognised_from_its_words(self):
        for text in ("Tell me when the car leaves work", "Let me know when the washer is done",
                     "Notify me once the garage closes", "Remind me when it starts raining",
                     "Watch for the delivery van"):
            self.assertTrue(is_deferred_request(text), text)
        for text in ("Where is the car", "Turn on the lamp", "When is dinner", "What time is it"):
            self.assertFalse(is_deferred_request(text), text)

    def test_the_join_key_folds_politeness_and_punctuation_only(self):
        self.assertEqual(normalize_request("Turn on the lamp."), normalize_request("turn on the lamp please"))
        self.assertEqual(normalize_request("hey can you turn on the lamp?"), normalize_request("Turn on the lamp"))
        self.assertNotEqual(normalize_request("Turn on the lamp"), normalize_request("Turn off the lamp"))
        self.assertNotEqual(normalize_request("Add milk to the list"), normalize_request("Add eggs to the list"))

    def test_the_commentary_is_truthful_and_names_the_earlier_request(self):
        self.assertIn("still working on it", promotion_commentary("What's the weather tomorrow?"))
        self.assertIn("Do not guess", promotion_commentary("x"))
        text = result_commentary(LOOKUP, "Sunny.", "What's the weather tomorrow?")
        self.assertIn("earlier request", text)
        self.assertIn("Sunny.", text)
        self.assertIn("Verified result", text)
        self.assertIn("spoken yes", result_commentary(CONFIRM, "Unlock the door?", "unlock the door"))
        self.assertIn("not completed", result_commentary(FAILED, "", "x"))
        self.assertIn("household assistant", result_commentary(LOOKUP, "A.", "x", source="openclaw"))
        self.assertIn("has happened", result_commentary(LOOKUP, "The car left.", "tell me when", deferred=True))
        self.assertEqual(short_request("a " * 60), ("a " * 60)[:60].rsplit(" ", 1)[0] + "…")
        spoken = spoken_result(ACTION, "The lamp is on.", "turn on the lamp")
        self.assertIn("earlier request", spoken)
        self.assertIn("The lamp is on.", spoken)
        self.assertIn("Nothing was done", spoken_result(FAILED, "", "x"))
        self.assertIn("You asked me to tell you", spoken_result(LOOKUP, "The car left.", "tell me when", deferred=True))


class DirectPersonTool(unittest.IsolatedAsyncioTestCase):
    """"Where is X" has a direct Home Assistant tool; the general assistant
    takes 30-120 s for the same question."""

    NAMES = ["intent__HassTurnOn", "intent_script__AlexETA", "script__car_answer_alex_eta",
             "script__car_watch_alex_home", "script__ask_openclaw", "llm__GetDateTime"]

    def test_the_intent_script_is_preferred_and_names_must_match(self):
        for text in ("Where is Alex?", "where's alex", "When will Alex be home?", "Is Alex home",
                     "What time does Alex get home?", "How far away is Alex?", "hey where is Alex right now?"):
            self.assertEqual(person_tool(text, self.NAMES), "intent_script__AlexETA", text)
        without_intent = [n for n in self.NAMES if "intent_script" not in n]
        self.assertEqual(person_tool("Where is Alex?", without_intent), "script__car_answer_alex_eta")
        self.assertIsNone(person_tool("Where is Sam?", self.NAMES), "no tool for that name: no guess")
        self.assertIsNone(person_tool("Where is the remote?", self.NAMES))
        self.assertIsNone(person_tool("Turn on the lamp", self.NAMES))
        self.assertIsNone(person_tool("Where is Alex", []))

    async def test_the_route_answers_from_the_direct_tool_without_the_backend(self):
        tools = TOOLS + [{"type": "function", "name": "intent_script__AlexETA", "description": "ETA.",
                          "parameters": {"type": "object", "properties": {}}}]
        backend = FakeBackend()
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=tools, post=backend.post))
        calls = []

        async def execute(name, args):
            calls.append((name, args))
            raw = {"speech": {"plain": {"speech": "Alex is twelve minutes away."}}}
            return raw, classify_result(raw)
        result = await router.route("Where is Alex?", execute)
        self.assertEqual(result.kind, LOOKUP)
        self.assertEqual(result.source, "ha_direct")
        self.assertIn("twelve minutes", result.text)
        self.assertEqual(calls, [("intent_script__AlexETA", {})])
        self.assertEqual(backend.bodies, [], "no backend call, no general assistant")

    async def test_a_failed_direct_tool_falls_through_to_the_backend(self):
        tools = TOOLS + [{"type": "function", "name": "intent_script__AlexETA", "description": "ETA.",
                          "parameters": {"type": "object", "properties": {}}}]
        backend = FakeBackend(response(message("I could not find out.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=tools, post=backend.post))

        async def execute(name, args):
            raw = {"error": "Error calling tool: no navigation data"}
            return raw, classify_result(raw)
        result = await router.route("Where is Alex?", execute)
        self.assertEqual(len(backend.bodies), 1)
        self.assertEqual(result.timings.get("local_fallthrough"), "error")

    def test_the_backend_is_told_to_prefer_direct_tools(self):
        from app.live_router import ROUTER_BACKEND_INSTRUCTIONS
        from app.openclaw_tool import get_openclaw_tool_definition
        self.assertIn("Prefer a specific tool", ROUTER_BACKEND_INSTRUCTIONS)
        self.assertIn("location or arrival time", get_openclaw_tool_definition()["description"])


class PendingOutcome(unittest.IsolatedAsyncioTestCase):
    def test_a_hand_off_classifies_as_pending_not_done(self):
        for result in ({"pending": True, "task_id": "t1"}, {"background": True, "answer": "Still working"},
                       {"accepted": True, "task_id": "t2"}, json.dumps({"pending": True, "task_id": "t3"})):
            outcome = classify_result(result)
            self.assertTrue(outcome.ok and outcome.pending, result)
            self.assertEqual(outcome.kind, "pending")
        self.assertEqual(classify_result({"pending": True, "task_id": "t1"}).task_id, "t1")
        self.assertFalse(classify_result({"pending": True, "error": "boom"}).pending)
        self.assertFalse(classify_result({"success": True}).pending)
        watch = classify_result({"deferred": True, "task_id": "w1"})
        self.assertTrue(watch.deferred and watch.pending and watch.ok)
        armed = classify_result({"watch": True})
        self.assertTrue(armed.deferred and armed.ok and not armed.pending, "no id: nothing to wait for")

    async def test_the_router_returns_pending_with_truthful_commentary(self):
        tools = TOOLS + [{"type": "function", "name": "ask_openclaw", "description": "Ask.",
                          "parameters": {"type": "object", "properties": {"question": {"type": "string"}}}}]
        backend = FakeBackend(response(call("ask_openclaw", {"question": "flights"}, "c1")),
                              response(message("I've asked; it will report back.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=tools, post=backend.post))

        async def execute(name, args):
            raw = {"pending": True, "task_id": "task-9"}
            return raw, classify_result(raw)
        result = await router.route("Find me flights to London.", execute)
        self.assertEqual(result.kind, PENDING)
        self.assertEqual(result.task_ids, ["task-9"])
        text = result.commentary()
        self.assertIn("still working", text)
        self.assertIn("do not guess", text)
        self.assertNotIn("Verified", text)

    async def test_a_pending_reply_that_claims_success_is_not_repeated(self):
        tools = TOOLS + [{"type": "function", "name": "ask_openclaw", "description": "Ask.",
                          "parameters": {"type": "object", "properties": {"question": {"type": "string"}}}}]
        backend = FakeBackend(response(call("ask_openclaw", {"question": "x"}, "c1")),
                              response(message("Done, the message is sent.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=tools, post=backend.post))

        async def execute(name, args):
            raw = {"pending": True, "task_id": "t"}
            return raw, classify_result(raw)
        result = await router.route("Text the plumber.", execute)
        self.assertEqual(result.kind, PENDING)
        self.assertNotIn("sent", result.commentary())

    async def test_a_watch_tool_makes_a_deferred_result(self):
        tools = TOOLS + [{"type": "function", "name": "script__car_watch_alex_home", "description": "Watch.",
                          "parameters": {"type": "object", "properties": {}}}]
        backend = FakeBackend(response(call("script__car_watch_alex_home", {}, "c1")),
                              response(message("I'll let you know when Alex leaves.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=tools, post=backend.post))

        async def execute(name, args):
            raw = {"success": True}
            return raw, classify_result(raw)
        result = await router.route("Tell me when Alex leaves work.", execute)
        self.assertEqual(result.kind, PENDING)
        self.assertTrue(result.deferred)
        self.assertIn("when it happens", result.commentary())
        self.assertIn("do not say it has happened", result.commentary())


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

def commentaries(h):
    return [m["content"] for m in h.socket.of("session.commentary.append")]


async def until(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(_wait(), timeout)


async def acked(h):
    """The endpoint acknowledges the commentary (a pending append counts as
    work for up to COMMENTARY_ACK_TIMEOUT_S, which is not what these tests
    measure)."""
    h.socket.push({"type": "session.commentary.appended"})
    await asyncio.sleep(0.05)


class NeverCancelled(Patched):
    """A tool that has started finishes, and its result is spoken."""

    async def test_a_new_utterance_during_a_tool_delivers_both_results_and_runs_each_once(self):
        backend = FakeBackend(response(call(*ON, "c1")), response(message("The lamp is on.")))
        async with FirstCallSlow(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp")
            await until(lambda: h.executed)
            await asyncio.sleep(NEW_UTTERANCE_S)
            # The user talks over the running tool: the clock answers locally.
            await h.user_says("What time is it?", start_ms=5000)
            await until(lambda: any("Current date and time" in c for c in commentaries(h)))
            self.assertEqual(h.service.tasks_pending, 1, "the lamp call continues in the background")
            h.release.set()
            await until(lambda: any("earlier request" in c and "lamp is on" in c for c in commentaries(h)), 5.0)
            self.assertEqual(h.executed, [ON, ("llm__GetDateTime", {})], "each tool ran exactly once")
            self.assertEqual(h.service.tasks_pending, 0)

    async def test_an_impatient_repeat_joins_the_running_operation(self):
        backend = FakeBackend(response(call(*ON, "c1")), response(message("The lamp is on.")))
        async with FirstCallSlow(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp")
            await until(lambda: h.executed)
            await asyncio.sleep(0.5)
            await h.user_says("Turn on the Atrium Lamp.", start_ms=5000)  # inside the fragment window
            await asyncio.sleep(0.3)
            self.assertEqual(h.executed, [ON], "the repeat ran nothing")
            self.assertEqual(h.service.router_stats["joined"], 1)
            h.release.set()
            await until(lambda: any("Verified result" in c for c in commentaries(h)))
            await asyncio.sleep(0.3)
            self.assertEqual(h.executed, [ON], "still exactly one execution")
            self.assertEqual(sum("lamp is on" in c for c in commentaries(h)), 1, "spoken once")
            self.assertTrue(h.service._op.done)

    async def test_a_later_repeat_also_joins(self):
        backend = FakeBackend(response(call(*ON, "c1")), response(message("The lamp is on.")))
        async with FirstCallSlow(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp")
            await until(lambda: h.executed)
            await asyncio.sleep(NEW_UTTERANCE_S)
            await h.user_says("Turn on the Atrium Lamp please", start_ms=8000)
            await asyncio.sleep(0.3)
            self.assertEqual(h.executed, [ON])
            self.assertEqual(h.service.router_stats["joined"], 1)
            h.release.set()
            await until(lambda: any("Verified result" in c for c in commentaries(h)))
            self.assertEqual(h.executed, [ON])

    async def test_a_held_confirmation_in_the_background_acts_on_nothing(self):
        """Safety unchanged: a background result that needs a spoken yes only
        asks the question; nothing is done without the yes."""
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Entry Lock"}, "c1")),
                              response(message("Do you want me to unlock the entry lock?")))

        class ConfirmSlow(FirstCallSlow):
            def _record(self, name):
                async def handler(params):
                    self.executed.append((name, dict(params.arguments or {})))
                    if len(self.executed) == 1:
                        await self.release.wait()
                        await params.result_callback({"confirmation_required": True, "confirm_id": "k1"})
                        return
                    await params.result_callback({"success": True})
                return handler

        async with ConfirmSlow(self, backend) as h:
            await h.user_says("Unlock the entry lock")
            await until(lambda: h.executed)
            await asyncio.sleep(NEW_UTTERANCE_S)
            await h.user_says("What time is it?", start_ms=5000)
            await until(lambda: any("Current date and time" in c for c in commentaries(h)))
            h.release.set()
            await until(lambda: any("spoken yes" in c for c in commentaries(h)), 5.0)
            self.assertEqual(len(h.executed), 2, "the lock call ran once, the clock once; nothing else")
            self.assertTrue(any("earlier request" in c and "unlock" in c.lower() for c in commentaries(h)))


class PromotedToBackground(Patched):
    """After the soft budget the voice says what it is doing, the gate opens,
    and the conversation goes on."""

    async def test_the_voice_is_told_and_the_gate_opens(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3)
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await asyncio.sleep(0.15)
            self.assertEqual(h.service._gate, "pre")
            await h.wait_commentary(count=1, timeout=2.0)
            self.assertIn("still working on it", commentaries(h)[0])
            self.assertIn("will tell them", commentaries(h)[0])
            self.assertEqual(h.service._gate, "open", "the voice may converse while the work goes on")
            self.assertEqual(h.service.router_stats["promoted"], 1)
            # Whatever the voice says now is heard.
            h.voice_says("Still on it, I'll let you know.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
            backend.gate.set()
            await until(lambda: any("sunny" in c for c in commentaries(h)))

    async def test_a_guess_held_before_the_promotion_is_dropped(self):
        backend = FakeBackend(response(call(*ON, "c1")), response(message("The lamp is on.")))
        async with FirstCallSlow(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3)
            await h.user_says("Turn on the Atrium Lamp")
            await asyncio.sleep(0.1)
            h.voice_says("Done, it's on.", ms=400)
            await h.wait_commentary(count=1, timeout=2.0)
            await asyncio.sleep(0.1)
            self.assertEqual(h.heard(), b"", "the guess never reached the speaker")
            h.release.set()
            await until(lambda: any("lamp is on" in c for c in commentaries(h)))

    async def test_the_conversation_continues_and_the_result_follows(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3)
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await h.wait_commentary(count=1, timeout=2.0)
            await asyncio.sleep(NEW_UTTERANCE_S)
            await h.user_says("What time is it?", start_ms=6000)
            await until(lambda: any("Current date and time" in c for c in commentaries(h)))
            self.assertEqual(h.executed, [("llm__GetDateTime", {})], "the clock question is answered meanwhile")
            backend.gate.set()
            await until(lambda: any("sunny" in c and "earlier request" in c for c in commentaries(h)), 5.0)
            self.assertEqual(h.service.tasks_pending, 0)

    async def test_progress_is_mentioned_only_when_the_user_is_not_talking(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.1, progress_every_s=0.3, progress_max=2,
                                               progress_quiet_s=0.0)
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await h.wait_commentary(count=1, timeout=2.0)
            await asyncio.sleep(NEW_UTTERANCE_S)
            # The user talks for a while (an open transcript).
            for i in range(40):
                h.socket.push({"type": "session.input_transcript.delta",
                               "delta": " and" if i else "also", "start_ms": 6000 + i})
                await asyncio.sleep(0.02)
            self.assertFalse(any("still working on the earlier request" in c for c in commentaries(h)),
                             "no progress line over the user's speech")
            await until(lambda: any("still working on the earlier request" in c for c in commentaries(h)), 3.0)
            backend.gate.set()
            await until(lambda: any("sunny" in c for c in commentaries(h)))
            progress = [c for c in commentaries(h) if "still working on the earlier request" in c]
            self.assertLessEqual(len(progress), 2)

    async def test_a_late_delegation_for_a_background_task_runs_nothing(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.2)
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await h.wait_commentary(count=1, timeout=2.0)
            await asyncio.sleep(NEW_UTTERANCE_S)
            await h.user_says("um", start_ms=6000)  # not routed: no foreground op
            await asyncio.sleep(0.2)
            h.socket.push({"type": "session.delegation.created",
                           "delegation": {"id": "item_late", "target": "client"}})
            await until(lambda: any(m["delegation_id"] == "item_late"
                                    for m in h.socket.of("session.commentary.append")))
            answer = next(m for m in h.socket.of("session.commentary.append") if m["delegation_id"] == "item_late")
            self.assertIn("still working", answer["content"])
            self.assertNotIn("say it again", answer["content"])
            backend.gate.set()
            await until(lambda: any("sunny" in c for c in commentaries(h)))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class CostRule(Patched):
    """COST IS A DESIGN REQUIREMENT. The real session gate on a fake clock:

    * a slow lookup inside a conversation keeps the session, but only up to
      the hard bound; past it the ordinary idle bound closes the session while
      the task still runs, and the result is delivered afterwards without that
      session (regression: fails if the session can stay open past the bound);
    * a deferred watch never holds the session at all;
    * every session opened for a result goes through the gate (spend ledger,
      daily caps) and can be refused.
    """

    POLICY = LiveSessionPolicy(idle_s=5.0, followup_s=5.0, max_silent_s=20.0,
                               max_session_s=300.0, tick_s=0.005)

    async def _gate_for(self, h):
        clock = Clock()
        gate = LiveSessionGate(h.service, device_id="kitchen", policy=self.POLICY,
                               ledger=LiveSpendLedger(path=None), monotonic=clock,
                               wall_clock=lambda: 1_700_000_000.0 + clock.t)
        h.service.on_user_utterance = gate.note_user_utterance
        h.service.on_audible_output = gate.note_audible_output
        h.service.on_backend_activity = gate.note_backend_activity
        h.service.on_reply_finished = gate.note_reply_finished
        original = h.service.close_session
        closes = []

        async def close_session(reason=""):
            closes.append(reason)
            h.socket.push({"type": "session.closed", "reason": "client", "usage": {"seconds": 3}})
            await original(reason)
        h.service.close_session = close_session
        return gate, clock, closes

    async def _advance(self, clock, seconds, step=1.0):
        remaining = seconds
        while remaining > 0:
            clock.t += min(step, remaining)
            remaining -= step
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    async def test_a_lookup_keeps_the_session_only_up_to_the_hard_bound(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3, hold_session_s=2.0, order=(ANNOUNCE,))
            announced = []

            async def announce(text, device_id):
                announced.append((text, device_id))
                return True
            h.service.on_task_announce = announce
            gate, clock, closes = await self._gate_for(h)
            self.assertTrue(await gate.open("wake"))
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await h.wait_commentary(count=1, timeout=2.0)  # promoted: "still working, I'll report back"
            await acked(h)
            self.assertTrue(h.service.router_pending, "within the bound the conversation keeps its session")
            await self._advance(clock, 12)
            self.assertEqual(gate.state, OPEN, "not closed under a lookup inside the bound")
            self.assertEqual(closes, [])
            # The hard bound passes with the task still running.
            await asyncio.sleep(2.2)
            self.assertFalse(h.service.router_pending, "past the bound nothing holds the session")
            self.assertFalse(h.service.backend_work_active)
            await self._advance(clock, 12)
            await until(lambda: gate.state == CLOSED, timeout=5.0)
            self.assertEqual(len(closes), 1, "closed on an ordinary bound, task still running")
            self.assertIn(closes[0], ("idle", "silent", "follow-up quiet"))
            self.assertFalse(backend.gate.is_set())
            self.assertEqual(h.service.tasks_pending, 1)
            backend.gate.set()
            await until(lambda: announced, timeout=5.0)
            self.assertIn("sunny", announced[0][0])
            self.assertIn("earlier request", announced[0][0])
            self.assertEqual(announced[0][1], "kitchen")
            self.assertEqual(gate.state, CLOSED, "the announcement opened no session")
            self.assertEqual(h.service.tasks_pending, 0)
            await gate.shutdown()

    async def test_a_deferred_watch_never_holds_the_session(self):
        watch = {"type": "function", "name": "script__car_watch_alex_home", "description": "Watch.",
                 "parameters": {"type": "object", "properties": {}}}
        backend = FakeBackend(response(call("script__car_watch_alex_home", {}, "c1")),
                              response(message("I'll let you know.")))
        with patch("tests.test_live_router.TOOLS", TOOLS + [watch]):
            async with RouterHarness(self, backend) as h:
                async def armed(params):
                    h.executed.append(("watch", {}))
                    await params.result_callback({"deferred": True, "task_id": "watch-1"})
                h.service.register_function("script__car_watch_alex_home", armed)
                h.service.task_policy = TaskPolicy(promote_s=0.3, order=(ANNOUNCE,), deferred_order=(ANNOUNCE,))
                announced = []

                async def announce(text, device_id):
                    announced.append(text)
                    return True
                h.service.on_task_announce = announce
                gate, clock, closes = await self._gate_for(h)
                self.assertTrue(await gate.open("wake"))
                await h.user_says("Tell me when Alex leaves work.")
                await h.wait_commentary(count=1, timeout=3.0)
                await acked(h)
                self.assertIn("when it happens", commentaries(h)[0])
                self.assertEqual(h.service.tasks_pending, 1)
                self.assertFalse(h.service.router_pending, "a watch holds no session, for hours")
                await self._advance(clock, 12)
                await until(lambda: gate.state == CLOSED, timeout=5.0)
                self.assertEqual(len(closes), 1)
                self.assertEqual(h.service.tasks_pending, 1, "still waiting for the event")
                # Hours later the event fires: told by voice on the same device.
                self.assertTrue(await live_tasks.deliver_external_result("watch-1", "Alex just left work."))
                await until(lambda: announced, timeout=5.0)
                self.assertIn("You asked me to tell you", announced[0])
                self.assertIn("just left work", announced[0])
                self.assertEqual(gate.state, CLOSED)
                self.assertEqual(h.service.tasks_pending, 0)
                await gate.shutdown()

    async def test_a_reopen_goes_through_the_gate_and_delivers_into_the_new_session(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3, hold_session_s=1.0, order=(REOPEN, ANNOUNCE))
            gate, clock, closes = await self._gate_for(h)
            opens = []

            async def reopen():
                opens.append(1)
                ok = await gate.open("background task result")
                if ok:
                    await h.start_session(f"sess_{len(opens) + 1}")
                return ok
            h.service.on_task_reopen = reopen
            self.assertTrue(await gate.open("wake"))
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await h.wait_commentary(count=1, timeout=2.0)
            await acked(h)
            await asyncio.sleep(1.2)
            await self._advance(clock, 12)
            await until(lambda: gate.state == CLOSED, timeout=5.0)
            first_socket = h.socket
            backend.gate.set()
            await until(lambda: opens and h.socket is not first_socket and any(
                "sunny" in m["content"] for m in h.socket.of("session.commentary.append")), timeout=5.0)
            self.assertEqual(opens, [1], "exactly one short session, opened through the gate")
            self.assertEqual(gate.opens, 2)
            delivered = next(m for m in h.socket.of("session.commentary.append") if "sunny" in m["content"])
            self.assertIn("earlier request", delivered["content"])
            self.assertEqual(h.service._gate, "open", "the result is audible in the new session")
            await gate.shutdown()

    async def test_the_spend_gate_can_refuse_the_reopen_and_the_next_route_is_used(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3, order=(REOPEN, ANNOUNCE))
            announced = []

            async def announce(text, device_id):
                announced.append(text)
                return True

            async def refused():
                return False  # what LiveSessionGate.open returns past the daily cap
            h.service.on_task_announce = announce
            h.service.on_task_reopen = refused
            await h.user_says("Ask the assistant what the weather is tomorrow.")
            await h.wait_commentary(count=1, timeout=2.0)
            await asyncio.wait_for(h.service.close_session("idle"), 10)
            backend.gate.set()
            await until(lambda: announced, timeout=5.0)
            self.assertIn("sunny", announced[0])


class FallbackOrder(Patched):
    async def _finished_after_close(self, h, backend):
        await h.user_says(self.request)
        await h.wait_commentary(count=1, timeout=2.0)
        await asyncio.wait_for(h.service.close_session("idle"), 10)
        backend.gate.set()

    request = "Ask the assistant what the weather is tomorrow."

    async def test_a_text_is_first_only_when_the_user_asked_for_one(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3, order=(ANNOUNCE, IMESSAGE))
            announced, texted = [], []

            async def announce(text, device_id):
                announced.append(text)
                return True

            async def text(message_text):
                texted.append(message_text)
                return True
            h.service.on_task_announce = announce
            h.service.on_task_message = text
            self.request = "Text me what the weather is tomorrow."
            await self._finished_after_close(h, backend)
            await until(lambda: texted, timeout=5.0)
            self.assertIn("sunny", texted[0])
            self.assertEqual(announced, [], "they asked for a text")

    async def test_voice_first_and_a_text_only_when_the_device_cannot_be_reached(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3, order=(ANNOUNCE, IMESSAGE))
            tried = []

            async def announce(text, device_id):
                tried.append(ANNOUNCE)
                return False  # the device is not connected

            async def text(message_text):
                tried.append(IMESSAGE)
                return True
            h.service.on_task_announce = announce
            h.service.on_task_message = text
            await self._finished_after_close(h, backend)
            await until(lambda: IMESSAGE in tried, timeout=5.0)
            self.assertEqual(tried, [ANNOUNCE, IMESSAGE])
            self.assertEqual(h.service.router_stats["delivered"], {IMESSAGE: 1})

    async def test_the_configured_order_is_followed(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3, order=(IMESSAGE, ANNOUNCE))
            tried = []

            async def announce(text, device_id):
                tried.append(ANNOUNCE)
                return True

            async def text(message_text):
                tried.append(IMESSAGE)
                return False
            h.service.on_task_announce = announce
            h.service.on_task_message = text
            await self._finished_after_close(h, backend)
            await until(lambda: ANNOUNCE in tried, timeout=5.0)
            self.assertEqual(tried, [IMESSAGE, ANNOUNCE])

    async def test_a_result_with_no_route_waits_for_the_next_session(self):
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            h.service.task_policy = TaskPolicy(promote_s=0.3)
            await self._finished_after_close(h, backend)
            await until(lambda: any(t.done for t in h.service._tasks.values()), timeout=3.0)
            await asyncio.sleep(0.2)
            self.assertEqual(h.service.tasks_pending, 1, "kept, not dropped")
            await h.open_session()
            await h.start_session("sess_2")
            h.service.turn_timeline.begin("w2")
            await h.user_says("What time is it?", start_ms=1000)
            await until(lambda: any("sunny" in c and "earlier request" in c for c in commentaries(h)), timeout=5.0)
            clock_first = next(i for i, c in enumerate(commentaries(h)) if "Current date and time" in c)
            task_after = next(i for i, c in enumerate(commentaries(h)) if "sunny" in c)
            self.assertLess(clock_first, task_after, "the user's own request is answered first")

    async def test_quiet_hours_hold_a_deferred_result(self):
        watch = {"type": "function", "name": "script__car_watch_alex_home", "description": "Watch.",
                 "parameters": {"type": "object", "properties": {}}}
        backend = FakeBackend(response(call("script__car_watch_alex_home", {}, "c1")),
                              response(message("I'll let you know.")))
        now = datetime.now()
        quiet = f"{(now - timedelta(hours=1)).hour:02d}:00-{(now + timedelta(hours=2)).hour:02d}:00"
        with patch("tests.test_live_router.TOOLS", TOOLS + [watch]):
            async with RouterHarness(self, backend) as h:
                async def armed(params):
                    await params.result_callback({"deferred": True, "task_id": "watch-2"})
                h.service.register_function("script__car_watch_alex_home", armed)
                h.service.task_policy = TaskPolicy(promote_s=0.3, deferred_order=(ANNOUNCE, IMESSAGE),
                                                   quiet_hours=quiet)
                announced = []

                async def announce(text, device_id):
                    announced.append(text)
                    return True
                h.service.on_task_announce = announce
                await h.user_says("Tell me when Alex leaves work.")
                await h.wait_commentary(count=1, timeout=3.0)
                await asyncio.wait_for(h.service.close_session("idle"), 10)
                self.assertTrue(await live_tasks.deliver_external_result("watch-2", "Alex just left work."))
                await asyncio.sleep(0.5)
                self.assertEqual(announced, [], "nobody is woken up at night")
                self.assertEqual(h.service.tasks_pending, 1, "told when quiet hours end")
                # Quiet hours end: the next flush announces it.
                h.service.task_policy = TaskPolicy(promote_s=0.3, deferred_order=(ANNOUNCE, IMESSAGE))
                h.service._task_flush_soon("quiet hours ended")
                await until(lambda: announced, timeout=3.0)
                self.assertIn("just left work", announced[0])


# ---------------------------------------------------------------------------
# the agent hand-off
# ---------------------------------------------------------------------------

ASK = {"type": "function", "name": "ask_openclaw", "description": "Ask.",
       "parameters": {"type": "object", "properties": {"question": {"type": "string"}}}}


class AskHarness(RouterHarness):
    def __init__(self, test, backend, reply):
        super().__init__(test, backend)
        self.reply = reply

    async def __aenter__(self):
        await super().__aenter__()

        async def ask(params):
            self.executed.append(("ask_openclaw", dict(params.arguments or {})))
            await params.result_callback(self.reply)
        self.service.register_function("ask_openclaw", ask)
        return self


class AgentHandOff(Patched):
    def setUp(self):
        super().setUp()
        self._tools = patch("tests.test_live_router.TOOLS", TOOLS + [ASK])
        self._tools.start()

    def tearDown(self):
        self._tools.stop()
        super().tearDown()

    async def test_an_accepted_request_is_pending_and_its_result_lands_later(self):
        backend = FakeBackend(response(call("ask_openclaw", {"question": "flights"}, "c1")),
                              response(message("I've asked; it will report back.")))
        async with AskHarness(self, backend, {"accepted": True, "task_id": "task-9"}) as h:
            await h.user_says("Find me flights to London in October.")
            await h.wait_commentary(count=1, timeout=3.0)
            first = commentaries(h)[0]
            self.assertIn("still working", first)
            self.assertNotIn("Verified", first)
            self.assertEqual(h.service.tasks_pending, 1)
            await acked(h)
            self.assertTrue(h.service.router_pending, "a lookup hand-off keeps the conversation (within the bound)")
            self.assertTrue(await live_tasks.deliver_external_result("task-9", "Flights start at 300 dollars."))
            await until(lambda: any("300 dollars" in c for c in commentaries(h)))
            landed = next(c for c in commentaries(h) if "300 dollars" in c)
            self.assertIn("household assistant", landed)
            self.assertIn("earlier request", landed)
            self.assertEqual(h.service.tasks_pending, 0)
            await acked(h)
            self.assertFalse(h.service.router_pending)
            self.assertFalse(await live_tasks.deliver_external_result("task-9", "again"), "delivered once")

    async def test_a_repeat_of_a_pending_hand_off_asks_nothing_again(self):
        backend = FakeBackend(response(call("ask_openclaw", {"question": "flights"}, "c1")),
                              response(message("I've asked; it will report back.")))
        async with AskHarness(self, backend, {"accepted": True, "task_id": "task-9"}) as h:
            await h.user_says("Find me flights to London in October.")
            await h.wait_commentary(count=1, timeout=3.0)
            await asyncio.sleep(NEW_UTTERANCE_S)
            await h.user_says("Find me flights to London in October", start_ms=8000)
            await h.wait_commentary(count=2, timeout=3.0)
            self.assertIn("already working on", commentaries(h)[1])
            self.assertEqual(len(h.executed), 1, "asked once")
            self.assertEqual(len(backend.bodies), 2, "no second backend route either")
            self.assertTrue(await live_tasks.deliver_external_result("task-9", "Three hundred dollars."))
            await until(lambda: any("Three hundred" in c for c in commentaries(h)))

    async def test_a_failed_hand_off_is_reported_as_not_done(self):
        backend = FakeBackend(response(call("ask_openclaw", {"question": "x"}, "c1")),
                              response(message("I've asked.")))
        async with AskHarness(self, backend, {"background": True, "answer": "Still working on that."}) as h:
            await h.user_says("Text the plumber about the leak.")
            await h.wait_commentary(count=1, timeout=3.0)
            task_id = next(t.external_id for t in h.service._tasks.values())
            self.assertTrue(task_id.startswith("rcall"), "the tool-call id names the task")
            self.assertTrue(await live_tasks.deliver_external_result(task_id, "Could not reach Messages.", ok=False))
            await until(lambda: any("not completed" in c for c in commentaries(h)))
            self.assertIn("do not say it was done", commentaries(h)[-1])


class TaskResultEndpoint(unittest.IsolatedAsyncioTestCase):
    """POST /task-result and an /announce that names a pending task complete
    it; nothing is spoken on the bare lane."""

    async def asyncSetUp(self):
        announce_http._recent.clear()
        announce_http._pending.clear()
        self.spoken = []
        self.claimed = []

        async def announcer(message, device_id):
            self.spoken.append(message)
            return True

        async def sink(task_id, text, ok):
            if task_id == "known":
                self.claimed.append((text, ok))
                return True
            return False
        live_tasks.register_sink(sink)
        self.sink = sink
        self.runner = await announce_http.start_announce_server(
            0, "tok", announcer, lambda device_id: True, task_sink=live_tasks.deliver_external_result)
        site = next(iter(self.runner.sites))
        self.port = site._server.sockets[0].getsockname()[1]
        self.session = ClientSession()

    async def asyncTearDown(self):
        live_tasks.unregister_sink(self.sink)
        await self.session.close()
        await self.runner.cleanup()

    def post(self, path, payload, token="tok"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.session.post(f"http://127.0.0.1:{self.port}{path}", json=payload, headers=headers)

    async def test_task_result_completes_a_known_task_and_refuses_unknown_ones(self):
        async with self.post("/task-result", {"task_id": "known", "answer": "Flights from 300.",
                                              "ok": True}) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())["status"], "accepted")
        self.assertEqual(self.claimed, [("Flights from 300.", True)])
        async with self.post("/task-result", {"task_id": "known", "answer": "Flights from 300."}) as r:
            self.assertEqual((await r.json())["status"], "duplicate_suppressed")
        async with self.post("/task-result", {"task_id": "nobody", "answer": "x"}) as r:
            self.assertEqual(r.status, 404)
        async with self.post("/task-result", {"task_id": "known", "answer": "x"}, token="bad") as r:
            self.assertEqual(r.status, 401)
        async with self.post("/task-result", {"task_id": "k2", "error": "failed"}) as r:
            self.assertEqual(r.status, 404, "an error for an unknown task is still unknown")
        self.assertEqual(self.spoken, [], "nothing went to the announcement lane")

    async def test_a_result_without_ok_is_not_a_verified_result(self):
        async with self.post("/task-result", {"task_id": "known", "answer": "Bridge error: timeout"}) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.claimed, [("Bridge error: timeout", False)])

    async def test_a_legacy_announce_with_the_request_id_lands_in_the_conversation(self):
        async with self.post("/announce", {"message": "Flights from 300.", "announcement_id": "known"}) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())["status"], "accepted")
        # Longtask review (ii): no "ok" is not ok, so bridge text is never
        # framed as a verified result.
        self.assertEqual(self.claimed, [("Flights from 300.", False)])
        self.assertEqual(self.spoken, [])
        async with self.post("/announce", {"message": "Dinner is ready.", "announcement_id": "other"}) as r:
            self.assertEqual((await r.json())["status"], "announced")
        self.assertEqual(self.spoken, ["Dinner is ready."], "an ordinary announcement still plays")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeClient:
    posted = []
    reply = {}

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        FakeClient.posted.append((url, json))
        return FakeResponse(FakeClient.reply)


class Params:
    def __init__(self, arguments, call_id="rcall_1"):
        self.arguments = arguments
        self.tool_call_id = call_id
        self.results = []

    async def result_callback(self, result):
        self.results.append(result)


class Registry:
    def __init__(self):
        self.handlers = {}

    def register_function(self, name, handler):
        self.handlers[name] = handler


class OpenClawAsyncPath(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeClient.posted = []
        self._env = patch.dict(os.environ, {"OPENCLAW_URL": "http://bridge/ask-x",
                                            "OPENCLAW_CALLBACK_URL": "http://ha:8090/task-result",
                                            "INSTANCE_NAME": "Workshop"})
        self._env.start()
        self._client = patch("app.openclaw_tool.httpx.AsyncClient", FakeClient)
        self._client.start()

    def tearDown(self):
        self._client.stop()
        self._env.stop()

    async def _ask(self, reply):
        from app.openclaw_tool import register_openclaw_tool
        FakeClient.reply = reply
        registry = Registry()
        register_openclaw_tool(registry, "workshop-device")
        params = Params({"question": "Find flights"}, call_id="rcall_42")
        await registry.handlers["ask_openclaw"](params)
        return FakeClient.posted[-1][1], params.results[-1]

    async def test_the_request_names_the_task_and_the_callback(self):
        payload, result = await self._ask({"answer": "Direct flights start at 300."})
        self.assertEqual(payload["task_id"], "rcall_42")
        self.assertEqual(payload["request_id"], "rcall_42")
        self.assertEqual(payload["callback_url"], "http://ha:8090/task-result")
        self.assertEqual(payload["device_id"], "workshop-device")
        self.assertEqual(payload["room"], "workshop")
        self.assertEqual(result, {"answer": "Direct flights start at 300."})

    async def test_an_accepted_reply_is_a_pending_result(self):
        payload, result = await self._ask({"accepted": True, "task_id": "bridge-7", "eta_s": 90})
        self.assertTrue(result["pending"])
        self.assertEqual(result["task_id"], "bridge-7")
        self.assertEqual(result["eta_s"], 90)
        self.assertIn("report back", result["what_to_do"])
        self.assertTrue(classify_result(result).pending)

    async def test_the_legacy_background_reply_is_pending_under_the_call_id(self):
        payload, result = await self._ask({"answer": "(Still working on that — I'll tell you.)",
                                           "background": True, "retry": False})
        self.assertTrue(result["pending"])
        self.assertEqual(result["task_id"], "rcall_42")
        self.assertIn("Still working", result["answer"])

    async def test_a_text_goes_through_the_bridge_notification_contract(self):
        from app.openclaw_tool import send_task_message
        FakeClient.reply = {"delivered": True}
        self.assertTrue(await send_task_message("Flights from 300.", "owner"))
        self.assertEqual(FakeClient.posted[-1][1], {"notification_destination": "owner",
                                                    "notification_message": "Flights from 300."})
        FakeClient.reply = {"delivered": False, "error": "unverified"}
        self.assertFalse(await send_task_message("x", "owner"))
        self.assertFalse(await send_task_message("x", ""), "no destination, no text")




class OvertakenConfirmationInTheBackground(unittest.IsolatedAsyncioTestCase):
    """Longtask review B1 (v35 merge): the twelfth freeze's eleventh-review B9
    regression, against the task list. An unlock whose op left the foreground
    (talked over at 2.6 s, or promoted at the 3.5 s soft budget) never holds an
    action for a yes: "Yes." after the newer answer runs nothing, and the voice
    is told the unlock was not done. Real ActionGate and confirm handler."""

    async def _case(self, wait_s):
        from app.action_gate import ActionGate, EntityDirectory
        from app.main import Application
        from tests.test_live_router import LOCK_OFF, STATES, OvertakenConfirmation

        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = OvertakenConfirmation.Backend()
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 30.0):
            async with RouterHarness(self, backend, gate=gate) as h:
                app = object.__new__(Application)
                app.action_gate = gate
                h.service.register_function("confirm_action",
                                            Application._create_confirm_handler(app, h.service))
                await h.user_says("Unlock the Entry Lock.")
                for _ in range(300):
                    if gate._pending:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(gate._pending, "the unlock is held for a yes")
                await asyncio.sleep(wait_s)
                await h.user_says("What's on the list?", start_ms=8000)
                await asyncio.sleep(0.05)
                backend.late.set()
                await h.wait_commentary(count=2, timeout=5)
                await asyncio.sleep(0.2)
                h.socket.push({"type": "session.commentary.appended"})
                await asyncio.sleep(0.05)
                h.voice_says("Milk and eggs.", ms=300)
                await asyncio.sleep(0.4)
                await h.user_says("Yes.", start_ms=9500)
                await asyncio.sleep(4.0)
                self.assertNotIn(LOCK_OFF, h.executed, "the unlock never ran: its question went unheard")
                self.assertEqual(gate._pending, {}, "the held action was withdrawn")
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                self.assertTrue(any("was not done" in c for c in contents), contents)
                self.assertFalse(any("Ask the user this yes/no question" in c for c in contents),
                                 "a background confirmation is never delivered as the question")

    async def test_superseded_at_2_6_s(self):
        await self._case(2.6)

    async def test_promoted_at_4_2_s(self):
        await self._case(4.2)


if __name__ == "__main__":
    unittest.main()
