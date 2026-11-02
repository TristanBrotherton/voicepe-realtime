"""Router decisions made from structured facts: what Home Assistant parsed,
what the tools reported, what ran. The shapes are the real failure cases from
the test device; the home here is fictional.
"""
import asyncio
import json
import unittest
import unittest.mock

from app.action_gate import ActionGate, EntityDirectory
from app.ha_household import parse_overview
from app.ha_names import entities_from_overview, resolve_target
from app.live_router import (
    ACTION, CONFIRM, LOOKUP, REPLY, BackendRouter, HaIntentRecognizer, LocalMatch, TurnRouter,
    is_clear_command, is_misheard_question_shape, is_wide_effect, local_state_answer, strip_lead_filler,
)
from app.tool_results import classify_result

from tests.test_live_router import FakeBackend, Patched, RouterHarness, call, message, response

TOOLS = [
    {"type": "function", "name": "intent__HassTurnOn", "description": "On.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"type": "function", "name": "intent__HassTurnOff", "description": "Off.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"type": "function", "name": "homeassistant__GetLiveContext", "description": "Read.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}},
]


class Recognizer:
    available = True

    def __init__(self, *matches, heard=None):
        self.matches = list(matches)
        self.sentences = []
        self.last_heard = heard

    async def recognize(self, text, device_id=None):
        self.sentences.append(text)
        return self.matches.pop(0) if self.matches else None


def router_with(backend=None, recognizer=None, **kw):
    backend = backend or FakeBackend()
    return TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post),
                      recognizer=recognizer, **kw)


def running(results=None):
    """An execute() that records calls and answers from ``results`` by tool."""
    executed = []

    async def execute(name, args, **kwargs):
        executed.append((name, dict(args), kwargs))
        raw = (results or {}).get(name.rsplit("__", 1)[-1], {"success": True})
        if callable(raw):
            raw = raw(args)
        return raw, classify_result(raw)
    return execute, executed


class LeadingConnective(unittest.IsolatedAsyncioTestCase):
    """Device regression on 0.20.12: "And turn off the test light" missed Home
    Assistant's local match, because its templates start at the verb."""

    def test_the_connective_goes_and_nothing_else(self):
        for said, want in (
            ("And turn off the test light", "turn off the test light"),
            ("also, turn off the test light.", "turn off the test light."),
            ("Then turn on the fan", "turn on the fan"),
            ("And then turn on the fan", "turn on the fan"),
            ("OK so what time is it", "what time is it"),
            ("Turn off Andy's lamp", "Turn off Andy's lamp"),
            ("Android tablet off", "Android tablet off"),
            ("and", "and"),
        ):
            self.assertEqual(strip_lead_filler(said), want, said)

    async def test_it_runs_locally_without_the_backend(self):
        recognizer = Recognizer(LocalMatch("HassTurnOff", {"name": "test light"}, ["light"], 1))
        backend = FakeBackend()
        execute, executed = running()
        result = await router_with(backend, recognizer).route("And turn off the test light", execute)
        self.assertEqual(recognizer.sentences, ["turn off the test light"])
        self.assertEqual((result.kind, result.source), (ACTION, "ha_local"))
        self.assertEqual(executed[0][0], "intent__HassTurnOff")
        self.assertEqual(backend.bodies, [])


