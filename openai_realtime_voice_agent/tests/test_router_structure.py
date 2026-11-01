"""Router decisions made from structured facts: what Home Assistant parsed,
what the tools reported, what ran. The shapes are the real failure cases from
the test device; the home here is fictional.
"""
import asyncio
import json
import unittest

from app.action_gate import ActionGate, EntityDirectory
from app.ha_household import parse_overview
from app.ha_names import entities_from_overview, resolve_target
from app.live_router import (
    ACTION, LOOKUP, BackendRouter, HaIntentRecognizer, LocalMatch, TurnRouter, local_state_answer,
    strip_lead_filler,
)
from app.tool_results import classify_result

from tests.test_live_router import FakeBackend, call, message, response

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
    """Garage regression on 0.20.12: "And turn off the test light" missed Home
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


if __name__ == "__main__":
    unittest.main()
