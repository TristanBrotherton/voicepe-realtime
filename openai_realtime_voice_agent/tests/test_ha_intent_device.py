"""candidate-v35 stream haintent.

1. The Home Assistant local intent request carries the asking satellite's
   device_id (so HA resolves room context), and the local path refuses an
   unnamed / domain-only target or more than eight targets: nothing runs
   locally, the backend (guarded path) gets the request, and if the backend
   is down the user hears "I couldn't tell which device you meant".
2. A tool-less (or read-only) backend answer to a read-only question is not
   turned into a failed action; the false-success protection for
   action-shaped requests is unchanged.
"""
import asyncio
import json
import unittest

from app.live_router import (
    ACTION, FAILED, LOOKUP, REPLY, REFUSED_TOO_MANY, REFUSED_UNNAMED, REFUSED_UNSAFE_DOMAIN,
    UNCLEAR_TARGET_TEXT, BackendRouter, HaIntentRecognizer, LocalMatch, TurnRouter,
    is_action_shaped, local_refusal, local_tool_call,
)
from app.tool_results import classify_result

from tests.test_live_router import TOOLS, FakeBackend, FakeRecognizer, call, message, response

NAMES = [t["name"] for t in TOOLS]
DEVICE = "satellite-a"


class FakeSocket:
    """A Home Assistant Core websocket that answers the debug command."""

    def __init__(self, replies):
        self.replies = list(replies)  # one per debug request: dict or callable(request)
        self.sent = []
        self.inbox = asyncio.Queue()
        self.inbox.put_nowait(json.dumps({"type": "auth_required"}))

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == "auth":
            await self.inbox.put(json.dumps({"type": "auth_ok"}))
            return
        reply = self.replies.pop(0)
        reply = reply(msg) if callable(reply) else reply
        await self.inbox.put(json.dumps(dict(reply, id=msg["id"])))

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        pass

    def debug_requests(self):
        return [m for m in self.sent if m.get("type") == "conversation/agent/homeassistant/debug"]


def matched(slots, targets):
    return {"success": True, "result": {"results": [{
        "match": True, "intent": {"name": "HassTurnOff"}, "slots": {k: {"value": v} for k, v in slots.items()},
        "targets": {t: {"matched": True} for t in targets}, "unmatched_slots": {}}]}}


def recognizer_for(sock):
    async def connect(url):
        return sock
    return HaIntentRecognizer("token", connect=connect)


class DeviceIdInTheIntentRequest(unittest.IsolatedAsyncioTestCase):
    async def test_the_debug_request_carries_the_satellite_device_id(self):
        sock = FakeSocket([matched({"area": "Atrium", "domain": "light"}, ["light.atrium"])])
        match = await recognizer_for(sock).recognize("Turn off the lights", device_id=DEVICE)
        self.assertEqual(sock.debug_requests()[0]["device_id"], DEVICE)
        self.assertEqual(match.slots.get("area"), "Atrium", "room context fills the area slot")

    async def test_an_older_core_without_device_context_is_asked_again_without_it(self):
        sock = FakeSocket([{"success": False, "error": {"code": "invalid_format", "message": "extra keys"}},
                           matched({"name": "Atrium Lamp"}, ["light.atrium"]),
                           matched({"name": "Atrium Lamp"}, ["light.atrium"])])
        rec = recognizer_for(sock)
        self.assertIsNotNone(await rec.recognize("Turn off the Atrium Lamp", device_id=DEVICE))
        await rec.recognize("Turn off the Atrium Lamp", device_id=DEVICE)
        sent = sock.debug_requests()
        self.assertEqual([("device_id" in m) for m in sent], [True, False, False])

    async def test_the_router_sends_its_device_id_with_every_recognition(self):
        rec = FakeRecognizer(LocalMatch("HassTurnOff", {"area": "Atrium", "domain": "light"}, ["light"], 2))
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS,
                                          post=FakeBackend().post),
                            recognizer=rec, device_id=DEVICE)
        executed = []

        async def execute(name, args):
            executed.append((name, args))
            raw = {"success": True}
            return raw, classify_result(raw)
        result = await router.route("Turn off the lights.", execute)
        self.assertEqual(rec.device_ids, [DEVICE])
        self.assertEqual(executed, [("intent__HassTurnOff", {"area": "Atrium", "domain": "light"})])
        self.assertEqual((result.kind, result.source), (ACTION, "ha_local"))