class StateQuestionAnsweredLocally(unittest.IsolatedAsyncioTestCase):
    """Home Assistant parses "Is the test light on?" as HassGetState with one
    target and compares the state itself; the backend is not needed."""

    def get_state(self, matches, name="test light", state="on", targets=1):
        return LocalMatch("HassGetState", {"name": name, "state": state}, ["light"], targets, matches)

    async def test_yes_and_no_without_the_backend_or_any_tool(self):
        for matches, want in ((True, "The test light is on."), (False, "The test light is not on.")):
            backend = FakeBackend()
            execute, executed = running()
            result = await router_with(backend, Recognizer(self.get_state(matches))).route(
                "Is the test light on?", execute)
            self.assertEqual((result.kind, result.source, result.text), (LOOKUP, "ha_local", want))
            self.assertTrue(result.verified)
            self.assertEqual((backend.bodies, executed), ([], []))
            self.assertIn("Verified result", result.commentary())

    async def test_a_follow_up_has_the_answer_in_its_history(self):
        backend = FakeBackend(response(message("It is.")))
        router = router_with(backend, Recognizer(self.get_state(True), None))
        execute, _ = running()
        await router.route("Is the test light on?", execute)
        await router.route("how long has that been", execute)
        contents = [i.get("content") for i in backend.bodies[0]["input"]]
        self.assertIn("The test light is on.", contents)

    def test_what_it_will_not_answer(self):
        for match in (
            None,
            LocalMatch("HassGetState", {"name": "test light"}, ["light"], 1, True),  # no state to compare
            self.get_state(True, targets=2),
            self.get_state(None),
            LocalMatch("HassGetState", {"state": "on"}, ["light"], 1, True),  # room context, no name
            LocalMatch("HassTurnOn", {"name": "lamp", "state": "on"}, ["light"], 1, True),
        ):
            self.assertEqual(local_state_answer(match), "", match)

    async def test_reserved_reads_go_through_the_guarded_backend(self):
        backend = FakeBackend(response(message("It is on.")))
        execute, _ = running()
        result = await router_with(backend, Recognizer(self.get_state(True)),
                                   local_state_reads=False).route("Is the test light on?", execute)
        self.assertEqual(result.source, "backend")
        self.assertEqual(len(backend.bodies), 1)

    async def test_the_debug_reply_carries_the_comparison(self):
        class Socket:
            def __init__(self):
                self.replies = [json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_ok"})]

            async def recv(self):
                return self.replies.pop(0)

            async def send(self, data):
                payload = json.loads(data)
                if payload.get("type", "").endswith("/debug"):
                    self.replies.append(json.dumps({
                        "id": payload["id"], "type": "result", "success": True, "result": {"results": [{
                            "intent": {"name": "HassGetState"}, "match": True, "unmatched_slots": {},
                            "slots": {"name": "test light", "state": "on"},
                            "targets": {"light.live_test_light": {"matched": False}}}]}}))

            async def close(self):
                pass

        async def connect(url):
            return Socket()

        match = await HaIntentRecognizer("t", connect=connect).recognize("Is the test light on?")
        self.assertIs(match.state_matches, False)
        self.assertEqual(local_state_answer(match), "The test light is not on.")


NOT_FOUND = {"error": "No exposed entities matched name 'x'"}


class LiveContextNameMiss(unittest.IsolatedAsyncioTestCase):
    async def route(self, first_args, read):
        backend = FakeBackend(response(call("homeassistant__GetLiveContext", first_args, "c1")),
                              response(message("It is off.")))
        execute, executed = running({"GetLiveContext": read})
        result = await router_with(backend).route("Is the test light on?", execute)
        return result, [e[1] for e in executed]

    async def test_a_name_that_matched_nothing_reads_the_area_instead(self):
        def read(args):
            return NOT_FOUND if "name" in args else {"result": "- names: Live Test Light\n  state: 'off'"}
        result, calls = await self.route({"name": "Test Light", "area": "Lab", "domain": ["light"]}, read)
        self.assertEqual(calls, [{"name": "Test Light", "area": "Lab", "domain": ["light"]}, {"area": "Lab"}])
        self.assertEqual(result.kind, LOOKUP)

    async def test_without_an_area_it_reads_the_domain(self):
        def read(args):
            return NOT_FOUND if "name" in args else {"result": "- names: Live Test Light"}
        _, calls = await self.route({"name": "Test Light", "domain": ["light"]}, read)
        self.assertEqual(calls[1], {"domain": ["light"]})

    async def test_it_never_reads_everything(self):
        _, calls = await self.route({"name": "Test Light"}, NOT_FOUND)
        self.assertEqual(len(calls), 1)

    async def test_a_second_miss_reports_the_first(self):
        result, calls = await self.route({"name": "Test Light", "area": "Lab"}, NOT_FOUND)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.tools[0].kind, "no-matching-entity")


PROMPT = """\
- names: Lab Live Test Light, test light
  domain: light
  areas: Lab Test, Test Room
- names: Lab Test Switch, test switch
  domain: input_boolean
  areas: Lab Test, Test Room
- names: Hall Lamp
  domain: light
  areas: Hall
- names: Hall Sconce
  domain: light
  areas: Hall
"""
ENTITIES = entities_from_overview(parse_overview(PROMPT))


