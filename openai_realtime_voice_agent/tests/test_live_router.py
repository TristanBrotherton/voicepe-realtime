"""Router mode (0.18.26): the app routes every request; the voice speaks only
verified results.

Each class pins one behaviour of the 0.18.26 design against the real
``OpenAILiveLLMService`` in a real pipeline on a fake socket, with a scripted
Responses backend and a scripted Home Assistant intent recognizer:

* routing happens when the user's transcript closes, with or without the
  voice delegating, and exactly once per utterance;
* nothing the voice says before a verified result is heard unless it is a
  plain acknowledgement; a guess at the outcome is dropped unheard;
* no false success: a tool-less claim, a failed tool and a pending
  confirmation are never turned into "done";
* an action never runs twice: identical calls in one step, a delegation for
  the same utterance and a re-routed continuation all reach the house once;
* risky actions need a spoken yes, and the local fast path never touches them;
* every tool is offered to the backend in a stable, cacheable request;
* reply audio streams as it arrives once the result is in;
* the session is not idle while a result is pending; the static
  acknowledgement is a logged fallback only.
"""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from pipecat.frames.frames import TTSAudioRawFrame

import app.live_service as live_service_module
from app.action_gate import ActionGate, EntityDirectory
from app.live_router import (
    ACTION, CHAT_KIND, CONFIRM, FAILED, LOOKUP, NOTHING, REPLY,
    BackendRouter, HaIntentRecognizer, LocalMatch, TurnRouter,
    claims_success, is_acknowledgement, local_tool_call,
)

from tests import live_fixtures as fx
from tests.live_harness import LiveHarness

TOOLS = [
    {"type": "function", "name": "intent__HassTurnOn", "description": "Turn on.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"type": "function", "name": "intent__HassTurnOff", "description": "Turn off.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"type": "function", "name": "todo__get_items", "description": "List items.",
     "parameters": {"type": "object", "properties": {"todo_list": {"type": "string"}}}},
    {"type": "function", "name": "llm__GetDateTime", "description": "Clock.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "confirm_action", "description": "Confirm.",
     "parameters": {"type": "object", "properties": {"confirm_id": {"type": "string"}}}},
]
STATES = [
    {"entity_id": "lock.front_door", "attributes": {"friendly_name": "Entry Lock"}},
    {"entity_id": "light.atrium", "attributes": {"friendly_name": "Atrium Lamp"}},
]


def b64(pcm):
    return base64.b64encode(pcm).decode("ascii")


def call(name, args, call_id):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}


