"""Consequential actions need a confirmation turn, enforced below the model."""
import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any

from app.action_gate import ActionGate, EntityDirectory, confirmation_result, replace_arguments
from app.main import Application, SafeRealtimeLLMService
from app.phase_emitter import TurnLiveness
from app.turn_timeline import TurnTimeline

STATES = [
    {"entity_id": "lock.front_door", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "cover.garage", "attributes": {"friendly_name": "Garage Door", "device_class": "garage"}},
    {"entity_id": "cover.blinds", "attributes": {"friendly_name": "Office Blinds", "device_class": "blind"}},
    {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "input_boolean.vacation_mode", "attributes": {"friendly_name": "Vacation Mode"}},
    {"entity_id": "alarm_control_panel.home", "attributes": {"friendly_name": "Home Alarm"}},
]


async def fetch_states():
    return STATES


def gate(**kwargs):
    return ActionGate(directory=EntityDirectory(fetch_states), **kwargs)


class TestClassification(unittest.IsolatedAsyncioTestCase):
    async def test_unlocking_requires_confirmation_locking_does_not(self):
        g = gate()
        self.assertTrue((await g.check("HassTurnOff", {"name": "Front Door"})).requires_confirmation)
        self.assertFalse((await g.check("HassTurnOn", {"name": "Front Door"})).requires_confirmation)
        self.assertTrue((await g.check("HassTurnOff", {"domain": ["lock"]})).requires_confirmation)

    async def test_opening_garage_requires_confirmation_closing_does_not(self):
        g = gate()
        decision = await g.check("HassTurnOn", {"name": "garage door"})
        self.assertTrue(decision.requires_confirmation)
        self.assertEqual(decision.summary, "open Garage Door")
        self.assertFalse((await g.check("HassTurnOff", {"name": "Garage Door"})).requires_confirmation)
        self.assertTrue((await g.check("HassSetPosition", {"name": "Garage Door", "position": 50})).requires_confirmation)
        self.assertFalse((await g.check("HassSetPosition", {"name": "Garage Door", "position": 0})).requires_confirmation)

    async def test_ordinary_devices_are_not_gated(self):
        g = gate()
        for args in ({"name": "Kitchen Light"}, {"name": "Office Blinds"}, {"area": "kitchen", "domain": ["light"]}):
            self.assertFalse((await g.check("HassTurnOn", args)).requires_confirmation, args)

    async def test_namespaced_intent_is_classified_as_the_intent_not_its_description(self):
        g = gate(tool_descriptions={
            "intent__HassTurnOff": "Turns off switches, locks, doors, garage doors, and helpers",
        })
        ordinary = await g.check(
            "intent__HassTurnOff", {"name": "Vacation Mode", "domain": ["switch"]}
        )
        self.assertFalse(ordinary.requires_confirmation)
        consequential = await g.check(
            "intent__HassTurnOff", {"name": "Front Door", "domain": ["lock"]}
        )
        self.assertTrue(consequential.requires_confirmation)
        self.assertEqual(consequential.rule, "lock")

    async def test_unique_named_target_reconciles_wrong_domain(self):
        g = gate()
        corrected = await g.reconcile_arguments(
            "intent__HassTurnOff", {"name": "Vacation Mode", "domain": ["switch"]}
        )
        self.assertEqual(corrected["domain"], ["input_boolean"])

    async def test_unique_named_cover_reconciles_wrong_device_class_and_stays_gated(self):
        async def gate_states():
            return [
                {"entity_id": "cover.garage", "attributes": {
                    "friendly_name": "Garage Door", "device_class": "gate"
                }}
            ]

        g = ActionGate(directory=EntityDirectory(gate_states))
        corrected = await g.reconcile_arguments(
            "intent__HassTurnOn", {"name": "Garage Door", "device_class": ["garage"]}
        )
        self.assertEqual(corrected["device_class"], ["gate"])
        decision = await g.check("intent__HassTurnOn", corrected)
        self.assertTrue(decision.requires_confirmation)
        self.assertEqual(decision.rule, "gate")

    async def test_reconciliation_preserves_ambiguity_and_strengthens_safety(self):
        async def ambiguous_states():
            return STATES + [
                {"entity_id": "switch.vacation_lamp", "attributes": {"friendly_name": "Vacation Lamp"}},
            ]

        ambiguous = ActionGate(directory=EntityDirectory(ambiguous_states))
        original = {"name": "Vacation", "domain": ["switch"]}
        self.assertEqual(await ambiguous.reconcile_arguments("intent__HassTurnOff", original), original)

        g = gate()
        corrected = await g.reconcile_arguments(
            "intent__HassTurnOff", {"name": "Front Door", "domain": ["switch"]}
        )
        self.assertEqual(corrected["domain"], ["lock"])
        self.assertTrue((await g.check("intent__HassTurnOff", corrected)).requires_confirmation)

    async def test_alarm_actions_are_gated(self):
        self.assertTrue((await gate().check("HassTurnOff", {"name": "Home Alarm"})).requires_confirmation)

    async def test_unresolvable_door_name_fails_safe(self):
        g = ActionGate(directory=EntityDirectory(None))  # Home Assistant unreachable
        self.assertTrue((await g.check("HassTurnOff", {"name": "back door"})).requires_confirmation)
        self.assertTrue((await g.check("HassTurnOn", {"name": "side gate"})).requires_confirmation)
        self.assertFalse((await g.check("HassTurnOn", {"name": "hall lamp"})).requires_confirmation)

    async def test_scripts_and_agent_requests(self):
        g = gate()
        g.tool_descriptions["front_entry"] = "Unlocks the front entry for guests"
        self.assertTrue((await g.check("front_entry", {})).requires_confirmation)
        self.assertTrue((await g.check("garage_toggle", {})).requires_confirmation)
        self.assertFalse((await g.check("good_night", {})).requires_confirmation)
        self.assertTrue((await g.check("ask_openclaw", {"question": "please unlock the front door"})).requires_confirmation)
        self.assertTrue((await g.check("ask_openclaw", {"question": "open the garage when I get home"})).requires_confirmation)
        self.assertFalse((await g.check("ask_openclaw", {"question": "what's on my calendar"})).requires_confirmation)

    async def test_rules_and_extra_tools_are_configurable(self):
        g = gate(rules=["lock"], extra_tools=["buy_now"])
        self.assertFalse((await g.check("HassTurnOn", {"name": "Garage Door"})).requires_confirmation)
        self.assertTrue((await g.check("buy_now", {})).requires_confirmation)
        self.assertFalse(ActionGate(rules=[]).enabled)