class RoomNameInsideADeviceName(unittest.TestCase):
    """The backend asked for "Lab Test Light" and the area is called "Lab
    Test": it read as "that room's lights" and went to Home Assistant as it
    was, which matched nothing."""

    def resolve(self, name, tool="homeassistant__GetLiveContext"):
        return resolve_target(tool, {"name": name}, ENTITIES)[0]["name"]

    def test_the_device_that_holds_every_word(self):
        self.assertEqual(self.resolve("Lab Test Light"), "Lab Live Test Light")
        self.assertEqual(self.resolve("Lab Test Light", "intent__HassTurnOff"), "Lab Live Test Light")
        self.assertEqual(self.resolve("Lab Test Switch"), "Lab Test Switch")

    def test_otherwise_it_stays_a_room(self):
        self.assertEqual(self.resolve("Hall Light"), "Hall Light")
        self.assertEqual(self.resolve("Test Room Light"), "Test Room Light")


class ShortEntityNameDoesNotRewriteTheDomain(unittest.IsolatedAsyncioTestCase):
    async def gate(self, *states):
        async def fetch():
            return [{"entity_id": e, "attributes": {"friendly_name": n}} for e, n in states]
        return ActionGate(directory=EntityDirectory(fetch))

    async def test_a_switch_called_light_does_not_claim_every_light(self):
        """Why the backend's HassTurnOff missed: domain "light" became "switch"."""
        gate = await self.gate(("switch.plug_7", "Light"), ("light.other", "Other Lamp"))
        args = {"name": "Lab Test Light", "domain": ["light"]}
        self.assertEqual(await gate.reconcile_arguments("intent__HassTurnOff", args), args)

    async def test_a_real_name_still_corrects_the_domain(self):
        gate = await self.gate(("input_boolean.lab", "Lab Test Switch"))
        for spoken in ("Lab Test Switch", "test switch"):
            out = await gate.reconcile_arguments("intent__HassTurnOff", {"name": spoken, "domain": ["switch"]})
            self.assertEqual(out["domain"], ["input_boolean"], spoken)

    async def test_the_safety_check_still_sees_a_part_of_the_name(self):
        gate = await self.gate(("lock.front", "Front Door"))
        decision = await gate.check("intent__HassTurnOff", {"name": "the front door lock please"})
        self.assertTrue(decision.requires_confirmation)


BROADCAST = "assist_satellite__HassBroadcast"
HELD = {"status": "confirmation_required", "confirm_id": "abc", "action": "x", "instructions": "ask"}
CLEAN_BROADCAST = {"intent": "HassBroadcast", "slots": {"message": "dinner is ready"}, "targets": 0, "unmatched": []}