def message(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def response(*output, cached=0):
    return {"output": list(output), "service_tier": "priority",
            "usage": {"input_tokens": 4000, "input_tokens_details": {"cached_tokens": cached},
                      "output_tokens": 20}}


class FakeBackend:
    """A scripted Responses endpoint. ``gate`` holds the first answer back."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def post(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        await self.gate.wait()
        if not self.responses:
            return response(message("CHAT"))
        nxt = self.responses.pop(0)
        return nxt(body) if callable(nxt) else nxt


class FakeRecognizer:
    available = True

    def __init__(self, match=None):
        self.match = match
        self.sentences = []
        self.device_ids = []

    async def recognize(self, text, device_id=None):
        self.sentences.append(text)
        self.device_ids.append(device_id)
        return self.match


class RouterHarness(LiveHarness):
    def __init__(self, test, backend, recognizer=None, gate=None):
        router = TurnRouter(BackendRouter("sk-test", model="gpt-6-luna", instructions="Backend.",
                                          tools=TOOLS, post=backend.post),
                            recognizer=recognizer, context=lambda: "Current local date and time: noon.")
        super().__init__(test, router=router, tools=TOOLS,
                         instructions=lambda: "Be brief. Current local date and time: noon.")
        self.backend = backend
        self.action_gate_obj = gate

    async def __aenter__(self):
        await super().__aenter__()
        self.executed = []
        self.service.action_gate = self.action_gate_obj
        self.service.spoken_prompts = None
        self.service.turn_timeline.begin("w1")
        for tool in TOOLS:
            if tool["name"] != "confirm_action":
                self.service.register_function(tool["name"], self._record(tool["name"]))
        await self.start_session()
        return self

    def _record(self, name):
        async def handler(params):
            self.executed.append((name, dict(params.arguments or {})))
            if name.endswith("get_items"):
                await params.result_callback({"items": ["milk", "eggs"]})
            elif name.endswith("GetDateTime"):
                await params.result_callback({"time": "12:00"})
            else:
                await params.result_callback({"success": True, "speech": "Turned on"})
        return handler

    async def user_says(self, text, start_ms=1000):
        self.socket.push({"type": "session.input_transcript.delta", "delta": text,
                          "start_ms": start_ms, "end_ms": start_ms + 800})

    async def wait_commentary(self, count=1, timeout=3.0):
        await self.socket.wait_for_sent("session.commentary.append", count=count, timeout=timeout)
        return self.socket.of("session.commentary.append")

    def voice_says(self, text, ms=200):
        self.socket.push({"type": "session.output_transcript.delta", "delta": text})
        self.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(ms))})

    def heard(self):
        return b"".join(f.audio for f in self.sink.of(TTSAudioRawFrame))


async def heard(h, timeout=5.0):
    """The voice speaks the delivered background result to the end. Since
    0.19.2 a result spoken from the task list is finished only once heard
    (audible output, then quiet), so tests that look at ``tasks_pending`` after
    a session delivery let the voice speak first."""
    import time as _time
    with patch.object(live_service_module, "TASK_HEARD_MIN_S", 0.3):
        h.voice_says("Here it is.", ms=400)
        await asyncio.sleep(0.35)
        h.voice_says(" Done.", ms=400)
        deadline = _time.monotonic() + timeout
        while h.service.tasks_pending and _time.monotonic() < deadline:
            await asyncio.sleep(0.05)


def fast():
    return [patch.object(live_service_module, "USER_TURN_GAP_S", 0.05),
            patch.object(live_service_module, "ACK_LAG_S", 0.05),
            patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 5.0)]


class Patched(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._patches = fast()
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class RoutingAtTranscript(Patched):
    async def test_the_app_routes_a_command_the_voice_never_delegated(self):
        backend = FakeBackend(
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
            response(message("The Atrium Lamp is on."), cached=3900),
        )
        async with RouterHarness(self, backend) as h:
            start = h.socket.sent[0]["session"]
            self.assertEqual(start["delegation"], {"type": "client"},
                             "the voice gets no backend of its own")
            await h.user_says("Turn on the Atrium Lamp.")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [("intent__HassTurnOn", {"name": "Atrium Lamp"})])
            self.assertIn("Verified result", sent[0]["content"])
            self.assertIn("The Atrium Lamp is on.", sent[0]["content"])
            self.assertIsNone(sent[0]["delegation_id"])
            self.assertEqual(h.socket.of("response.create"), [],
                             "nothing waits on the voice to delegate")
            self.assertEqual(backend.bodies[0]["input"][-1],
                             {"role": "user", "content": "Turn on the Atrium Lamp."})

    async def test_a_delegation_attaches_to_the_routed_request_and_runs_nothing(self):
        backend = FakeBackend(
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
            response(message("The Atrium Lamp is on.")),
        )
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp.")
            await asyncio.sleep(0.15)
            h.socket.push({"type": "session.delegation.created",
                           "delegation": {"id": "item_d1", "target": "client"}})
            await asyncio.sleep(0.05)
            backend.gate.set()
            sent = await h.wait_commentary()
            self.assertEqual(sent[0]["delegation_id"], "item_d1",
                             "the delegation is answered with the router's verified result")
            # The voice delegates again for the same request: answered, not re-run.
            h.socket.push({"type": "session.delegation.created",
                           "delegation": {"id": "item_d2", "target": "client"}})
            sent = await h.wait_commentary(count=2)
            self.assertIn("already been answered", sent[1]["content"])
            self.assertEqual(len(h.executed), 1, "one utterance, one action")

    async def test_a_delegation_before_the_transcript_closes_routes_once(self):
        backend = FakeBackend(response(call("todo__get_items", {"todo_list": "shopping"}, "c1")),
                              response(message("Milk and eggs.")))
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the shopping list?")
            await asyncio.sleep(0.01)
            h.socket.push({"type": "session.delegation.created",
                           "delegation": {"id": "item_d1", "target": "client"}})
            sent = await h.wait_commentary()
            await asyncio.sleep(0.2)
            self.assertEqual(len(h.socket.of("session.commentary.append")), 1)
            self.assertEqual(len(backend.bodies), 2, "one route: one call, one continuation")
            self.assertEqual(sent[0]["delegation_id"], "item_d1")

    async def test_a_local_intent_match_runs_one_tool_and_no_model(self):
        recognizer = FakeRecognizer(LocalMatch("HassTurnOn", {"name": "Atrium Lamp"}, ["light"], 1))
        backend = FakeBackend()
        async with RouterHarness(self, backend, recognizer=recognizer) as h:
            await h.user_says("Turn on the Atrium Lamp.")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [("intent__HassTurnOn", {"name": "Atrium Lamp"})])
            self.assertEqual(backend.bodies, [], "a confident local match needs no model call")
            self.assertIn("Turned on Atrium Lamp.", sent[0]["content"])

    async def test_an_accidental_wake_fragment_is_not_routed_and_closes_silently(self):
        backend = FakeBackend()
        with patch.object(live_service_module, "ROUTER_SILENT_CLOSE_S", 0.2):
            async with RouterHarness(self, backend) as h:
                silent = []
                h.service.on_silent_response = lambda: silent.append(1)
                await h.user_says("um")
                await asyncio.sleep(0.5)
                self.assertEqual(backend.bodies, [])
                self.assertEqual(h.executed, [])
                self.assertEqual(silent, [1], "closed as silent, not left to the thinking watchdog")


class VerifiedOnlySpeech(Patched):
    async def test_a_guess_at_the_outcome_is_never_heard(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
                              response(message("The Atrium Lamp is on.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp.")
            await asyncio.sleep(0.15)
            h.voice_says("Done, it's on.", ms=400)
            await asyncio.sleep(0.3)
            self.assertEqual(h.heard(), b"", "the guess is held, not played")
            backend.gate.set()
            await h.wait_commentary()
            await asyncio.sleep(0.1)
            self.assertEqual(h.heard(), b"", "and dropped once a verified result is coming")
            h.socket.push({"type": "session.commentary.appended"})
            h.voice_says("The Atrium Lamp is on now.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame)
            self.assertIn(fx.tone(300)[:4800], h.heard())

    async def test_an_acknowledgement_plays_while_the_work_runs(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the shopping list?")
            await asyncio.sleep(0.15)
            h.voice_says("Sure, one sec.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame)
            self.assertTrue(h.heard(), "an acknowledgement is not held")
            backend.gate.set()
            await h.wait_commentary()

    async def test_chat_releases_the_held_answer_in_order(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.15)
            h.voice_says("Owls can turn their heads very far.", ms=400)
            await asyncio.sleep(0.3)
            self.assertEqual(h.heard(), b"")
            backend.gate.set()
            await h.sink.wait_for(TTSAudioRawFrame)
            await asyncio.sleep(0.2)
            self.assertEqual(h.heard(), fx.tone(400), "released whole, byte for byte")
            self.assertEqual(h.socket.of("session.commentary.append"), [])


class NoFalseSuccess(unittest.IsolatedAsyncioTestCase):
    async def route(self, *responses, recognizer=None, executed=None, outcome_ok=True):
        from app.tool_results import classify_result
        backend = FakeBackend(*responses)
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post),
                            recognizer=recognizer)

        async def execute(name, args):
            raw = {"success": True} if outcome_ok else {"error": "Received invalid slot info"}
            (executed if executed is not None else []).append(name)
            return raw, classify_result(raw)
        return await router.route("Turn on the Atrium Lamp.", execute), backend

    async def test_a_tool_less_claim_is_reported_as_not_confirmed(self):
        result, _ = await self.route(response(message("Done, the lamp is on.")))
        self.assertEqual(result.kind, FAILED)
        self.assertNotIn("Verified", result.commentary())
        self.assertIn("do not say it was done", result.commentary())

    async def test_a_failed_tool_is_never_a_success(self):
        result, _ = await self.route(response(call("intent__HassTurnOn", {"name": "x"}, "c1")),
                                     response(message("The lamp could not be turned on.")),
                                     outcome_ok=False)
        self.assertEqual(result.kind, FAILED)
        self.assertNotIn("Verified", result.commentary())

    async def test_a_clarifying_question_is_not_a_result(self):
        result, _ = await self.route(response(message("Which lamp do you mean?")))
        self.assertEqual(result.kind, REPLY)
        self.assertIn("nothing has been done", result.commentary())

    async def test_sentinels(self):
        self.assertEqual((await self.route(response(message("CHAT"))))[0].kind, CHAT_KIND)
        self.assertEqual((await self.route(response(message("NO_REQUEST"))))[0].kind, NOTHING)
        self.assertIsNone((await self.route(response(message("CHAT"))))[0].commentary())

    def test_claim_and_acknowledgement_classifiers(self):
        for text in ("Done.", "The light is now on.", "Added milk."):
            self.assertTrue(claims_success(text), text)
        self.assertFalse(claims_success("Should I turn it off?"))
        for text in ("Sure, one sec.", "Checking.", "On it.", "Let me check that for you."):
            self.assertTrue(is_acknowledgement(text), text)
        for text in ("Done.", "It's on.", "Lights on.", "Ten forty-one.", "Milk and eggs."):
            self.assertFalse(is_acknowledgement(text), text)


class Dedupe(Patched):
    async def test_identical_calls_in_one_step_run_once(self):
        backend = FakeBackend(
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1"),
                     call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c2")),
            response(message("It is on.")),
        )
        async with RouterHarness(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp.")
            await h.wait_commentary()
            self.assertEqual(len(h.executed), 1)
            outputs = [i for i in backend.bodies[1]["input"] if i.get("type") == "function_call_output"]
            self.assertEqual({o["call_id"] for o in outputs}, {"c1", "c2"},
                             "both calls are answered, one action ran")

    async def test_a_continued_request_does_not_repeat_an_action_that_ran(self):
        backend = FakeBackend(
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
            response(message("It is on.")),
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c9")),
            response(message("It is on.")),
        )
        async with RouterHarness(self, backend) as h:
            with patch.object(live_service_module, "USER_FRAGMENT_MERGE_S", 5.0):
                await h.user_says("Turn on the Atrium")
                await asyncio.sleep(0.4)
                await h.user_says(" Lamp please", start_ms=3000)
                await h.wait_commentary(count=1)
                await asyncio.sleep(0.5)
            self.assertEqual(len(h.executed), 1,
                             "the repeat ledger answers the second identical call")


class SlowHouse(RouterHarness):
    """Home Assistant answers only when the test releases it."""

    def _record(self, name):
        async def handler(params):
            self.executed.append((name, dict(params.arguments or {})))
            await self.release.wait()
            await params.result_callback({"success": True, "speech": "Turned on"})
        return handler

    async def __aenter__(self):
        self.release = asyncio.Event()
        return await super().__aenter__()


class InFlightRepeat(Patched):
    """candidate-v33 review B1: a continuation re-routed while the first call
    was still running sent the identical action to the house a second time,
    because the repeat ledger learns an outcome only when the call returns."""

    async def _continue_while_running(self, h):
        with patch.object(live_service_module, "USER_FRAGMENT_MERGE_S", 5.0):
            await h.user_says("Turn on the Atrium Lamp")
            for _ in range(100):
                if h.executed:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(h.executed), 1, "the first call is running")
            await h.user_says(" please", start_ms=3000)
            await asyncio.sleep(0.6)
            h.release.set()
            await asyncio.sleep(0.6)

    async def test_backend_path(self):
        backend = FakeBackend(
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1")),
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c9")),
            response(message("It is on.")),
            response(message("It is on.")),
        )
        async with SlowHouse(self, backend) as h:
            await self._continue_while_running(h)
            self.assertEqual(len(h.executed), 1, "the identical action reached the house twice")

    async def test_local_intent_path(self):
        recognizer = FakeRecognizer(LocalMatch("HassTurnOn", {"name": "Atrium Lamp"}, ["light"], 1))
        async with SlowHouse(self, FakeBackend(), recognizer=recognizer) as h:
            await self._continue_while_running(h)
            self.assertEqual(len(h.executed), 1, "the identical action reached the house twice")
            await h.wait_commentary()


class RouteOnce(Patched):
    async def test_the_same_utterance_is_routed_once(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.service._router_start("Tell me about owls.")
            first = h.service._op
            await h.service._router_start("Tell me about owls.")
            self.assertIs(h.service._op, first, "a second trigger for the same utterance is not a new route")
            backend.gate.set()
            await asyncio.sleep(0.2)
            self.assertEqual(len(backend.bodies), 1)


class ReadOnlyClaims(unittest.IsolatedAsyncioTestCase):
    """candidate-v33 review N1: a read plus an invented action is not verified."""

    async def test_a_read_cannot_verify_an_action_claim(self):
        from app.tool_results import classify_result
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")),
                              response(message("Done, the lock is unlocked.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post))

        async def execute(name, args):
            raw = {"items": []}
            return raw, classify_result(raw)
        result = await router.route("Unlock the door.", execute)
        self.assertEqual(result.kind, FAILED)
        self.assertNotIn("Verified", result.commentary())

    async def test_a_state_answer_from_a_read_is_still_a_lookup(self):
        from app.tool_results import classify_result
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")),
                              response(message("The front door is locked.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post))

        async def execute(name, args):
            raw = {"items": []}
            return raw, classify_result(raw)
        self.assertEqual((await router.route("Is the door locked?", execute)).kind, LOOKUP)


from app.live_router import is_state_question  # noqa: E402


class DeviceNamesWithHouseholdWordsAreStillQuestions(unittest.TestCase):
    """Workshop real-audio trial, 0.18.34: "Is boost heat downstairs on?"
    turned Boost Heat on. "heat" is a household verb, so the question was
    judged action-shaped and the read-only guard never applied."""

    def test_be_questions_about_devices_are_read_only(self):
        for text in ("Is boost heat downstairs on?", "Is the heat on?", "Are the cooling fans running?",
                     "Is the front door locked?", "Hey, is boost heat on downstairs?",
                     "Was the lock left open?"):
            self.assertTrue(is_state_question(text), text)

    def test_commands_and_requests_are_not(self):
        for text in ("Turn on boost heat downstairs.", "Boost heat downstairs on.", "Heat the house.",
                     "Can you turn on the heat?", "Is boost heat on, turn it off.",
                     "Is it possible to turn on the heat?", "How do I turn off the heat?",
                     "Are you turning on the heat?"):
            self.assertFalse(is_state_question(text), text)


class ReadOnlyQuestionRunsNoAction(unittest.IsolatedAsyncioTestCase):
    """v35 rig lookup-light: "Is the test light on?" ran HassLightSet."""

    async def _route(self, text, *responses):
        from app.tool_results import classify_result
        backend = FakeBackend(*responses)
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post))
        executed = []

        async def execute(name, args):
            executed.append(name)
            raw = {"success": True, "speech": "Done"}
            return raw, classify_result(raw)
        return await router.route(text, execute), executed, backend

    async def test_a_state_question_never_runs_a_house_control_tool(self):
        result, executed, backend = await self._route(
            "Is the test light on?",
            response(call("homeassistant__GetLiveContext", {"name": "test light"}, "c1")),
            response(call("light__HassLightSet", {"name": "test light", "brightness": 100}, "c2")),
            response(message("The test light is on.")))
        self.assertEqual(executed, ["homeassistant__GetLiveContext"])
        self.assertEqual(result.kind, LOOKUP)
        self.assertIn("read_only_refused", result.timings)
        refusal = backend.bodies[-1]["input"][-1]
        self.assertEqual(refusal["type"], "function_call_output")
        self.assertIn("Not run", refusal["output"])

    async def test_a_question_about_a_device_named_with_a_household_word_runs_no_action(self):
        """Workshop real-audio trial, 0.18.34: "Is boost heat downstairs on?"
        ran HassTurnOn twice and turned Boost Heat on."""
        result, executed, _ = await self._route(
            "Is boost heat downstairs on?",
            response(call("intent__HassTurnOn", {"name": "boost heat downstairs"}, "c1")),
            response(call("homeassistant__GetLiveContext", {"name": "boost heat downstairs"}, "c2")),
            response(message("Boost heat downstairs is off.")))
        self.assertEqual(executed, ["homeassistant__GetLiveContext"])
        self.assertEqual(result.timings.get("read_only_refused"), ["intent__HassTurnOn"])

    async def test_a_question_cannot_confirm_a_pending_action(self):
        _, executed, _ = await self._route(
            "Is the door locked?",
            response(call("confirm_action", {"confirm_id": "x"}, "c1")),
            response(message("It is locked.")))
        self.assertEqual(executed, [])

    async def test_a_question_may_still_ask_an_agent(self):
        _, executed, _ = await self._route(
            "Where is Alex?",
            response(call("script__ask_openclaw", {"q": "where is Alex"}, "c1")),
            response(message("Alex is at work.")))
        self.assertEqual(executed, ["script__ask_openclaw"])

    async def test_a_command_still_runs(self):
        result, executed, _ = await self._route(
            "Turn on the test light.",
            response(call("light__HassLightSet", {"name": "test light", "brightness": 100}, "c1")),
            response(message("Done.")))
        self.assertEqual(executed, ["light__HassLightSet"])
        self.assertEqual(result.kind, ACTION)


class FirstCallSlow(RouterHarness):
    """Only the first house call is slow; later calls answer at once."""

    def _record(self, name):
        async def handler(params):
            self.executed.append((name, dict(params.arguments or {})))
            if len(self.executed) == 1:
                await self.release.wait()
            await params.result_callback({"success": True, "speech": "Done"})
        return handler

    async def __aenter__(self):
        self.release = asyncio.Event()
        return await super().__aenter__()


ON = ("intent__HassTurnOn", {"name": "Atrium Lamp"})
OFF = ("intent__HassTurnOff", {"name": "Atrium Lamp"})


def by_request(body):
    """A backend that answers from the request: one action per command, then done."""
    items = body["input"]
    if items and items[-1].get("type") == "function_call_output":
        return response(message("Done as asked."))
    text = str(items[-1].get("content", "")).lower()
    return response(call(*(OFF if " off " in f" {text} " else ON), f"c{len(text)}-{id(body) % 1000}"))


class CancelledJoin(Patched):
    """candidate-v33 second review B2: a call cancelled while in flight left
    its shared result unresolved, so every later identical request joined a
    call nobody awaited, never reached the house, and held the session.

    Since the background task list (app/live_tasks.py) a started call is
    never cancelled at all: it finishes in the background and its result is
    delivered. These scenarios now pin that the house is still reachable
    afterwards and that the first call's result is spoken, not dropped."""

    async def _scenario(self, h, supersede, repeat=ON, repeat_text="Turn on the Atrium Lamp"):
        await h.user_says("Turn on the Atrium Lamp")
        for _ in range(100):
            if h.executed:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(h.executed, [ON])
        await supersede(h)
        await asyncio.sleep(0.3)
        h.release.set()
        await asyncio.sleep(3.0)
        self.assertTrue(any("earlier request" in m["content"]
                            for m in h.socket.of("session.commentary.append")),
                        "the first call's result is delivered, not dropped")
        before = list(h.executed)
        await h.user_says(repeat_text, start_ms=20000)
        await asyncio.sleep(3.0)
        op = h.service._op
        self.assertEqual(h.executed[len(before):], [repeat],
                         "a new request reaches the house")
        self.assertTrue(op.done, "and its route finishes")

    async def test_a_new_utterance_never_cancels_a_started_action(self):
        """Audit (b): a started tool finishes and its result is still spoken;
        later requests run normally."""
        backend = FakeBackend(*([by_request] * 12))
        async with FirstCallSlow(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp")
            for _ in range(100):
                if h.executed:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(2.5)
            await h.user_says("Turn off the Atrium Lamp", start_ms=8000)
            await asyncio.sleep(1.0)
            h.release.set()
            await asyncio.sleep(1.5)
            self.assertEqual(h.executed[:2], [ON, OFF], "both ran, the first exactly once")
            contents = [m["content"] for m in h.socket.of("session.commentary.append")]
            # merge (v35): delivered through the task list (live_tasks.result_commentary).
            self.assertTrue(any("the earlier request" in c for c in contents), contents)
            await asyncio.sleep(2.0)
            await h.user_says("Turn on the Atrium Lamp", start_ms=20000)
            await asyncio.sleep(1.5)
            self.assertEqual(h.executed.count(ON), 2, "a later identical request runs normally")

    async def test_a_new_utterance_does_not_cancel_the_first_call(self):
        backend = FakeBackend(
            response(call(*ON, "c1")),
            response(call(*OFF, "c2")), response(message("The lamp is off.")),
            response(message("The lamp is on.")),   # the first call's own continuation
            response(call(*ON, "c3")), response(message("The lamp is on.")),
        )
        async with FirstCallSlow(self, backend) as h:
            async def supersede(h):
                await asyncio.sleep(2.5)
                await h.user_says("Turn off the Atrium Lamp", start_ms=8000)
                await asyncio.sleep(1.0)
            await self._scenario(h, supersede)
            self.assertEqual(h.executed.count(ON), 2, "each ON request ran exactly once")

    async def test_a_disconnect_does_not_cancel_the_first_local_call(self):
        recognizer = FakeRecognizer(LocalMatch("HassTurnOn", {"name": "Atrium Lamp"}, ["light"], 1))
        async with FirstCallSlow(self, FakeBackend(), recognizer=recognizer) as h:
            async def supersede(h):
                await h.service._router_supersede("disconnect")
            await self._scenario(h, supersede)

    async def test_a_session_closed_mid_call_does_not_poison_the_next_wake(self):
        backend = FakeBackend(response(call(*ON, "c1")), response(message("The lamp is on.")),
                              response(call(*OFF, "c3")), response(message("The lamp is off.")))
        async with FirstCallSlow(self, backend) as h:
            await h.user_says("Turn on the Atrium Lamp")
            for _ in range(100):
                if h.executed:
                    break
                await asyncio.sleep(0.02)
            await asyncio.wait_for(h.service.close_session("max lifetime"), 10)
            h.release.set()
            await asyncio.sleep(0.5)
            self.assertEqual(h.service._router_running, {})
            self.assertEqual(h.service.tasks_pending, 1, "the finished call waits for a session")
            await h.open_session()
            await h.start_session("sess_2")
            h.service.turn_timeline.begin("w2")
            await h.user_says("Turn off the Atrium Lamp", start_ms=1000)
            await asyncio.sleep(3.0)
            self.assertEqual(h.executed, [ON, OFF], "the next wake's request reaches the house")
            self.assertTrue(h.service._op.done)
            contents = [m["content"] for m in h.socket.of("session.commentary.append")]
            self.assertTrue(any("earlier request" in c and "on" in c for c in contents), contents)
            await heard(h)
            self.assertEqual(h.service.tasks_pending, 0)


class FailedActionWithARead(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_action_is_not_verified_by_a_successful_read(self):
        from app.tool_results import classify_result
        backend = FakeBackend(
            response(call("intent__HassTurnOn", {"name": "Atrium Lamp"}, "c1"),
                     call("todo__get_items", {}, "c2")),
            response(message("The Atrium Lamp is on.")))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post))

        async def execute(name, args):
            raw = {"items": []} if "get_items" in name else {"error": "Received invalid slot info"}
            return raw, classify_result(raw)
        result = await router.route("Turn on the Atrium Lamp.", execute)
        self.assertEqual(result.kind, FAILED)
        self.assertNotIn("Verified", result.commentary())


class NothingBeforeTheRequest(Patched):
    """Workshop canary attempt 2 (candidate-v33): 0.9 s after session.start the
    endpoint emitted a quiet sound before the user had said anything. Once streamed,
    it put the device into "replying"; the device drained it, went idle, and
    the follow-up cut-off closed the session before the request was heard."""

    async def test_audio_before_any_user_speech_is_never_heard(self):
        backend = FakeBackend(response(message("CHAT")))
        async with RouterHarness(self, backend) as h:
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(300, amplitude=169))})
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(300))})
            await asyncio.sleep(0.3)
            self.assertEqual(h.heard(), b"", "nobody asked anything yet")
            await h.user_says("Tell me a fun fact about owls.")
            await asyncio.sleep(0.05)
            h.voice_says("They can turn their heads very far.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
            self.assertEqual(h.heard(), fx.tone(300), "the answer to the request is heard, and only it")

    async def test_an_acknowledgement_while_the_user_is_still_speaking_is_heard(self):
        backend = FakeBackend(response(message("CHAT")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the shopping list")
            h.voice_says("Sure, one sec.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
            backend.gate.set()


class CanaryAttemptThree(Patched):
    """Workshop canary attempt 3 (candidate-v33): the transcript was cut to
    "What time", the backend called it a fragment, the held speech was
    released, and the voice told the time with no tool; its personalised
    acknowledgement ("One moment, <name>.") had been held as a claim."""

    async def test_a_cut_clock_question_reads_the_clock(self):
        backend = FakeBackend()
        async with RouterHarness(self, backend) as h:
            await h.user_says("What time")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [("llm__GetDateTime", {})])
            self.assertEqual(backend.bodies, [], "no model call for the clock")
            self.assertIn("Verified result", sent[0]["content"])

    async def test_speech_held_for_a_non_request_is_dropped_not_released(self):
        backend = FakeBackend(response(message("NO_REQUEST")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("and then the weather was fine")
            await asyncio.sleep(0.1)
            h.voice_says("It is a quarter past eleven.", ms=300)
            await asyncio.sleep(0.2)
            backend.gate.set()
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"", "nothing was asked; the voice's guess is not played")

    async def test_a_personalised_acknowledgement_plays(self):
        from app import live_router
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
        backend.gate.clear()
        live_router.set_known_names(["Alex"])
        self.addCleanup(live_router.set_known_names, [])
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the shopping list?")
            await asyncio.sleep(0.1)
            h.voice_says("One moment, Alex.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
            backend.gate.set()
            await h.wait_commentary()

    async def test_the_renderer_cuts_lines_off_the_event_loop(self):
        import threading
        import app.live_voice_prompts as lvp
        from tests.test_live_voice_prompts import FakeRenderSocket
        threads = []
        real = lvp.cut_line

        def spy(*args):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(*args)

        async def connect(uri, additional_headers=None, **_):
            return FakeRenderSocket()
        renderer = lvp.LiveVoiceRenderer("k", model="gpt-live-1", backend_model="gpt-6-luna", voice="vesper",
                                         connect=connect, linger_s=0.0, line_timeout_s=3.0)
        with patch.object(lvp, "cut_line", spy):
            result = await renderer.render(["One moment."])
        self.assertIn("One moment.", result)
        self.assertTrue(threads)
        self.assertFalse(any(threads), "the timeline scan never blocks the loop a device session runs on")


CAPITALISED_ANSWERS = ("Saturday", "Tuesday", "October", "Finished", "Complete", "Enabled")


class FormOfAddressIsNotAClaim(Patched):
    """candidate-v33 fifth review B3: the form-of-address rule dropped any one
    word after a comma, so "Okay, unlocked." played as an acknowledgement
    while the unlock was still waiting for a spoken yes."""

    def test_only_a_name_is_dropped(self):
        from app import live_router
        live_router.set_known_names(["Alex"])
        self.addCleanup(live_router.set_known_names, [])
        for text in ("Okay, done.", "Sure, done.", "Got it, done!", "Okay, unlocked.",
                     "Alright, locked.", "Your light, on.", "Okay, off."):
            self.assertFalse(is_acknowledgement(text), text)
        self.assertTrue(is_acknowledgement("One moment, Alex."))

    def test_only_a_configured_name_is_a_form_of_address(self):
        """Sixth review B4: a capitalised word is as often the answer."""
        from app import live_router
        live_router.set_known_names(["Alex"])
        self.addCleanup(live_router.set_known_names, [])
        # Capitalised answers, written as "Okay, <word>." the way a transcript
        # delivers them; none of them is a configured name.
        for word in CAPITALISED_ANSWERS + ("Jordan",):
            self.assertFalse(is_acknowledgement("Okay, " + word + "."), word)

    def test_a_bare_yes_or_no_is_an_answer(self):
        """Sixth review B5: "Yes." about a lock before any read."""
        for text in ("Yes.", "No.", "Yeah.", "Yep.", "Yes, one sec."):
            self.assertFalse(is_acknowledgement(text), text)
        self.assertFalse(is_acknowledgement("No problem."), "no waiting phrase: held")
        self.assertFalse(is_acknowledgement("No problem, one sec."), "tenth review N32: an answer")

    async def test_a_yes_about_a_lock_before_any_read_is_never_heard(self):
        backend = FakeBackend(response(message("I could not read the Entry Lock.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Is the Entry Lock locked?")
            await asyncio.sleep(0.1)
            h.voice_says("Yes.", ms=300)
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"")
            backend.gate.set()
            await h.wait_commentary()

    async def test_a_guessed_day_before_the_result_is_never_heard(self):
        backend = FakeBackend(response(call("llm__GetDateTime", {}, "c1")),
                              response(message("The market is on Sunday.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("What day is the market this week?")
            await asyncio.sleep(0.1)
            h.voice_says("Okay, " + CAPITALISED_ANSWERS[0] + ".", ms=400)
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"")
            backend.gate.set()
            await h.wait_commentary()

    async def test_an_unlock_claimed_before_the_yes_is_never_heard(self):
        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = FakeBackend(response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
                              response(message("Do you want me to unlock the Entry Lock?")))
        backend.gate.clear()
        async with RouterHarness(self, backend, gate=gate) as h:
            await h.user_says("Unlock the Entry Lock.")
            await asyncio.sleep(0.1)
            h.voice_says("Okay, unlocked.", ms=400)
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"")
            backend.gate.set()
            await h.wait_commentary()
            await asyncio.sleep(0.2)
            self.assertEqual(h.heard(), b"")
            self.assertEqual(h.executed, [])


class NothingMeansSilence(Patched):
    async def test_the_rest_of_an_answer_to_nothing_is_dropped_too(self):
        backend = FakeBackend(response(message("NO_REQUEST")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("and then the weather was fine")
            await asyncio.sleep(0.1)
            h.voice_says("It is a quarter", ms=200)
            await asyncio.sleep(0.2)
            backend.gate.set()
            await asyncio.sleep(0.3)
            h.socket.push({"type": "session.output_transcript.delta", "delta": " past eleven."})
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(200))})
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"", "no part of an answer to nothing is played")

    async def test_nothing_after_a_heard_acknowledgement_says_it_was_not_caught(self):
        """Kitchen 2026-10-13: an acknowledgement played, the backend said
        NO_REQUEST, and the device sat "thinking" for 20 s in silence."""
        from app.live_service import NOT_CAUGHT_TEXT
        backend = FakeBackend(response(message("NO_REQUEST")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Turn on the kitchen lights.")
            await asyncio.sleep(0.1)
            h.voice_says("Okay, one moment.", ms=300)
            await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
            backend.gate.set()
            sent = await h.wait_commentary()
            self.assertIn(NOT_CAUGHT_TEXT, sent[-1]["content"])
            self.assertEqual(h.executed, [])


class RoomTalkClosesTheSession(Patched):
    """Workshop rig 2026-10-16: a monologue spoken after a wake came in as
    separate utterances, each answered "Sorry, I didn't catch that". The
    second utterance with no request in one session closes it silently."""

    async def _closes(self, h):
        closed = []

        async def close():
            closed.append(1)

        h.service.on_background_conversation = close
        return closed

    async def test_a_second_utterance_with_no_request_closes_silently(self):
        backend = FakeBackend(response(message("NO_REQUEST")), response(message("NO_REQUEST")))
        async with RouterHarness(self, backend) as h:
            closed = await self._closes(h)
            await h.user_says("and then the weather was fine all weekend")
            await asyncio.sleep(0.3)
            self.assertEqual(closed, [], "one utterance with no request is not a conversation")
            with self.assertLogs("app.live_service", level="WARNING") as logs:
                await h.user_says("we walked along the cliffs for a couple of hours")
                await asyncio.sleep(0.3)
            self.assertEqual(closed, [1])
            self.assertTrue(any("background conversation" in m for m in logs.output))
            self.assertFalse(any("cliffs" in m for m in logs.output), "never the words")
            self.assertEqual(h.heard(), b"")

    async def test_a_backend_say_it_again_counts_as_not_caught(self):
        backend = FakeBackend(response(message("NO_REQUEST")),
                              response(message("Sorry, could you say that again?")))
        async with RouterHarness(self, backend) as h:
            closed = await self._closes(h)
            await h.user_says("and then the weather was fine all weekend")
            await asyncio.sleep(0.3)
            await h.user_says("we walked along the cliffs for a couple of hours")
            await asyncio.sleep(0.3)
            self.assertEqual(closed, [1])

    async def test_a_request_between_does_not_close(self):
        backend = FakeBackend(response(message("NO_REQUEST")),
                              response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
        async with RouterHarness(self, backend) as h:
            closed = await self._closes(h)
            await h.user_says("and then the weather was fine all weekend")
            await asyncio.sleep(0.3)
            await h.user_says("What's on the shopping list?")
            await h.wait_commentary()
            self.assertEqual(closed, [])


class OnlyAWaitingPhraseIsAnAcknowledgement(Patched):
    """candidate-v33 seventh review B6: a bag of acknowledgement words let a
    count or an affirmative through ("Just one.", "Mm hmm.", "Absolutely.").
    An acknowledgement is now a complete waiting phrase, nothing else."""

    def test_answers_built_from_friendly_words_are_held(self):
        for text in ("Just one.", "One timer.", "Mm hmm.", "Absolutely.", "Of course.",
                     "Right.", "Problem.", "Certainly.", "Sure."):
            self.assertFalse(is_acknowledgement(text), text)
        for text in ("Sure, one sec.", "Checking.", "On it.", "Let me check that for you.",
                     "Hang on.", "Okay, one moment."):
            self.assertTrue(is_acknowledgement(text), text)

    def test_nothing_may_follow_the_waiting_phrase_but_another(self):
        """Eighth review B7 and N27."""
        for text in ("One sec. Absolutely.", "Let me check. Got it.", "Checking. Right.",
                     "Hang on. Great.", "Mm hmm, one sec.", "Of course, one moment.",
                     "Absolutely, checking."):
            self.assertFalse(is_acknowledgement(text), text)
        self.assertTrue(is_acknowledgement("One sec. Checking."))

    async def test_an_affirmative_after_the_waiting_phrase(self):
        backend = FakeBackend(response(message("Which door do you mean?")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("Is the garage door closed?")
            await asyncio.sleep(0.1)
            h.voice_says("Let me check.", ms=200)
            await asyncio.sleep(0.2)
            heard = len(h.heard())
            h.voice_says(" Absolutely.", ms=400)
            await asyncio.sleep(0.4)
            self.assertEqual(len(h.heard()), heard, "the affirmative is held")
            backend.gate.set()
            await h.wait_commentary()

    async def test_a_trailing_acknowledgement_never_arms_a_held_action(self):
        """Eighth review N26: the acknowledgement plays after the result went
        in but before the voice took it; it is not the question."""
        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = FakeBackend(response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
                              response(message("Do you want me to unlock the Entry Lock?")))
        backend.gate.clear()
        with patch.object(live_service_module, "ACK_LAG_S", 1.0):
            async with RouterHarness(self, backend, gate=gate) as h:
                await h.user_says("Unlock the Entry Lock.")
                await asyncio.sleep(0.1)
                h.voice_says("Sure, one sec.", ms=300)
                await asyncio.sleep(0.1)
                backend.gate.set()
                await h.wait_commentary()
                await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
                await asyncio.sleep(0.2)
                self.assertTrue(h.heard(), "the acknowledgement was heard after the result went in")
                self.assertTrue(h.service._confirmation_prompts_pending,
                                "but it is not the question and arms nothing")

    async def _never_heard(self, question, answer, result):
        backend = FakeBackend(response(message(result)))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says(question)
            await asyncio.sleep(0.1)
            h.voice_says(answer, ms=400)
            await asyncio.sleep(0.4)
            self.assertEqual(h.heard(), b"", answer)
            backend.gate.set()
            await h.wait_commentary()
            await asyncio.sleep(0.3)
            self.assertEqual(h.heard(), b"", answer + " is not played once the result is in either")

    async def test_a_guessed_count(self):
        await self._never_heard("How many timers are running?", "Just one.",
                                "Which timers do you mean?")

    async def test_an_affirmative_about_a_lock(self):
        await self._never_heard("Is the Entry Lock locked?", "Mm hmm.",
                                "Which lock do you mean?")

    async def test_an_affirmative_about_a_garage_door(self):
        await self._never_heard("Is the garage door closed?", "Absolutely.",
                                "Which door do you mean?")


class SlowRequestStaysConversational(Patched):
    """A request that takes a while (an agent question) becomes a background
    task: the voice says it is still working on it and will report back, the
    conversation goes on, the session is NOT held open (cost rule), and the
    result is spoken when it lands (tests/test_live_tasks.py has the rest)."""

    async def test_the_voice_narrates_the_promotion_then_the_result(self):
        from app.live_tasks import TaskPolicy
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        with patch.object(live_service_module, "PROGRESS_SILENT_FALLBACK_S", 0.1):
            async with RouterHarness(self, backend) as h:
                h.service.task_policy = TaskPolicy(promote_s=0.3)
                await h.user_says("Ask the assistant what the weather is tomorrow.")
                await h.wait_commentary(count=1, timeout=2.0)
                progress = h.socket.of("session.commentary.append")[0]["content"]
                self.assertIn("still working on it", progress)
                self.assertIn("will tell them", progress)
                h.voice_says("Still working on it, I'll let you know.", ms=300)
                await h.sink.wait_for(TTSAudioRawFrame, timeout=2.0)
                self.assertTrue(h.service.backend_work_active,
                                "a lookup in a conversation keeps its session within the hard bound")
                backend.gate.set()
                for _ in range(100):
                    if any("Verified" in m["content"] or "Say this" in m["content"]
                           for m in h.socket.of("session.commentary.append")):
                        break
                    await asyncio.sleep(0.02)
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                self.assertTrue(any("sunny" in c and "earlier request" in c for c in contents), contents)

    async def test_a_silent_voice_gets_the_fallback_clip_at_the_promotion(self):
        from app.live_tasks import TaskPolicy
        backend = FakeBackend(response(message("It is sunny tomorrow.")))
        backend.gate.clear()
        with patch.object(live_service_module, "PROGRESS_SILENT_FALLBACK_S", 0.1):
            async with RouterHarness(self, backend) as h:
                h.service.task_policy = TaskPolicy(promote_s=0.3)
                fired = []

                async def clip(device_id):
                    fired.append(device_id)
                    return True
                h.service.on_fallback_ack = clip
                await h.user_says("Ask the assistant what the weather is tomorrow.")
                await asyncio.sleep(0.8)
                self.assertTrue(fired, "nobody spoke after the promotion; the clip did")
                backend.gate.set()
                await asyncio.sleep(0.2)

    def test_progress_phrases_are_acknowledgements(self):
        self.assertFalse(is_acknowledgement("Almost there."), "tenth review N39: an answer")
        for text in ("Still working on it, hang on.", "Still checking.",
                     "This is taking a little longer, one moment."):
            self.assertTrue(is_acknowledgement(text), text)
        self.assertFalse(is_acknowledgement("Still working on it. It is sunny."))


class AuditFindings(Patched):
    """Independent audit 2026-10-12, items (a) and (c)."""

    async def test_an_unpunctuated_yes_to_a_confirmation_is_routed(self):
        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = FakeBackend(
            response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
            response(message("Do you want me to unlock the Entry Lock?")),
            response(message("Which lock do you mean?")),
        )
        async with RouterHarness(self, backend, gate=gate) as h:
            await h.user_says("Unlock the Entry Lock.")
            await h.wait_commentary()
            h.socket.push({"type": "session.commentary.appended"})
            h.voice_says("Do you want me to unlock the Entry Lock?")
            await asyncio.sleep(0.2)
            await h.user_says("Yes", start_ms=6000)
            await asyncio.sleep(0.5)
            self.assertEqual(len(backend.bodies), 3, "the bare reply reached the router")
            self.assertEqual(backend.bodies[-1]["input"][-1], {"role": "user", "content": "Yes"})

    def test_a_local_action_needs_a_named_target_and_few_entities(self):
        names = [t["name"] for t in TOOLS]
        self.assertIsNone(local_tool_call(LocalMatch("HassTurnOn", {"domain": "light"}, ["light"], 3), names),
                          "domain-only: every exposed light")
        self.assertIsNone(local_tool_call(LocalMatch("HassTurnOn", {"area": "x"}, ["light"], 9), names),
                          "more than eight targets")
        self.assertIsNotNone(local_tool_call(LocalMatch("HassTurnOn", {"area": "x"}, ["light"], 8), names))


class CostBounds(Patched):
    """Cost is a design requirement: nothing connects to OpenAI between wakes,
    and no routed request can hold a session open without end."""

    async def test_router_mode_opens_no_socket_while_idle_or_after_a_close(self):
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS,
                                          post=FakeBackend().post))
        async with LiveHarness(self, router=router, tools=TOOLS, on_demand=True,
                               instructions=lambda: "Be brief.") as h:
            await asyncio.sleep(0.3)
            self.assertEqual(h.sockets, [], "idle: no socket at all")
            await h.open_session()
            await h.start_session()
            self.assertEqual(len(h.sockets), 1)
            close = asyncio.create_task(h.service.close_session("idle"))
            await h.socket.wait_for_sent("session.close")
            h.socket.push({"type": "session.closed"})
            await asyncio.wait_for(close, 5)
            await asyncio.sleep(0.3)
            self.assertEqual(len(h.sockets), 1, "nothing is pre-opened after the close")
            self.assertIsNone(h.service._warm_socket)

    async def test_a_routed_request_has_a_hard_end(self):
        backend = FakeBackend()
        backend.gate.clear()  # the backend never answers
        with patch.object(live_service_module, "ROUTE_TIMEOUT_S", 0.3):
            async with RouterHarness(self, backend) as h:
                await h.user_says("What's on the shopping list?")
                sent = await h.wait_commentary(timeout=3.0)
                self.assertIn("not completed", sent[-1]["content"])
                await asyncio.sleep(0.1)
                h.socket.push({"type": "session.commentary.appended"})
                await asyncio.sleep(0.1)
                self.assertFalse(h.service.router_pending, "the session can idle-close again")
            backend.gate.set()


class EarlierResultWaitsForTheNewVerdict(unittest.IsolatedAsyncioTestCase):
    """candidate-v33 tenth review B8: an earlier request's late result opened
    the gate while the user's new question was still open, so the voice's
    guess at the new question played with nothing verified."""

    class Backend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.second = asyncio.Event()

        async def post(self, body):
            self.bodies.append(json.loads(json.dumps(body)))
            items = body["input"]
            if items and items[-1].get("type") == "function_call_output":
                return response(message("The Atrium Lamp is on."))
            if "atrium" in str(items[-1].get("content", "")).lower():
                return response(call(*ON, "c1"))
            await self.second.wait()
            return response(message("Which lock do you mean?"))

    async def _case(self, push_ack, gap):
        backend = self.Backend()
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 5.0):
            async with SlowHouse(self, backend) as h:
                await h.user_says("Turn on the Atrium Lamp")
                for _ in range(200):
                    if h.executed:
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(gap)
                await h.user_says("Is the Entry Lock locked?", start_ms=8000)
                await asyncio.sleep(0.05)
                h.release.set()
                await asyncio.sleep(0.3)
                if push_ack:
                    h.socket.push({"type": "session.commentary.appended"})
                    await asyncio.sleep(0.05)
                before = len(h.heard())
                h.voice_says("Yes, the Entry Lock is locked.", ms=400)
                await asyncio.sleep(0.45)
                heard = len(h.heard()) - before
                backend.second.set()
                # merge (v35): the earlier result now comes through the task
                # list, which leaves TASK_AFTER_RESULT_S after the new answer.
                await asyncio.sleep(3.5)
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                return heard, contents

    async def test_superseded_request(self):
        heard, contents = await self._case(True, 2.6)
        self.assertEqual(heard, 0)
        self.assertTrue(any("Atrium Lamp is on" in c for c in contents), "the earlier result is still spoken")

    async def test_continuation_fragment(self):
        heard, _ = await self._case(True, 1.2)
        self.assertEqual(heard, 0)

    async def test_superseded_request_without_ack(self):
        heard, _ = await self._case(False, 2.6)
        self.assertEqual(heard, 0)


class ProgressIsBounded(Patched):
    """Re-pinned at the v35 merge: promotion and the task progress watch
    (app/live_tasks.py) replace _router_progress_watch; the bound is
    TaskPolicy.progress_max."""

    async def test_at_most_progress_max_prompts(self):
        from app.live_tasks import TaskPolicy
        backend = FakeBackend()
        backend.gate.clear()
        with patch.object(live_service_module, "PROGRESS_SILENT_FALLBACK_S", 0.01):
            async with RouterHarness(self, backend) as h:
                h.service.task_policy = TaskPolicy(promote_s=0.05, progress_every_s=0.05,
                                                   progress_max=3, progress_quiet_s=0.0)
                await h.user_says("Ask the assistant something slow.")
                await asyncio.sleep(1.5)
                prompts = [m for m in h.socket.of("session.commentary.append")
                           if "still working on the earlier request" in m["content"]]
                self.assertEqual(len(prompts), 3)
                backend.gate.set()


LOCK_OFF = ("intent__HassTurnOff", {"name": "Entry Lock"})


class OvertakenConfirmation(unittest.IsolatedAsyncioTestCase):
    """candidate-v33 eleventh review B9: an unlock overtaken by a newer
    request was armed by the newer answer's audio, so a "yes" said before the
    unlock question ran the unlock. An overtaken confirmation is withdrawn."""

    class Backend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.late = asyncio.Event()

        async def post(self, body):
            self.bodies.append(json.loads(json.dumps(body)))
            items = body["input"]
            last = items[-1]
            text = str(last.get("content", ""))
            if last.get("type") == "function_call_output":
                out = json.loads(last["output"])
                if "confirm_id" in out:
                    await self.late.wait()
                    return response(message("Do you want me to unlock the Entry Lock?"))
                if "items" in out:
                    return response(message("Milk and eggs."))
                return response(message("The Entry Lock is unlocked."))
            if "unlock" in text.lower():
                return response(call(*LOCK_OFF, "c1"))
            if "list" in text:
                return response(call("todo__get_items", {}, "c2"))
            if text.strip().lower().startswith("yes"):
                ids = [json.loads(i["output"]).get("confirm_id") for i in items
                       if i.get("type") == "function_call_output" and i["output"].startswith("{")]
                ids = [i for i in ids if i]
                if ids:
                    return response(call("confirm_action", {"confirm_id": ids[-1]}, "c3"))
            return response(message("CHAT"))

    async def test_a_yes_during_a_newer_answer_never_runs_the_overtaken_unlock(self):
        from app.main import Application

        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = self.Backend()
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
                await asyncio.sleep(2.6)
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
                await asyncio.sleep(2.5)
                self.assertNotIn(LOCK_OFF, h.executed)
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                self.assertTrue(any("was not done" in c for c in contents), contents)


class DeliveredThenOvertakenConfirmation(unittest.IsolatedAsyncioTestCase):
    """candidate-v33 twelfth review B11 and B12. B11: the unlock question was
    sent, with the user's turn closed, but a new request began before its
    audio arrived; the question was dropped unheard and the newer answer's
    audio armed the unlock. B12: the queue-path withdraw (result arriving
    while the user's turn is open) had no regression."""

    class Backend(FakeBackend):
        def __init__(self, delay=0.0):
            super().__init__()
            self.delay = delay
            self.late = asyncio.Event()
            self.late.set()

        async def post(self, body):
            self.bodies.append(json.loads(json.dumps(body)))
            items = body["input"]
            last = items[-1]
            text = str(last.get("content", ""))
            if last.get("type") == "function_call_output":
                out = json.loads(last["output"])
                if "confirm_id" in out:
                    await self.late.wait()
                    await asyncio.sleep(self.delay)
                    return response(message("Do you want me to unlock the Entry Lock?"))
                if "items" in out:
                    return response(message("Milk and eggs."))
                return response(message("The Entry Lock is unlocked."))
            if "list" in text:
                return response(call("todo__get_items", {}, "c2"))
            if "unlock" in text.lower():
                return response(call(*LOCK_OFF, "c1"))
            if text.strip().lower().startswith("yes"):
                ids = [json.loads(i["output"]).get("confirm_id") for i in items
                       if i.get("type") == "function_call_output" and i["output"].startswith("{")]
                ids = [i for i in ids if i]
                if ids:
                    return response(call("confirm_action", {"confirm_id": ids[-1]}, "c3"))
            return response(message("CHAT"))

    def _wire(self, h, gate):
        from app.main import Application
        app = object.__new__(Application)
        app.action_gate = gate
        h.service.register_function("confirm_action",
                                    Application._create_confirm_handler(app, h.service))

    @staticmethod
    def _gate():
        async def states():
            return STATES
        return ActionGate(directory=EntityDirectory(states))

    async def test_b11_a_sent_but_unheard_question_is_never_armed_by_the_next_answer(self):
        gate = self._gate()
        backend = self.Backend(delay=1.8)
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 30.0):
            async with RouterHarness(self, backend, gate=gate) as h:
                self._wire(h, gate)
                await h.user_says("Unlock the Entry Lock.")
                await h.wait_commentary(count=1, timeout=5)  # question sent, turn closed
                self.assertTrue(h.service._confirmation_prompts_pending)
                await asyncio.sleep(0.5)
                await h.user_says("What's on the list?", start_ms=4000)
                await asyncio.sleep(0.1)
                h.socket.push({"type": "session.commentary.appended"})
                h.voice_says("Do you want me to unlock the Entry Lock?", ms=300)  # held, dropped
                await h.wait_commentary(count=2, timeout=8)
                h.socket.push({"type": "session.commentary.appended"})
                await asyncio.sleep(0.05)
                h.voice_says("Milk and eggs.", ms=300)
                await asyncio.sleep(1.4)
                await h.user_says("Yes.", start_ms=9500)
                await asyncio.sleep(3.0)
                self.assertNotIn(LOCK_OFF, h.executed)
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                self.assertTrue(any("was not done" in c for c in contents), contents)

    async def test_b11_control_a_heard_question_and_a_yes_still_runs(self):
        gate = self._gate()
        backend = self.Backend(delay=1.8)
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 30.0):
            async with RouterHarness(self, backend, gate=gate) as h:
                self._wire(h, gate)
                await h.user_says("Unlock the Entry Lock.")
                await h.wait_commentary(count=1, timeout=5)
                h.socket.push({"type": "session.commentary.appended"})
                await asyncio.sleep(0.05)
                h.voice_says("Do you want me to unlock the Entry Lock?", ms=300)
                await asyncio.sleep(1.4)
                await h.user_says("Yes.", start_ms=9500)
                await asyncio.sleep(3.0)
                self.assertIn(LOCK_OFF, h.executed)

    async def test_b12_a_result_queued_while_the_user_speaks_withdraws_its_question(self):
        gate = self._gate()
        backend = self.Backend()
        backend.late.clear()
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 30.0):
            async with RouterHarness(self, backend, gate=gate) as h:
                self._wire(h, gate)
                await h.user_says("Unlock the Entry Lock.")
                for _ in range(300):
                    if gate._pending:
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.2)
                await h.user_says("And what's on the list?", start_ms=3000)
                await asyncio.sleep(0.05)
                self.assertTrue(h.service._user_turn.open)
                backend.late.set()  # the unlock result arrives while the user speaks
                await asyncio.sleep(0.3)
                self.assertFalse(h.service._confirmation_prompts_pending)
                await asyncio.sleep(2.5)
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                self.assertFalse(any("Do you want me to unlock" in c for c in contents), contents)
                h.socket.push({"type": "session.commentary.appended"})
                await asyncio.sleep(0.05)
                h.voice_says("Milk and eggs.", ms=300)
                await asyncio.sleep(1.4)
                await h.user_says("Yes.", start_ms=9500)
                await asyncio.sleep(3.0)
                self.assertNotIn(LOCK_OFF, h.executed)
                # v35 merge: the "not done" line comes through the task list,
                # after the newer answer (TASK_AFTER_RESULT_S), not inside 2.5 s.
                contents = [m["content"] for m in h.socket.of("session.commentary.append")]
                self.assertFalse(any("Do you want me to unlock" in c for c in contents), contents)
                self.assertTrue(any("was not done" in c for c in contents), contents)


class ArmOnlyTheCurrentQuestion(unittest.TestCase):
    """Twelfth review B11: in router mode, heard audio arms only the current
    operation's question, never an id left over from an earlier one."""

    def test_only_the_current_operations_confirm_id_is_fenced(self):
        from types import SimpleNamespace
        from app.live_service import OpenAILiveLLMService, RouterOp
        fenced = []
        svc = object.__new__(OpenAILiveLLMService)
        svc.router = object()
        svc.action_gate = SimpleNamespace(fence_after_prompt=lambda cid, *a: fenced.append(cid))
        svc.turn_timeline = SimpleNamespace(user_turn_seq=3)
        svc._input_audio_ms_sent = 0
        op = RouterOp(op_id="op2", seq=3, text="t", started_mono=0.0)
        op.confirm_ids.add("current")
        svc._op = op
        svc._confirmation_prompts_pending = {"current", "stale"}
        svc._arm_confirmation_prompts()
        self.assertEqual(fenced, ["current"])
        self.assertEqual(svc._confirmation_prompts_pending, set())
        svc._op, fenced[:] = None, []
        svc._confirmation_prompts_pending = {"stale"}
        svc._arm_confirmation_prompts()
        self.assertEqual(fenced, [])


class QueuedResultsLifecycle(Patched):
    """Eleventh review B10, re-pinned at the v35 merge: the orphan queue was
    replaced by the background task list (app/live_tasks.py), whose results
    may be delivered in a later session by design (framed as the earlier
    request, after the user's own request). What stays pinned: nothing is
    spoken into a closing session, a held confirmation never survives, and a
    short unrouted utterance lets a finished result be spoken."""

    @staticmethod
    def _finished_task(h, text="Turn on the Atrium Lamp", kind="action",
                       result="The Atrium Lamp is on.", confirm_ids=()):
        from app.live_router import RouteResult
        op = live_service_module.RouterOp(op_id="op-q1", seq=0, text=text, started_mono=0.0,
                                          key=live_service_module.normalize_request(text))
        op.background = True
        op.tools_started = True
        op.confirm_ids = set(confirm_ids)
        op.result = RouteResult(kind, result, "ha_local")
        op.done = True
        op.result_ready_mono = live_service_module.time.monotonic()
        h.service._tasks[op.op_id] = op
        return op

    async def test_a_close_speaks_nothing_and_keeps_no_held_confirmation(self):
        backend = FakeBackend(*([by_request] * 6))
        async with RouterHarness(self, backend) as h:
            h.service._confirmation_prompts_pending.add("k9")
            op = self._finished_task(h, kind="confirm", result="Unlock the Entry Lock?",
                                     confirm_ids=("k9",))
            await h.service._task_complete(op)
            self.assertNotIn("k9", h.service._confirmation_prompts_pending, "withdrawn")
            self.assertEqual(op.result.kind, "failed", "it says it was not done")
            before = len(h.socket.of("session.commentary.append"))
            h.service._task_flush_again = False
            await h.service._cancel(h.service._task_flush_task)
            await asyncio.wait_for(h.service.close_session("idle"), 10)
            self.assertEqual(len(h.socket.of("session.commentary.append")), before,
                             "nothing is spoken into the closing session")
            self.assertEqual(h.service._confirmation_prompts_pending, set())

    async def test_a_short_unrouted_utterance_lets_a_queued_result_be_spoken(self):
        backend = FakeBackend(*([by_request] * 6))
        async with RouterHarness(self, backend) as h:
            self._finished_task(h)
            await h.user_says("hm")
            await asyncio.sleep(2.0)
            contents = [m["content"] for m in h.socket.of("session.commentary.append")]
            self.assertTrue(any("The Atrium Lamp is on." in c and "earlier request" in c
                                for c in contents), contents)


class RiskyActionsNeedAYes(Patched):
    def test_the_local_path_never_acts_on_locks_covers_or_alarms(self):
        names = [t["name"] for t in TOOLS]
        for domain in ("lock", "cover", "alarm_control_panel", "valve", "script"):
            self.assertIsNone(local_tool_call(LocalMatch("HassTurnOff", {"name": "x"}, [domain], 1), names),
                              domain)
        self.assertIsNone(local_tool_call(LocalMatch("HassTurnOn", {"name": "x"}, [], 0), names),
                          "no resolved target, no local action")
        self.assertEqual(local_tool_call(LocalMatch("HassTurnOn", {"name": "x"}, ["light"], 1), names),
                         ("intent__HassTurnOn", {"name": "x"}))

    async def test_an_unlock_is_held_for_a_spoken_yes_and_said_as_a_question(self):
        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))
        backend = FakeBackend(
            response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
            response(message("Do you want me to unlock the Entry Lock?")),
        )
        async with RouterHarness(self, backend, gate=gate) as h:
            await h.user_says("Unlock the Entry Lock.")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [], "nothing reaches the lock before a yes")
            self.assertIn("Nothing has been done yet", sent[0]["content"])
            self.assertTrue(h.service._confirmation_prompts_pending)
            h.voice_says("Sure, one sec.")  # not the question: arms nothing yet
            await asyncio.sleep(0.2)
            self.assertTrue(h.service._confirmation_prompts_pending)
            h.socket.push({"type": "session.commentary.appended"})
            h.voice_says("Do you want me to unlock the Entry Lock?")
            await asyncio.sleep(0.2)
            self.assertFalse(h.service._confirmation_prompts_pending,
                             "the question itself arms the spoken-yes fence")

    async def test_after_a_spoken_yes_the_held_action_runs_exactly_once(self):
        from app.main import Application

        async def states():
            return STATES
        gate = ActionGate(directory=EntityDirectory(states))

        def confirm(body):
            outputs = [json.loads(i["output"]) for i in body["input"]
                       if i.get("type") == "function_call_output"]
            confirm_id = next(o["confirm_id"] for o in outputs if "confirm_id" in o)
            return response(call("confirm_action", {"confirm_id": confirm_id}, "c2"))

        backend = FakeBackend(
            response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
            response(message("Do you want me to unlock the Entry Lock?")),
            confirm,
            response(message("The Entry Lock is unlocked.")),
        )
        async with RouterHarness(self, backend, gate=gate) as h:
            app = object.__new__(Application)
            app.action_gate = gate
            h.service.register_function("confirm_action",
                                        Application._create_confirm_handler(app, h.service))
            await h.user_says("Unlock the Entry Lock.")
            await h.wait_commentary()
            h.socket.push({"type": "session.commentary.appended"})
            h.voice_says("Do you want me to unlock the Entry Lock?")
            await asyncio.sleep(0.2)
            self.assertEqual(h.executed, [])
            await h.user_says("Yes.", start_ms=6000)
            sent = await h.wait_commentary(count=2)
            self.assertEqual(h.executed, [("intent__HassTurnOff", {"name": "Entry Lock"})],
                             "the held action ran once, after the yes")
            self.assertIn("Verified result", sent[1]["content"])


