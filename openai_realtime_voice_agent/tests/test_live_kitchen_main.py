"""A failed Home Assistant light action, replayed end to end.

The synthetic fixture preserves the protocol and result shapes that matter. In
the reproduced turn
the whole tool lifecycle completed — ``observed 4, dispatched 4, submitted 4,
continued 4, abandoned 0`` — Atrium Lamp stayed off, and the assistant told the
user control had failed. Two separate defects produced that:

1. GPT-Live's delegated backend filled every optional parameter with a
   placeholder (``floor: ""``, ``color: ""``, ``temperature: 0``,
   ``device_class: []``). Home Assistant's intent slot schema validates
   ``name``/``area``/``floor`` with ``non_empty_string``, so ``floor: ""`` made
   both actions fail before any entity was resolved. See
   ``fx.HA_REJECTED_SLOTS`` for the Home Assistant validation shapes.
2. Nothing inspected what a tool answered. ``Error calling tool: Received
   invalid slot info for HassLightSet`` went back to the model untouched —
   pipecat logged it as "completed successfully" — so the model guessed, and
   guessed by firing a second action at the same light.

These tests replay that turn through the real service, the real delegation
ledger and the production tool guards, and assert the repaired behaviour: the
placeholders are gone before Home Assistant sees the call, a failure comes back
as something the model can act on, and a third attempt at one target is
answered locally instead of being sent to the house.
"""
import json
import unittest

from app.action_gate import ActionGate, EntityDirectory

from tests import live_fixtures as fx
from tests.live_harness import LiveHarness

STATES = [
    {"entity_id": "light.kitchen_main", "attributes": {"friendly_name": "Atrium Lamp"}},
    {"entity_id": "lock.kitchen_door", "attributes": {"friendly_name": "Entry Lock"}},
]


async def fetch_states():
    return STATES


class KitchenMain(LiveHarness):
    """The harness with the two tools the canary actually called."""

    def __init__(self, test, results=None, **kwargs):
        super().__init__(test, tools=[fx.LIGHT_SET_TOOL, fx.TURN_ON_TOOL], **kwargs)
        # tool name -> what Home Assistant answers. Default: success.
        self.canned = results or {}

    async def __aenter__(self):
        await super().__aenter__()
        self.service.action_gate = ActionGate(directory=EntityDirectory(fetch_states))
        self.service.spoken_prompts = None
        self.service.turn_timeline.begin("w1")
        self.executed = []
        for name in ("light__HassLightSet", "intent__HassTurnOn",
                     "intent__HassTurnOff", "homeassistant__GetLiveContext"):
            self.service.register_function(name, self._handler(name))
        await self.start_session()
        # The ledger is per-utterance; the canary's three calls were one.
        await self.service._open_turn("user")
        return self

    def _handler(self, name):
        async def handler(params):
            self.executed.append((name, dict(params.arguments or {})))
            await params.result_callback(self.canned.get(name, {"status": "done"}))
        return handler

    async def call(self, call_id, name, arguments, delegation_id):
        self.socket.push_all(fx.function_call_sequence(
            call_id, name, arguments, delegation_id=delegation_id))
        await self.socket.wait_for_sent(
            "response.item.create", count=len(self.submitted()) + 1)

    def submitted(self):
        """The ``response.item.create`` items sent back to the model."""
        return [m["item"] for m in self.socket.of("response.item.create")]

    def outputs(self):
        """Each submitted result, parsed back out of its JSON envelope."""
        out = []
        for item in self.submitted():
            try:
                out.append(json.loads(item["output"]))
            except (ValueError, KeyError, TypeError):
                out.append(item.get("output"))
        return out


class TestArgumentsReachHomeAssistantClean(unittest.IsolatedAsyncioTestCase):
    async def test_the_exact_canary_light_set_loses_only_its_placeholders(self):
        async with KitchenMain(self) as h:
            await h.call(fx.LIGHT_SET_CALL_ID, "light__HassLightSet",
                         fx.LIGHT_SET_ARGUMENTS, "d1")
            self.assertEqual(h.executed, [("light__HassLightSet", {
                "name": "Atrium Lamp", "area": "Kitchen",
                "domain": ["light"], "brightness": 100,
            })], "floor='', color='' and temperature=0 made this call fail in the house")

    async def test_the_exact_canary_turn_on_loses_only_its_placeholders(self):
        async with KitchenMain(self) as h:
            await h.call(fx.TURN_ON_CALL_ID, "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d1")
            self.assertEqual(h.executed, [("intent__HassTurnOn", {
                "name": "Atrium Lamp", "area": "Kitchen", "domain": ["light"],
            })])

    async def test_the_dropped_keys_are_recorded_without_their_values(self):
        async with KitchenMain(self) as h:
            await h.call(fx.LIGHT_SET_CALL_ID, "light__HassLightSet",
                         fx.LIGHT_SET_ARGUMENTS, "d1")
            line = h.service.calls.snapshot()["recent"][-1]
            self.assertIn("dropped color,floor,temperature", line)
            self.assertIn("-> ok", line)
            self.assertNotIn("Atrium Lamp", line, "argument values are never logged")

    async def test_a_consequential_action_still_waits_for_confirmation(self):
        """Sanitizing must not let a gated action through."""
        async with KitchenMain(self) as h:
            await h.call("call_unlock1", "intent__HassTurnOff",
                         '{"name":"Entry Lock","area":"","floor":"","domain":["lock"]}',
                         "d1")
            self.assertEqual(h.executed, [],
                             "nothing may reach Home Assistant before a yes")
            self.assertIn("confirm", json.dumps(h.outputs()[0]).lower())