class WideToolsClearCommandElseConfirm(unittest.IsolatedAsyncioTestCase):
    def test_what_counts_as_wide(self):
        for name, args in ((BROADCAST, {"message": "x"}), ("intent__HassBroadcast", {}),
                           ("intent__HassTurnOff", {"domain": ["light"]}), ("intent__HassTurnOff", {}),
                           ("script__whole_house_off", {}), ("script__announce_dinner", {})):
            self.assertTrue(is_wide_effect(name, args), (name, args))
        for name, args in (("intent__HassTurnOff", {"name": "lamp"}), ("intent__HassTurnOff", {"area": "Hall"}),
                           ("intent__HassTurnOff", {"floor": "Upstairs", "domain": ["light"]}),
                           ("homeassistant__GetLiveContext", {}), ("intent__HassListAddItem", {"item": "x"}),
                           ("confirm_action", {"confirm_id": "x"}), ("web_search", {"query": "x"}),
                           ("intent__HassMediaNext", {}), ("intent__HassVacuumStart", {}),
                           ("ask_openclaw", {"question": "x"})):
            self.assertFalse(is_wide_effect(name, args), (name, args))

    def test_clear_or_not(self):
        for text in ("Announce dinner is ready", "announce that dinner is ready.", "Can you announce dinner's ready?",
                     "Please broadcast that we're leaving", "Tell everyone dinner is ready",
                     "Let the family know dinner is ready", "OK, announce it",
                     "Say dinner is ready on all the speakers"):
            self.assertTrue(is_clear_command(BROADCAST, text, None), text)
        for text in ("Test the placement. One, two, three, four, five", "Testing the placement",
                     "Dinner is ready", "What did the announcement say", "We should announce it sometime",
                     "Tell me a joke", "Say that again"):
            self.assertFalse(is_clear_command(BROADCAST, text, None), text)
        self.assertTrue(is_clear_command(BROADCAST, "whatever it heard", CLEAN_BROADCAST))
        self.assertFalse(is_clear_command(BROADCAST, "Test the placement",
                                          {"intent": "HassBroadcast", "unmatched": ["message"]}))
        off = "intent__HassTurnOff"
        for text in ("Turn off all the lights", "Please turn everything off", "Could you shut everything off?"):
            self.assertTrue(is_clear_command(off, text, None), text)
        for text in ("Turned off the lights", "The lights are all on", "Everything off"):
            self.assertFalse(is_clear_command(off, text, None), text)

    async def route(self, text, *responses, heard=None, results=None):
        backend = FakeBackend(*responses)
        recognizer = Recognizer(heard=heard) if heard else None
        execute, executed = running(results)
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS + [
            {"type": "function", "name": BROADCAST, "description": "Say.",
             "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}}], post=backend.post),
            recognizer=recognizer)
        return await router.route(text, execute), executed

    async def test_a_clear_announcement_runs_straight_away(self):
        result, executed = await self.route(
            "Announce dinner is ready", response(call(BROADCAST, {"message": "dinner is ready"}, "c1")),
            response(message("Announced.")), heard=CLEAN_BROADCAST)
        self.assertEqual(executed, [(BROADCAST, {"message": "dinner is ready"}, {})])
        self.assertEqual(result.kind, ACTION)

    async def test_an_unclear_one_asks_first(self):
        """The issue's case: a mistranscribed test phrase reached every speaker."""
        result, executed = await self.route(
            "Test the placement. One, two, three, four, five",
            response(call(BROADCAST, {"message": "one two three"}, "c1")),
            response(message("The placement test was broadcast.")), results={"HassBroadcast": HELD})
        self.assertEqual(executed[0][2], {"confirm": "announce that"})
        self.assertEqual((result.kind, result.text), (CONFIRM, "Do you want me to announce that?"))
        self.assertIn("Nothing has been done yet", result.commentary())

    async def test_a_state_question_cannot_reach_it_at_all(self):
        result, executed = await self.route(
            "Is the announcement on", response(call(BROADCAST, {"message": "x"}, "c1")),
            response(message("No.")))
        self.assertEqual(executed, [])

    async def test_without_a_gate_it_is_refused_and_asked(self):
        from app.live_router import READ_ONLY_REFUSED, unclear_refusal
        raw, outcome = unclear_refusal("announce that")
        self.assertIn("do you want me to announce that?", raw["error"])
        self.assertEqual((outcome.ok, outcome.kind), (False, READ_ONLY_REFUSED))


MISHEARD = {"intent": "HassTurnOn", "slots": {"name": "voice PE test switch"}, "targets": 0, "unmatched": ["area"]}


class MisheardQuestionDoesNotSwitch(unittest.IsolatedAsyncioTestCase):
    """"Is the Voice PE test switch on?" heard as "And just the voice PE test
    switch on": Home Assistant found the device; the rest is no room."""

    def test_the_shape(self):
        self.assertTrue(is_misheard_question_shape("And just the voice PE test switch on", MISHEARD))
        for text in ("Just turn the test switch on", "Please just the test switch on",
                     "Can you just the test switch on"):
            self.assertFalse(is_misheard_question_shape(text, MISHEARD), text)
        self.assertFalse(is_misheard_question_shape("just the switch on", None))
        self.assertFalse(is_misheard_question_shape("just the switch on", dict(MISHEARD, unmatched=["name"])))
        self.assertFalse(is_misheard_question_shape("just the switch on", dict(MISHEARD, unmatched=[])))
        self.assertFalse(is_misheard_question_shape("just the switch on", dict(MISHEARD, intent="HassGetState")))

    async def test_the_control_call_is_held_for_a_yes(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Voice PE Test Switch"}, "c1")),
                              response(message("It's on.")))
        execute, executed = running({"HassTurnOn": HELD})
        result = await router_with(backend, Recognizer(heard=MISHEARD)).route(
            "And just the voice PE test switch on", execute)
        self.assertEqual(executed[0][2], {"confirm": "turn on Voice PE Test Switch"})
        self.assertEqual((result.kind, result.text), (CONFIRM, "Do you want me to turn on Voice PE Test Switch?"))

    async def test_a_read_passes(self):
        backend = FakeBackend(response(call("homeassistant__GetLiveContext", {"area": "Lab"}, "c1")),
                              response(message("It's on.")))
        execute, executed = running({"GetLiveContext": {"result": "- names: Test Switch\n  state: 'on'"}})
        result = await router_with(backend, Recognizer(heard=MISHEARD)).route("just the test switch on", execute)
        self.assertEqual(executed[0][2], {})
        self.assertEqual(result.kind, LOOKUP)

    async def test_a_clear_command_with_a_wildcard_parse_is_untouched(self):
        backend = FakeBackend(response(call("intent__HassTurnOn", {"name": "Test Switch"}, "c1")),
                              response(message("It's on.")))
        execute, executed = running()
        result = await router_with(backend, Recognizer(heard=MISHEARD)).route("Turn on the test switch in there", execute)
        self.assertEqual(executed[0][2], {})
        self.assertEqual(result.kind, ACTION)


class HeldThroughTheRealGate(Patched):
    """End to end: the guarded path holds it, a spoken yes runs it once."""

    STATES = [{"entity_id": "light.foyer", "attributes": {"friendly_name": "Foyer Lamp"}}]

    async def gate(self):
        async def states():
            return self.STATES
        return ActionGate(directory=EntityDirectory(states))

    async def test_nothing_runs_until_the_yes_then_once(self):
        from app.main import Application

        def confirm(body):
            outputs = [json.loads(i["output"]) for i in body["input"] if i.get("type") == "function_call_output"]
            confirm_id = next(o["confirm_id"] for o in outputs if "confirm_id" in o)
            return response(call("confirm_action", {"confirm_id": confirm_id}, "c2"))

        gate = await self.gate()
        backend = FakeBackend(
            response(call("intent__HassTurnOff", {"domain": ["light"]}, "c1")),
            response(message("All the lights are off.")),  # the backend's words are not used
            confirm, response(message("All the lights are off.")))
        async with RouterHarness(self, backend, gate=gate) as h:
            app = object.__new__(Application)
            app.action_gate = gate
            h.service.register_function("confirm_action", Application._create_confirm_handler(app, h.service))
            await h.user_says("The lights are all on.")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [], "held")
            self.assertIn("Do you want me to turn off every light in the house?", sent[0]["content"])
            self.assertEqual(h.service._forced_confirmations, {})
            h.socket.push({"type": "session.commentary.appended"})
            h.voice_says("Do you want me to turn off the lights everywhere?")
            await asyncio.sleep(0.2)
            await h.user_says("Yes.", start_ms=6000)
            sent = await h.wait_commentary(count=2)
            self.assertEqual(h.executed, [("intent__HassTurnOff", {"domain": ["light"]})], "ran once, after the yes")
            self.assertIn("Verified result", sent[1]["content"])

    async def test_a_clear_command_is_not_held(self):
        gate = await self.gate()
        backend = FakeBackend(response(call("intent__HassTurnOff", {"domain": ["light"]}, "c1")),
                              response(message("All the lights are off.")))
        async with RouterHarness(self, backend, gate=gate) as h:
            await h.user_says("Turn off all the lights.")
            await h.wait_commentary()
            self.assertEqual(h.executed, [("intent__HassTurnOff", {"domain": ["light"]})])

    async def test_a_lock_flow_is_unchanged(self):
        async def states():
            return [{"entity_id": "lock.front", "attributes": {"friendly_name": "Entry Lock"}}]
        backend = FakeBackend(response(call("intent__HassTurnOff", {"name": "Entry Lock"}, "c1")),
                              response(message("Do you want me to unlock the Entry Lock?")))
        async with RouterHarness(self, backend, gate=ActionGate(directory=EntityDirectory(states))) as h:
            await h.user_says("Unlock the Entry Lock.")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [])
            self.assertIn("Do you want me to unlock the Entry Lock?", sent[0]["content"])

    async def test_without_a_gate_it_is_refused_unrun(self):
        backend = FakeBackend(response(call("intent__HassTurnOff", {"domain": ["light"]}, "c1")),
                              response(message("Do you want me to turn off every light?")))
        async with RouterHarness(self, backend) as h:
            await h.user_says("The lights are all on.")
            sent = await h.wait_commentary()
            self.assertEqual(h.executed, [])
            self.assertIn("Do you want me to turn off every light?", sent[0]["content"])
            self.assertIn("nothing has been done", sent[0]["content"])