class CacheableRequestWithEveryTool(unittest.TestCase):
    def test_every_tool_in_a_stable_prefix_with_volatile_context_last(self):
        tools = TOOLS + [{"type": "function", "name": f"tool_{i}", "description": "d",
                          "parameters": {"type": "object", "properties": {}}} for i in range(39)]
        backend = BackendRouter("k", model="gpt-6-luna", instructions="Static.", tools=tools)
        router = TurnRouter(backend, context=lambda: "Current local date and time: noon.")
        one = backend.body(router._input("first"))
        two = backend.body(router._input("second"))
        self.assertEqual(len(one["tools"]), 44, "all 44 tools are offered on every call")
        self.assertEqual([t["name"] for t in one["tools"]], [t["name"] for t in tools])
        self.assertEqual(list(one)[:3], ["model", "instructions", "tools"])
        self.assertEqual(list(one)[-1], "input", "volatile content goes last")
        self.assertEqual({k: v for k, v in one.items() if k != "input"},
                         {k: v for k, v in two.items() if k != "input"},
                         "the prefix is byte-identical between calls")
        self.assertNotIn("noon", one["instructions"])
        self.assertEqual(one["input"][-2]["role"], "developer")
        self.assertTrue(one["parallel_tool_calls"])
        self.assertEqual(one["service_tier"], "priority")
        self.assertEqual(one["reasoning"], {"effort": "none"})
        self.assertTrue(one["prompt_cache_key"])
        self.assertFalse(one["store"])