class LocalRefusals(unittest.IsolatedAsyncioTestCase):
    async def route(self, match, *responses, backend_down=False):
        backend = FakeBackend(*responses)

        async def down(body):
            raise RuntimeError("backend unreachable")
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS,
                                          post=down if backend_down else backend.post),
                            recognizer=FakeRecognizer(match), device_id=DEVICE)
        executed = []

        async def execute(name, args):
            executed.append(name)
            raw = {"success": True}
            return raw, classify_result(raw)
        result = await router.route("Turn off the lights.", execute)
        return result, executed, backend

    def test_reasons(self):
        self.assertEqual(local_refusal(LocalMatch("HassTurnOff", {"domain": "light"}, ["light"], 3), NAMES),
                         REFUSED_UNNAMED)
        self.assertEqual(local_refusal(LocalMatch("HassTurnOff", {"name": "  "}, ["light"], 1), NAMES),
                         REFUSED_UNNAMED, "a blank name is no name")
        self.assertEqual(local_refusal(LocalMatch("HassTurnOff", {"area": "x"}, ["light"], 9), NAMES),
                         REFUSED_TOO_MANY)
        self.assertIsNone(local_refusal(LocalMatch("HassTurnOff", {"area": "x"}, ["light"], 8), NAMES))
        for domain in ("lock", "cover", "alarm_control_panel", "valve", "script"):
            self.assertEqual(local_refusal(LocalMatch("HassTurnOff", {"name": "x"}, [domain], 1), NAMES),
                             REFUSED_UNSAFE_DOMAIN, domain)

    async def test_domain_only_is_refused_and_goes_to_the_backend(self):
        result, executed, backend = await self.route(
            LocalMatch("HassTurnOff", {"domain": "light"}, ["light"], 3),
            response(message("Which lights do you mean?")))
        self.assertEqual(executed, [], "nothing runs locally")
        self.assertEqual(result.timings.get("local_refused"), REFUSED_UNNAMED)
        self.assertEqual(len(backend.bodies), 1)
        self.assertEqual(result.kind, REPLY)

    async def test_nine_targets_are_refused_eight_allowed(self):
        result, executed, _ = await self.route(LocalMatch("HassTurnOff", {"area": "x"}, ["light"], 9),
                                               response(message("Which lights do you mean?")))
        self.assertEqual((executed, result.timings.get("local_refused")), ([], REFUSED_TOO_MANY))
        result, executed, backend = await self.route(LocalMatch("HassTurnOff", {"area": "x"}, ["light"], 8))
        self.assertEqual((executed, result.source, backend.bodies), (["intent__HassTurnOff"], "ha_local", []))

    async def test_an_unnamed_target_with_the_backend_down_says_which_device(self):
        result, executed, _ = await self.route(LocalMatch("HassTurnOff", {"domain": "light"}, ["light"], 3),
                                               backend_down=True)
        self.assertEqual(executed, [])
        self.assertEqual((result.kind, result.text), (FAILED, UNCLEAR_TARGET_TEXT))
        self.assertNotIn("Verified", result.commentary())

    def test_a_lock_is_still_never_local(self):
        self.assertIsNone(local_tool_call(LocalMatch("HassTurnOff", {"name": "Entry Lock"}, ["lock"], 1), NAMES))