class GuessAfterACompleteAcknowledgement(Patched):
    """Seen on a test device: "On it. But they're off." for a command that
    turned them on. The gate had seen a complete acknowledgement when the
    result went in, so what the voice said next played as the answer."""

    def route_a_command(self):
        return FakeBackend(response(call("intent__HassTurnOn", {"name": "Foyer Lamp"}, "c1")),
                           response(message("The Foyer Lamp is on.")))

    async def result_is_in(self, h, ack="On it.", ms=100):
        await h.user_says("Turn on the foyer lamp.")
        await asyncio.sleep(0.1)
        h.voice_says(ack, ms=ms)
        await asyncio.sleep(0.15)
        h.backend.gate.set()
        await h.wait_commentary()
        await asyncio.sleep(0.02)

    async def test_the_guess_is_dropped_and_the_answer_plays(self):
        backend = self.route_a_command()
        backend.gate.clear()
        async with RouterHarness(self, backend) as h:
            await self.result_is_in(h)
            before = len(h.heard())
            self.assertGreater(before, 0, "the acknowledgement was heard")
            h.voice_says(" But they're off.", ms=300)
            await asyncio.sleep(0.15)
            self.assertEqual(len(h.heard()), before, "the guess is not heard")
            h.socket.push({"type": "session.commentary.appended"})
            await asyncio.sleep(0.02)
            h.voice_says("It's on.", ms=300)
            await asyncio.sleep(0.15)
            self.assertGreater(len(h.heard()), before, "the answer is heard")
            self.assertEqual(h.service._post_text, "It's on.", "the guess does not count as the answer")

    async def test_the_acknowledgements_own_tail_still_plays(self):
        from app import live_router
        backend = self.route_a_command()
        backend.gate.clear()
        with unittest.mock.patch.object(live_router, "KNOWN_NAMES", {"sam"}):
            async with RouterHarness(self, backend) as h:
                await self.result_is_in(h, "One moment")
                before = len(h.heard())
                h.voice_says(", Sam.", ms=200)
                await asyncio.sleep(0.15)
                self.assertGreater(len(h.heard()), before, "the rest of the phrase is not cut")
                self.assertFalse(h.service._gate_drop_until_ack)

    async def test_a_delegation_during_the_send_does_not_repeat_the_answer(self):
        backend = FakeBackend(response(message("It is noon.")))
        async with RouterHarness(self, backend) as h:
            sends = []
            real = h.service._send_commentary

            async def slow_send(text, did):
                sends.append(text)
                if len(sends) == 1:
                    await h.service._router_on_delegation("d1", "client")
                await real(text, did)
            h.service._send_commentary = slow_send
            await h.user_says("Could you tell me the plan?")
            await h.wait_commentary(count=2)
            self.assertIn("It is noon.", sends[0])
            self.assertIn("already been answered", sends[1])
            self.assertNotIn("It is noon.", sends[1])