class StreamingAndLifecycle(Patched):
    async def test_post_result_audio_streams_as_it_arrives(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the list?")
            await h.wait_commentary()
            h.socket.push({"type": "session.commentary.appended"})
            h.socket.push({"type": "session.output_transcript.delta", "delta": "Milk"})
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(fx.tone(100))})
            await h.sink.wait_for(TTSAudioRawFrame, timeout=1.0)
            self.assertEqual(h.heard(), fx.tone(100),
                             "the first 100 ms plays without waiting for the end of the reply")

    async def test_the_session_is_busy_while_a_result_is_pending(self):
        backend = FakeBackend(response(message("Milk.")))
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await h.user_says("What's on the list?")
            await asyncio.sleep(0.2)
            self.assertTrue(h.service.backend_work_active, "no idle close under a pending result")
            self.assertGreater(h.service.turn_liveness.in_flight, 0, "no thinking-watchdog force idle")
            backend.gate.set()
            await h.wait_commentary()
            h.socket.push({"type": "session.commentary.appended"})
            await asyncio.sleep(0.1)
            self.assertFalse(h.service.backend_work_active)

    async def test_the_fallback_clip_plays_only_when_nothing_was_heard(self):
        backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
        backend.gate.clear()
        with patch.object(live_service_module, "FALLBACK_ACK_AFTER_S", 0.2):
            async with RouterHarness(self, backend) as h:
                fired = []

                async def clip(device_id):
                    fired.append(device_id)
                    return True
                h.service.on_fallback_ack = clip
                with self.assertLogs("app.live_service", level="WARNING") as logs:
                    await h.user_says("What's on the list?")
                    await asyncio.sleep(0.6)
                self.assertEqual(fired, ["kitchen"])
                self.assertTrue(any("fallback acknowledgement fired" in m for m in logs.output))
                backend.gate.set()
                await h.wait_commentary()
            backend = FakeBackend(response(call("todo__get_items", {}, "c1")), response(message("Milk.")))
            backend.gate.clear()
            async with RouterHarness(self, backend) as h:
                fired = []

                async def clip2(device_id):
                    fired.append(device_id)
                    return True
                h.service.on_fallback_ack = clip2
                await h.user_says("What's on the list?")
                await asyncio.sleep(0.06)
                h.voice_says("Checking.", ms=300)
                await asyncio.sleep(0.6)
                self.assertEqual(fired, [], "the voice acknowledged; no clip on top of it")
                backend.gate.set()
                await h.wait_commentary()

    async def test_each_session_starts_with_the_current_instructions(self):
        stamps = iter(["10:00", "10:05"])
        backend = FakeBackend()
        async with RouterHarness(self, backend) as h:
            h.service.instructions = lambda: f"Current local date and time: {next(stamps)}."
            self.assertIn("10:00", h.service._session_start_event()["session"]["instructions"])
            self.assertIn("10:05", h.service._session_start_event()["session"]["instructions"])