class ReadOnlyAnswers(unittest.IsolatedAsyncioTestCase):
    async def route(self, user_text, *responses):
        backend = FakeBackend(*responses)
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post))

        async def execute(name, args):
            raw = {"items": ["milk"]} if name.endswith("get_items") else {"success": True}
            return raw, classify_result(raw)
        return await router.route(user_text, execute)

    async def test_a_tool_less_answer_to_a_question_is_not_a_failure(self):
        for question, answer in (
            ("Is the door locked?", "The front door is locked."),
            ("What's the temperature?", "It's 21 degrees, and the heating is set to 22."),
            ("What's on the list?", "Milk and eggs were added this morning."),
            ("Hey, is the door locked?", "The front door is locked."),
        ):
            result = await self.route(question, response(message(answer)))
            self.assertEqual(result.kind, REPLY, question)
            self.assertNotIn("not completed", result.commentary() or "", question)

    async def test_a_state_answer_after_a_read_is_a_lookup(self):
        result = await self.route("What's the temperature?", response(call("todo__get_items", {}, "c1")),
                                  response(message("It is now 21 degrees.")))
        self.assertEqual(result.kind, LOOKUP)

    async def test_action_shaped_requests_keep_the_false_success_protection(self):
        for request, answer in (
            ("Turn on the Atrium Lamp.", "Done, the lamp is on."),
            ("Atrium lights off.", "Turned the atrium lights off."),
            ("Can you lock the door?", "The door is locked."),
            ("Is the lamp on? Turn it off.", "The lamp is turned off."),
            ("Is the door locked and lock it", "The door is locked."),
            ("Did you turn off the lamp?", "Yes, I turned it off."),
            ("What's on the list?", "I've added milk."),
            # haintent review B1: tool-less replies to comma-joined and
            # indirect requests, and "Did you / Have" questions.
            ("Did you turn off the lights", "They're turned off."),
            ("Did you add milk?", "Milk was added."),
            ("Have the lights turned off", "The lights are turned off."),
            ("When you get a chance, turn off the lights", "The lights are turned off."),
            ("What about turning off the lights", "The lights are turned off."),
            ("Is there any way you can turn off the lights", "The lights are turned off."),
            ("Which lights are on, turn them all off", "The lights are now off."),
            ("Are the lights on?", "The lights are now off."),
        ):
            result = await self.route(request, response(message(answer)))
            self.assertEqual(result.kind, FAILED, request)
            self.assertIn("do not say it was done", result.commentary(), request)

    async def test_a_read_still_cannot_verify_an_action_for_a_command(self):
        result = await self.route("Unlock the door.", response(call("todo__get_items", {}, "c1")),
                                  response(message("The door is now unlocked.")))
        self.assertEqual(result.kind, FAILED)

    async def test_review_b1_a_read_cannot_verify_a_comma_joined_command(self):
        """haintent review B1: a question joined to a command by a comma, a
        read tool, and "now off" in the reply was spoken as verified."""
        for request, answer in (
            ("Which lights are on, turn them all off", "The lights are now off."),
            ("Is the heating on, make it warmer", "The heating is now at 23 degrees."),
            ("Is the garage closed, and if not close it", "The garage is now closed."),
            ("Is it too warm in here, cool it down", "It is now cooler."),
            ("Is anything playing, pause it", "The music is now paused."),
            ("When's the timer done, cancel it", "The timer is now cancelled."),
            ("What's the volume, turn it down", "The volume is now 30 percent."),
            ("Where's the vacuum, send it home", "The vacuum is now docked."),
            ("Are the lights on, if so switch them off", "The lights are now off."),
            ("Are the lights on?", "The lights are now off."),
            ("Did you turn off the lights", "They're turned off now; I've checked."),
        ):
            result = await self.route(request, response(call("todo__get_items", {}, "c1")),
                                      response(message(answer)))
            self.assertEqual(result.kind, FAILED, request)
            self.assertNotIn("Verified", result.commentary(), request)

    async def test_merge_a_read_cannot_verify_an_outcome_for_a_household_verb(self):
        """haintent review fold-in (v35 merge): "They're turned off." after
        only a read, for a question about an action, was a LOOKUP."""
        for request, answer in (
            ("Did you turn off the lights?", "They're turned off."),
            ("Did you arm the alarm?", "Yes, the alarm is armed."),
        ):
            result = await self.route(request, response(call("todo__get_items", {}, "c1")),
                                      response(message(answer)))
            self.assertEqual(result.kind, FAILED, request)
        result = await self.route("What's on the list?", response(call("todo__get_items", {}, "c1")),
                                  response(message("Milk and eggs.")))
        self.assertEqual(result.kind, LOOKUP, "a plain lookup stays a lookup")

    async def test_merge_armed_and_already_are_outcome_claims(self):
        """household-context review fold-in (v35 merge): with no tool, "armed" and
        "already" for an action-shaped request are not confirmed."""
        for request, answer in (
            ("Arm the alarm.", "Yes, the alarm is armed."),
            ("Close the garage door.", "The garage door is already closed."),
            ("Disarm the alarm please", "It's disarmed."),
        ):
            result = await self.route(request, response(message(answer)))
            self.assertEqual(result.kind, FAILED, request)

    def test_action_shape(self):
        for text in ("turn off the lights", "lights off", "can you open the garage", "please, what's on",
                     "could you tell me if the door is locked", "Is the lamp on? Turn it off.",
                     "Which lights are on, turn them all off", "Is the heating on, make it warmer",
                     "Is the garage closed, and if not close it", "Which lights are on, lights off",
                     "Did you add milk?", "Did you hear me", "Have the lights turned off",
                     "Hey, is the garage open?"):
            self.assertTrue(is_action_shaped(text), text)
        for text in ("what's the temperature", "is the door locked?", "what's on the list",
                     "Hey, is the door locked?", "how warm is it outside"):
            self.assertFalse(is_action_shaped(text), text)


if __name__ == "__main__":
    unittest.main()