def report(*success, failed=()):
    return json.dumps({"speech": {}, "response_type": "action_done",
                       "data": {"success": list(success), "failed": list(failed)}})


def entity(name, entity_id):
    return {"name": name, "type": "entity", "id": entity_id}


class RepliesComeFromWhatRan(unittest.IsolatedAsyncioTestCase):
    """"Turn on the test switch in the test room", heard as "and the test
    room": the switch turned on and the reply doubted it, or named the room as
    a second device."""

    SWITCH = report(entity("Lab Test Switch", "input_boolean.lab_test_switch"))

    def test_the_fact(self):
        from app.live_router import action_fact
        self.assertEqual(action_fact("intent__HassTurnOn", self.SWITCH), "Turned on Lab Test Switch.")
        self.assertEqual(action_fact("HassTurnOff", json.loads(self.SWITCH)), "Turned off Lab Test Switch.")
        lamps = [entity("A", "light.a"), entity("B", "light.b")]
        self.assertEqual(action_fact("intent__HassTurnOff", report(*lamps)), "Turned off A and B.")
        self.assertEqual(action_fact("intent__HassTurnOff", report({"name": "Hall", "type": "area", "id": "hall"}, *lamps)),
                         "Turned off 2 devices in Hall.")

    def test_no_fact_when_it_does_not_say_or_reads_wrong(self):
        from app.live_router import action_fact
        for name, result in (
            ("intent__HassTurnOn", {"success": True}),
            ("intent__HassTurnOn", "not json"),
            ("intent__HassTurnOn", report()),
            ("intent__HassTurnOn", report(entity("A", "light.a"), failed=[entity("B", "light.b")])),
            ("intent__HassTurnOn", report(entity("Gate", "cover.gate"))),
            ("intent__HassTurnOff", report(entity("Door", "lock.door"))),
            ("intent__HassLightSet", self.SWITCH),
            ("homeassistant__GetLiveContext", self.SWITCH),
        ):
            self.assertEqual(action_fact(name, result), "", result)

    async def route(self, text, *responses, results=None):
        router = router_with(FakeBackend(*responses))
        execute, _ = running(results or {"HassTurnOn": self.SWITCH, "HassTurnOff": self.SWITCH})
        return await router.route(text, execute), router

    async def test_a_doubting_reply_does_not_undo_what_ran(self):
        result, router = await self.route(
            "Turn on the test switch and the test room",
            response(call("intent__HassTurnOn", {"name": "test switch"}, "c1")),
            response(message("I'm not sure which switch you mean.")))
        self.assertEqual((result.kind, result.text), (ACTION, "Turned on Lab Test Switch."))
        self.assertIn("Turned on Lab Test Switch.", result.commentary())
        self.assertEqual(router.last_assistant(), "Turned on Lab Test Switch.")

    async def test_a_garbled_reply_is_replaced_too(self):
        result, _ = await self.route(
            "Turn off the test light and the test room",
            response(call("intent__HassTurnOff", {"name": "test light"}, "c1")),
            response(message("The test light and the test room are off.")))
        self.assertEqual(result.text, "Turned off Lab Test Switch.")

    async def test_a_read_in_the_mix_keeps_the_backends_answer(self):
        result, _ = await self.route(
            "Turn off the lamp and is the door locked",
            response(call("intent__HassTurnOff", {"name": "lamp"}, "c1"),
                     call("homeassistant__GetLiveContext", {"name": "door"}, "c2")),
            response(message("The lamp is off and the door is locked.")),
            results={"HassTurnOff": self.SWITCH, "GetLiveContext": {"result": "locked"}})
        self.assertEqual(result.text, "The lamp is off and the door is locked.")

    async def test_a_report_with_no_data_keeps_the_backends_answer(self):
        result, _ = await self.route("Turn on the lamp", response(call("intent__HassTurnOn", {"name": "lamp"}, "c1")),
                                     response(message("The lamp is on.")), results={"HassTurnOn": {"success": True}})
        self.assertEqual(result.text, "The lamp is on.")