class IntentRecognizerWire(unittest.IsolatedAsyncioTestCase):
    async def test_a_debug_match_is_parsed_and_nothing_is_executed(self):
        sent = []

        class Socket:
            def __init__(self):
                self.replies = [json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_ok"})]

            async def recv(self):
                return self.replies.pop(0)

            async def send(self, data):
                payload = json.loads(data)
                sent.append(payload)
                if payload.get("type") == "conversation/agent/homeassistant/debug":
                    self.replies.append(json.dumps({"id": payload["id"], "type": "result", "success": True,
                                                    "result": {"results": [{
                                                        "intent": {"name": "HassTurnOn"},
                                                        "slots": {"name": "Atrium Lamp"},
                                                        "targets": {"light.atrium": {"matched": True}},
                                                        "match": True, "unmatched_slots": {}}]}}))

            async def close(self):
                pass

        async def connect(url):
            return Socket()

        recognizer = HaIntentRecognizer("token", connect=connect)
        match = await recognizer.recognize("turn on the atrium lamp")
        self.assertEqual((match.intent, match.slots, match.domains, match.targets),
                         ("HassTurnOn", {"name": "Atrium Lamp"}, ["light"], 1))
        self.assertEqual([m["type"] for m in sent], ["auth", "conversation/agent/homeassistant/debug"],
                         "recognition only: the debug command never runs the intent")
        self.assertIsNone(await HaIntentRecognizer("").recognize("x"))


if __name__ == "__main__":
    unittest.main()