class TestPending(unittest.TestCase):
    def setUp(self):
        self.t = 0.0
        self.g = ActionGate(clock=lambda: self.t, window_s=30)

    async def _noop(self, params):
        return None

    def test_requires_a_new_user_utterance(self):
        p = self.g.request("kitchen", "HassTurnOff", {"name": "Front Door"}, self._noop, "unlock Front Door", 3, 1)
        pending, error = self.g.take(p.confirm_id, "kitchen", 3, 1)
        self.assertIsNone(pending)
        self.assertIn("not answered", error)
        pending, error = self.g.take(p.confirm_id, "kitchen", 4, 1)
        self.assertIs(pending.confirm_id, p.confirm_id)
        self.assertIsNone(self.g.take(p.confirm_id, "kitchen", 5, 1)[0], "single use")

    def test_other_device_expired_and_new_wake_are_refused(self):
        p = self.g.request("kitchen", "HassTurnOff", {"name": "Front Door"}, self._noop, "unlock", 0, 1)
        self.assertIsNone(self.g.take(p.confirm_id, "office", 9, 1)[0])
        self.assertIsNone(self.g.take(p.confirm_id, "kitchen", 9, 2)[0], "new wake cancels")
        p2 = self.g.request("kitchen", "HassTurnOff", {"name": "Front Door"}, self._noop, "unlock", 0, 1)
        self.t += 31
        self.assertIsNone(self.g.take(p2.confirm_id, "kitchen", 9, 1)[0], "expired")

    def test_repeated_request_reuses_pending(self):
        a = self.g.request("kitchen", "HassTurnOff", {"name": "Front Door"}, self._noop, "unlock", 0, 1)
        b = self.g.request("kitchen", "HassTurnOff", {"name": "Front Door"}, self._noop, "unlock", 0, 1)
        self.assertEqual(a.confirm_id, b.confirm_id)

    def test_result_tells_the_model_nothing_happened(self):
        p = self.g.request("kitchen", "HassTurnOff", {}, self._noop, "unlock Front Door", 0, 1)
        result = confirmation_result(p, 30)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertIn("has NOT been done", result["instructions"])