class FakeHousehold:
    class Room:
        def __init__(self, name):
            self.name = name

    def __init__(self, *rooms):
        self.rooms = [self.Room(r) for r in rooms]

    def spoken_room(self, text):
        from app.ha_household import mentions
        return next((r for r in self.rooms if mentions(text, r.name)), None)

    def room_of(self, ip):
        return None

    def device_id_of(self, ip):
        return None

    def resolve_target(self, name, args):
        return args, None


class AndTheRoom(unittest.IsolatedAsyncioTestCase):
    MATCH = LocalMatch("HassTurnOn", {"name": "test switch", "area": "test room"}, ["input_boolean"], 1)

    async def route(self, text, *matches, backend=None):
        recognizer = Recognizer(*matches)
        backend = backend or FakeBackend(response(message("OK?")))
        router = router_with(backend, recognizer, household=FakeHousehold("Test Room", "Hall"))
        execute, executed = running()
        return await router.route(text, execute), recognizer, executed

    async def test_heard_as_and_it_runs_locally(self):
        result, recognizer, executed = await self.route("Turn on the test switch and the test room", None, self.MATCH)
        self.assertEqual(recognizer.sentences, ["Turn on the test switch and the test room",
                                                "Turn on the test switch in the test room"])
        self.assertEqual(result.source, "ha_local")
        self.assertEqual(executed[0][1], {"name": "test switch", "area": "test room"})

    async def test_two_real_requests_are_not_merged_into_a_wrong_target(self):
        result, recognizer, executed = await self.route("Turn on the lamp and the hall", None, None)
        self.assertEqual(len(recognizer.sentences), 2)
        self.assertEqual(result.source, "backend")
        self.assertEqual(executed, [])

    async def test_not_a_room_is_not_asked_again(self):
        _, recognizer, _ = await self.route("Turn on the lamp and the fan", None)
        self.assertEqual(len(recognizer.sentences), 1)