class TestFailuresComeBackActionable(unittest.IsolatedAsyncioTestCase):
    async def test_an_invalid_slot_error_becomes_advice_the_model_can_use(self):
        async with KitchenMain(self, results={
            "light__HassLightSet": fx.INVALID_SLOTS_LIGHT_SET,
        }) as h:
            await h.call(fx.LIGHT_SET_CALL_ID, "light__HassLightSet",
                         fx.LIGHT_SET_ARGUMENTS, "d1")
            result = h.outputs()[0]
            self.assertIs(result["state_changed"], False)
            self.assertIn("leave those parameters out", result["what_to_do"])
            self.assertIn("Received invalid slot info", result["error"])

    async def test_an_unmatched_name_asks_the_user_instead_of_claiming_success(self):
        async with KitchenMain(self, results={
            "intent__HassTurnOn": fx.NO_MATCH_RESULT,
        }) as h:
            await h.call(fx.TURN_ON_CALL_ID, "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d1")
            result = h.outputs()[0]
            self.assertIs(result["state_changed"], False)
            self.assertIn("GetLiveContext", result["what_to_do"])

    async def test_a_failure_is_recorded_in_the_turn_diagnostics(self):
        async with KitchenMain(self, results={
            "intent__HassTurnOn": fx.NO_MATCH_RESULT,
        }) as h:
            await h.call(fx.TURN_ON_CALL_ID, "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d1")
            line = h.service.calls.snapshot()["recent"][-1]
            self.assertIn("no-matching-entity", line)
            self.assertIn("No exposed entities matched name '…'", line,
                          "the entity name is redacted out of the log")
            self.assertNotIn("kitchen lights", line)

    async def test_a_successful_action_is_still_reported_as_success(self):
        async with KitchenMain(self) as h:
            await h.call(fx.TURN_ON_CALL_ID, "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d1")
            self.assertEqual(h.outputs()[0], {"status": "done"},
                             "a working tool's result is passed through untouched")


class TestTheFallbackChainStops(unittest.IsolatedAsyncioTestCase):
    async def test_a_third_attempt_at_one_target_never_reaches_the_house(self):
        """Exactly the canary's shape: HassLightSet, then HassTurnOn, then more."""
        async with KitchenMain(self, results={
            "light__HassLightSet": fx.INVALID_SLOTS_LIGHT_SET,
            "intent__HassTurnOn": fx.INVALID_SLOTS_TURN_ON,
        }) as h:
            await h.call(fx.LIGHT_SET_CALL_ID, "light__HassLightSet",
                         fx.LIGHT_SET_ARGUMENTS, "d1")
            await h.call(fx.TURN_ON_CALL_ID, "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d2")
            self.assertEqual(len(h.executed), 2, "both real attempts were made")
            await h.call("call_third", "intent__HassTurnOn",
                         '{"name":"Atrium Lamp","area":"Kitchen","domain":["light"]}',
                         "d3")
            self.assertEqual(len(h.executed), 2,
                             "the third attempt must not be sent to Home Assistant")
            refusal = h.outputs()[-1]
            self.assertIs(refusal["state_changed"], False)
            self.assertIn("already failed", refusal["error"])
            self.assertEqual(h.service.actions.suppressed, 1)
            self.assertIn("repeats_refused", h.service.calls.snapshot(
                repeats_refused=h.service.actions.suppressed))

    async def test_the_refusal_is_still_submitted_and_continued(self):
        """A refused call must answer the model, or the turn hangs."""
        async with KitchenMain(self, results={
            "light__HassLightSet": fx.INVALID_SLOTS_LIGHT_SET,
            "intent__HassTurnOn": fx.INVALID_SLOTS_TURN_ON,
        }) as h:
            for index, (call_id, name, args) in enumerate((
                (fx.LIGHT_SET_CALL_ID, "light__HassLightSet", fx.LIGHT_SET_ARGUMENTS),
                (fx.TURN_ON_CALL_ID, "intent__HassTurnOn", fx.TURN_ON_ARGUMENTS),
                ("call_third", "intent__HassTurnOn", fx.TURN_ON_ARGUMENTS),
            )):
                await h.call(call_id, name, args, f"d{index}")
            self.assertEqual(len(h.submitted()), 3)
            log = h.service.calls.snapshot()
            self.assertEqual(log["submitted"], 3)
            self.assertEqual(log["abandoned"], 0)
            self.assertIn("refused as a repeat", log["recent"][-1])
            self.assertNotIn("RESULT-NOT-SUBMITTED", log["recent"][-1])

    async def test_a_new_utterance_lets_the_user_try_again(self):
        async with KitchenMain(self, results={
            "light__HassLightSet": fx.INVALID_SLOTS_LIGHT_SET,
            "intent__HassTurnOn": fx.INVALID_SLOTS_TURN_ON,
        }) as h:
            await h.call(fx.LIGHT_SET_CALL_ID, "light__HassLightSet",
                         fx.LIGHT_SET_ARGUMENTS, "d1")
            await h.call(fx.TURN_ON_CALL_ID, "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d2")
            h.canned = {}  # the user asks again; this time it works
            await h.service._open_turn("user")
            await h.call("call_next_turn", "intent__HassTurnOn",
                         fx.TURN_ON_ARGUMENTS, "d3")
            self.assertEqual(len(h.executed), 3,
                             "a fresh request must never be refused by the last one")
            self.assertEqual(h.outputs()[-1], {"status": "done"})


if __name__ == "__main__":
    unittest.main()