@dataclass
class Params:
    function_name: str
    tool_call_id: str
    arguments: Any
    llm: Any = None
    context: Any = None
    result_callback: Any = None
    results: list = field(default_factory=list)


class TestWrapperIntegration(unittest.IsolatedAsyncioTestCase):
    """The held call never reaches Home Assistant until confirm_action succeeds."""

    async def asyncSetUp(self):
        self.registered = {}
        service = object.__new__(SafeRealtimeLLMService)
        service.male_only_tools = set()
        service.speaker_probe = None
        service.turn_liveness = TurnLiveness()
        service.turn_timeline = TurnTimeline("kitchen")
        service.turn_timeline.begin("w1")
        service.action_gate = gate()
        service.spoken_prompts = None
        # Capture what pipecat would register instead of needing a live service.
        import pipecat.services.openai.realtime.llm as llm_mod
        base = llm_mod.OpenAIRealtimeLLMService
        self._orig = base.register_function

        def fake_register(_self, name, handler, start_callback=None, cancel_on_interruption=True):
            self.registered[name] = handler

        base.register_function = fake_register
        self.service = service
        self.executed = []

        async def unlock_handler(params):
            self.executed.append(dict(params.arguments))
            await params.result_callback({"status": "done"})

        service.register_function("HassTurnOff", unlock_handler)
        service.register_function("intent__HassTurnOff", unlock_handler)
        app = Application()
        app.action_gate = service.action_gate
        service.register_function("confirm_action", app._create_confirm_handler(service))

    async def asyncTearDown(self):
        import pipecat.services.openai.realtime.llm as llm_mod
        llm_mod.OpenAIRealtimeLLMService.register_function = self._orig

    async def call(self, name, args):
        results = []

        async def cb(result):
            results.append(result)

        await self.registered[name](Params(name, "call_1", args, result_callback=cb))
        return results[-1]

    async def test_unlock_is_held_until_confirmed_after_user_reply(self):
        held = await self.call("HassTurnOff", {"name": "Front Door"})
        self.assertEqual(held["status"], "confirmation_required")
        self.assertEqual(self.executed, [])
        # Model tries to confirm before the user answered: refused.
        early = await self.call("confirm_action", {"confirm_id": held["confirm_id"]})
        self.assertIn("error", early)
        self.assertEqual(self.executed, [])
        # The user answers (a new end-of-utterance), then confirmation runs it.
        self.service.turn_timeline.note_speech_stopped(None, None, None)
        done = await self.call("confirm_action", {"confirm_id": held["confirm_id"]})
        self.assertEqual(done, {"status": "done"})
        self.assertEqual(self.executed, [{"name": "Front Door"}])

    async def test_ordinary_calls_run_immediately(self):
        result = await self.call("HassTurnOff", {"name": "Kitchen Light"})
        self.assertEqual(result, {"status": "done"})
        self.assertEqual(self.executed, [{"name": "Kitchen Light"}])

    async def test_namespaced_call_corrects_domain_and_runs_without_confirmation(self):
        self.service.action_gate.tool_descriptions["intent__HassTurnOff"] = (
            "Turns off switches, locks, doors, garage doors, and helpers"
        )
        result = await self.call(
            "intent__HassTurnOff", {"name": "Vacation Mode", "domain": ["switch"]}
        )
        self.assertEqual(result, {"status": "done"})
        self.assertEqual(
            self.executed,
            [{"name": "Vacation Mode", "domain": ["input_boolean"]}],
        )

    def test_replace_arguments_clones_dataclass(self):
        params = Params("confirm_action", "c", {"confirm_id": "x"})
        clone = replace_arguments(params, "HassTurnOff", {"name": "Front Door"})
        self.assertEqual((clone.function_name, clone.arguments), ("HassTurnOff", {"name": "Front Door"}))
        self.assertEqual(params.function_name, "confirm_action")


if __name__ == "__main__":
    unittest.main()