class SelfCorrectionInAListRequest(unittest.IsolatedAsyncioTestCase):
    """What Home Assistant really parsed: the item is everything up to the last
    "to the ... list", the list is the corrected one."""

    TOOLS = TOOLS + [{"type": "function", "name": "todo__HassListAddItem", "description": "Add.",
                      "parameters": {"type": "object", "properties": {"item": {"type": "string"}}}}]
    GARBLED = LocalMatch("HassListAddItem", {"item": "oat bites to the shopping list Sorry add it", "name": "Hardware"},
                         ["todo"], 1)

    def router(self, backend, recognizer=None):
        return TurnRouter(BackendRouter("k", model="m", instructions="i", tools=self.TOOLS, post=backend.post),
                          recognizer=recognizer)

    def test_garbled_or_not(self):
        from app.live_router import is_garbled_item
        for item in ("oat bites to the shopping list Sorry add it", "milk to the test list actually add it",
                     "milk sorry eggs", "milk I mean eggs", "eggs no wait"):
            self.assertTrue(is_garbled_item(item), item)
        for item in ("milk", "oat bites", "mac and cheese", "sorrel", "waiter's friend", "bread", ""):
            self.assertFalse(is_garbled_item(item), item)

    async def test_it_does_not_run_locally_and_the_backend_carries_the_correction(self):
        backend = FakeBackend(
            response(call("todo__HassListAddItem", {"item": "Oat bites", "name": "Hardware"}, "c1")),
            response(message("Oat bites is on the hardware list.")))
        execute, executed = running()
        result = await self.router(backend, Recognizer(self.GARBLED)).route(
            "Add oat bites to the shopping list. Sorry, add it to the hardware list", execute)
        self.assertEqual(result.source, "backend")
        self.assertEqual(result.timings["local_refused"], "self_correction_in_item")
        self.assertEqual([e[1] for e in executed], [{"item": "Oat bites", "name": "Hardware"}])

    async def test_the_backend_is_told_and_cannot_add_it_garbled_either(self):
        from app.live_router import ROUTER_BACKEND_INSTRUCTIONS
        self.assertIn("corrects themselves", ROUTER_BACKEND_INSTRUCTIONS)
        backend = FakeBackend(
            response(call("todo__HassListAddItem", {"item": "oat bites to the shopping list sorry", "name": "Hardware"}, "c1")),
            response(call("todo__HassListAddItem", {"item": "Oat bites", "name": "Hardware"}, "c2")),
            response(message("Added.")))
        execute, executed = running()
        result = await self.router(backend).route("Add oat bites, sorry, to Hardware", execute)
        self.assertEqual([e[1]["item"] for e in executed], ["Oat bites"])
        self.assertEqual(result.kind, ACTION)

    async def test_a_plain_item_still_runs_locally(self):
        match = LocalMatch("HassListAddItem", {"item": "bread", "name": "test list"}, ["todo"], 1)
        execute, executed = running()
        result = await self.router(FakeBackend(), Recognizer(match)).route("Add bread to the test list", execute)
        self.assertEqual(result.source, "ha_local")
        self.assertEqual(executed[0][1]["item"], "bread")


if __name__ == "__main__":
    unittest.main()
