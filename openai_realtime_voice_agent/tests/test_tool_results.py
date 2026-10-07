"""Tool outcomes are read, explained, and not repeated without end.

The second canary's log showed every lifecycle counter healthy — observed 4,
dispatched 4, submitted 4, continued 4, abandoned 0 — while Atrium Lamp stayed
off, because nothing ever looked at what the tools *answered*. These tests pin
the three parts that now do: classification, the advice handed back to the
model, and the per-utterance limit on repeats.
"""
import unittest

from app.tool_results import (
    MAX_FAILED_ATTEMPTS,
    ActionLedger,
    ToolOutcome,
    classify_result,
    explain_for_model,
    redact,
)

from tests import live_fixtures as fx

ARGS = {"name": "Atrium Lamp", "area": "Kitchen", "domain": ["light"]}


class TestClassification(unittest.TestCase):
    def test_the_canary_invalid_slot_errors_are_failures(self):
        for text in (fx.INVALID_SLOTS_LIGHT_SET, fx.INVALID_SLOTS_TURN_ON):
            with self.subTest(text=text):
                outcome = classify_result(text)
                self.assertFalse(outcome.ok, "pipecat called this a success")
                self.assertEqual(outcome.kind, "invalid-arguments")
                self.assertTrue(outcome.definitive)

    def test_an_unmatched_entity_name_is_a_failure(self):
        outcome = classify_result(fx.NO_MATCH_RESULT)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, "no-matching-entity")

    def test_the_same_shape_arriving_as_json_text_classifies_identically(self):
        import json
        self.assertEqual(classify_result(json.dumps(fx.NO_MATCH_RESULT)).kind,
                         classify_result(fx.NO_MATCH_RESULT).kind)

    def test_a_successful_get_live_context_is_ok(self):
        outcome = classify_result(fx.CONTEXT_RESULT)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.describe(), "ok")

    def test_an_unconfirmed_call_is_not_definitive(self):
        outcome = classify_result(
            "Error: Home Assistant did not confirm HassTurnOn; it may or may not "
            "have completed. Say so briefly; do not retry automatically."
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, "uncertain")
        self.assertFalse(outcome.definitive,
                         "neither a retry nor a confident report is safe here")

    def test_a_confirmation_hold_is_not_a_failed_attempt(self):
        for result in (
            {"requires_confirmation": True, "confirm_id": "ab12"},
            {"status": "confirmation_required", "confirm_id": "ab12"},
        ):
            with self.subTest(result=result):
                outcome = classify_result(result)
                self.assertTrue(outcome.pending_confirmation)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.describe(), "awaiting confirmation")

    def test_ordinary_results_are_left_alone(self):
        for result in (None, "done", {"status": "done"}, {"success": True},
                       "Live Context: ...", 42):
            with self.subTest(result=result):
                self.assertTrue(classify_result(result).ok)

    def test_a_speaker_gate_refusal_is_recognised(self):
        outcome = classify_result({"error": (
            "Not available: this capability is reserved for Alex, and the "
            "current speaker's voice was not recognized as Alex. Relay this "
            "politely."
        )})
        self.assertEqual(outcome.kind, "speaker-gate")


class TestRedaction(unittest.TestCase):
    def test_quoted_values_never_reach_a_log_line(self):
        outcome = classify_result(fx.NO_MATCH_RESULT)
        self.assertIn("kitchen lights", outcome.detail,
                      "the model needs the name it asked for")
        self.assertNotIn("kitchen lights", outcome.log_detail,
                         "a household entity name must not be logged")
        self.assertIn("No exposed entities matched name '…'", outcome.log_detail)

    def test_redaction_is_bounded(self):
        self.assertLessEqual(len(redact("x" * 10_000)), 160)
        self.assertEqual(redact(None), "")
        self.assertEqual(redact("a\n  b\tc"), "a b c")


class TestAdviceToTheModel(unittest.TestCase):
    def test_invalid_arguments_tell_the_model_what_to_change(self):
        explained = explain_for_model("light__HassLightSet",
                                      fx.INVALID_SLOTS_LIGHT_SET)
        self.assertIs(explained["state_changed"], False)
        self.assertIn("leave those parameters out", explained["what_to_do"])
        self.assertIn("Received invalid slot info", explained["error"],
                      "the original message is kept alongside the advice")

    def test_an_unmatched_name_asks_for_a_clarification(self):
        explained = explain_for_model("intent__HassTurnOn", fx.NO_MATCH_RESULT)
        self.assertIn("GetLiveContext", explained["what_to_do"])
        self.assertIn("ask the user", explained["what_to_do"])
        self.assertIn("kitchen lights", explained["error"],
                      "the model needs the name back to ask about it")

    def test_an_uncertain_result_forbids_an_automatic_retry(self):
        explained = explain_for_model("intent__HassTurnOn",
                                      "Error: it may or may not have completed")
        self.assertIn("do not retry automatically", explained["what_to_do"])
        self.assertEqual(explained["state_changed"], "unknown")

    def test_a_success_is_returned_untouched(self):
        for result in (fx.CONTEXT_RESULT, "done", None):
            self.assertIs(explain_for_model("x", result), result)

    def test_a_confirmation_hold_is_returned_untouched(self):
        held = {"requires_confirmation": True, "confirm_id": "ab12"}
        self.assertIs(explain_for_model("intent__HassTurnOff", held), held)

    def test_an_unclassifiable_failure_is_returned_untouched(self):
        self.assertEqual(explain_for_model("x", "Error: something odd"),
                         "Error: something odd")


class TestActionLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = ActionLedger()

    def test_read_only_and_non_hass_tools_are_never_limited(self):
        for name in ("homeassistant__GetLiveContext", "web_search", "ask_openclaw",
                     "confirm_action", "todo__get_items", "script__tesla_watch"):
            with self.subTest(name=name):
                self.assertFalse(ActionLedger.guards(name))
                self.ledger.record(name, ARGS, ToolOutcome(ok=False, kind="error"))
                self.assertIsNone(self.ledger.check(name, ARGS))

    def test_an_exact_repeat_after_a_success_is_answered_from_the_ledger(self):
        self.ledger.record("intent__HassTurnOn", ARGS, ToolOutcome(ok=True))
        refusal = self.ledger.check("intent__HassTurnOn", ARGS)
        self.assertIsNotNone(refusal)
        self.assertIn("already ran successfully", refusal["error"])
        self.assertIn("Tell the user it is done", refusal["what_to_do"])
        self.assertIs(refusal["state_changed"], False)
        self.assertEqual(self.ledger.suppressed, 1)

    def test_an_exact_repeat_after_a_failure_is_refused_too(self):
        self.ledger.record("intent__HassTurnOn", ARGS,
                           classify_result(fx.INVALID_SLOTS_TURN_ON))
        refusal = self.ledger.check("intent__HassTurnOn", ARGS)
        self.assertIn("already failed", refusal["error"])
        self.assertIn("Do not repeat the identical call", refusal["what_to_do"])

    def test_the_canary_fallback_chain_stops_after_two_failures(self):
        """HassLightSet failed, HassTurnOn failed; a third must not reach the house."""
        self.assertIsNone(self.ledger.check("light__HassLightSet", ARGS))
        self.ledger.record("light__HassLightSet", ARGS,
                           classify_result(fx.INVALID_SLOTS_LIGHT_SET))
        self.assertIsNone(self.ledger.check("intent__HassTurnOn", ARGS))
        self.ledger.record("intent__HassTurnOn", ARGS,
                           classify_result(fx.INVALID_SLOTS_TURN_ON))
        refusal = self.ledger.check("intent__HassSetPosition", ARGS)
        self.assertIsNotNone(refusal, "a third attempt on the same target")
        self.assertIn("attempts to act on this target have already failed",
                      refusal["error"])
        self.assertIn("ask which device they mean", refusal["what_to_do"])
        self.assertEqual(MAX_FAILED_ATTEMPTS, 2)

    def test_a_different_target_is_unaffected_by_another_target_s_failures(self):
        other = {"name": "Workshop Lamp", "area": "Workshop", "domain": ["light"]}
        for name in ("light__HassLightSet", "intent__HassTurnOn"):
            self.ledger.record(name, ARGS, ToolOutcome(ok=False, kind="invalid-arguments"))
        self.assertIsNotNone(self.ledger.check("intent__HassTurnOn", ARGS))
        self.assertIsNone(self.ledger.check("intent__HassTurnOn", other))

    def test_a_compound_command_is_not_blocked_by_an_earlier_success(self):
        """"Turn it on, then set it to half" is two real instructions."""
        self.ledger.record("intent__HassTurnOn", ARGS, ToolOutcome(ok=True))
        dim = dict(ARGS, brightness=50)
        self.assertIsNone(self.ledger.check("light__HassLightSet", dim))
        self.assertIsNone(self.ledger.check("intent__HassTurnOn", dim),
                          "different arguments are a different instruction")

    def test_the_targeting_key_ignores_case_order_and_padding(self):
        self.ledger.record("light__HassLightSet",
                           {"name": " Atrium Lamp ", "area": "KITCHEN",
                            "domain": ["light"]},
                           ToolOutcome(ok=False, kind="invalid-arguments"))
        self.ledger.record("intent__HassTurnOn",
                           {"name": "atrium lamp", "area": "kitchen",
                            "domain": ["light"]},
                           ToolOutcome(ok=False, kind="invalid-arguments"))
        self.assertIsNotNone(self.ledger.check("intent__HassTurnOn", ARGS))

    def test_an_uncertain_result_fences_an_identical_automatic_retry(self):
        uncertain = classify_result("Error: it may or may not have completed")
        self.ledger.record("intent__HassTurnOn", ARGS, uncertain)
        refusal = self.ledger.check("intent__HassTurnOn", ARGS)
        self.assertEqual(refusal["state_changed"], "unknown")
        self.assertIn("Do not repeat", refusal["what_to_do"])

    def test_a_confirmation_hold_does_not_consume_an_attempt(self):
        held = classify_result({"requires_confirmation": True, "confirm_id": "ab12"})
        for _ in range(4):
            self.ledger.record("intent__HassTurnOff", ARGS, held)
        self.assertIsNone(self.ledger.check("intent__HassTurnOff", ARGS),
                          "asking the user is not a failed attempt")

    def test_a_new_utterance_clears_everything(self):
        for name in ("light__HassLightSet", "intent__HassTurnOn"):
            self.ledger.record(name, ARGS, ToolOutcome(ok=False, kind="error"))
        self.assertIsNotNone(self.ledger.check("intent__HassTurnOn", ARGS))
        self.ledger.reset()
        self.assertIsNone(self.ledger.check("intent__HassTurnOn", ARGS))
        self.assertIsNone(self.ledger.check("light__HassLightSet", ARGS))

    def test_unhashable_arguments_do_not_crash_the_ledger(self):
        weird = {"name": "X", "domain": ["light"], "blob": object()}
        self.ledger.record("intent__HassTurnOn", weird, ToolOutcome(ok=True))
        self.assertIsNotNone(self.ledger.check("intent__HassTurnOn", weird))


if __name__ == "__main__":
    unittest.main()
