"""Experiment overrides: gated on experiment_mode, bounded, never a cost bound."""
import json
import logging
import unittest

from app import experiment_overrides as eo

RIG = {"EXPERIMENT_MODE": "true"}


class TestGate(unittest.TestCase):
    def test_needs_experiment_mode(self):
        self.assertTrue(eo.enabled(RIG))
        self.assertFalse(eo.enabled({"EXPERIMENT_MODE": "false"}))
        self.assertFalse(eo.enabled({}))

    def test_mode_off_ignores_cold_overrides(self):
        env = {"EXPERIMENT_MODE": "false",
               "EXPERIMENT_OVERRIDES": json.dumps({"arm": "B", "overrides": {"live_service_tier": "flex"}})}
        self.assertIsNone(eo.apply_startup(env))
        self.assertNotIn("LIVE_SERVICE_TIER", env)


class TestCold(unittest.TestCase):
    def test_cold_overrides_reach_the_environment(self):
        env = dict(RIG, INSTRUCTIONS="Base.", EXPERIMENT_OVERRIDES=json.dumps({
            "run_id": "r1", "arm": "B", "overrides": {
                "live_service_tier": "flex", "live_reasoning_effort": "minimal", "live_router": False,
                "live_backend_model": "gpt-6-luna", "voice_prompt_variant": "Be brief."}}))
        data = eo.apply_startup(env)
        self.assertEqual(data["arm"], "B")
        self.assertEqual((env["LIVE_SERVICE_TIER"], env["LIVE_REASONING_EFFORT"], env["LIVE_ROUTER"]),
                         ("flex", "minimal", "false"))
        self.assertEqual(env["INSTRUCTIONS"], "Base. Be brief.")

    def test_cost_bounds_and_bad_values_are_never_applied(self):
        env = dict(RIG, EXPERIMENT_OVERRIDES=json.dumps({"overrides": {
            "live_idle_close_s": 600, "live_max_usd_per_day": 100, "live_followup_s": 120,
            "live_service_tier": "ultra", "live_backend_model": "rm -rf /"}}))
        data = eo.apply_startup(env)
        self.assertEqual(data["overrides"], {})
        for var in ("LIVE_IDLE_CLOSE_S", "LIVE_MAX_USD_PER_DAY", "LIVE_FOLLOWUP_S", "LIVE_SERVICE_TIER",
                    "LIVE_BACKEND_MODEL"):
            self.assertNotIn(var, env)

    def test_hot_values_are_clamped(self):
        ov = eo.validate({"ack_lag_s": 99, "user_turn_gap_s": 0.0, "fallback_ack_after_s": "x"}, set(eo.HOT_KEYS))
        self.assertEqual(ov, {"ack_lag_s": 1.5, "user_turn_gap_s": 0.3})

    def test_ack_lag_has_a_floor(self):
        self.assertEqual(eo.validate({"ack_lag_s": 0}, set(eo.HOT_KEYS)), {"ack_lag_s": 0.15})


class TestGrammarAdditionsNeverClaimAnOutcome(unittest.TestCase):
    """Rig review: a grammar addition could make "Okay, unlocked." an
    acknowledgement, so a guessed outcome played before the verified result."""

    def setUp(self):
        from app import live_router as lr
        self.lr = lr
        self.saved = (lr._WAIT_CORE, lr._WAIT_TAILS, lr._WAITS)

    def tearDown(self):
        self.lr._WAIT_CORE, self.lr._WAIT_TAILS, self.lr._WAITS = self.saved

    def test_claims_and_state_words_are_refused(self):
        bad = ["done", "all set", "unlocked", "i turned on", "on", "yes"]
        ov = eo.validate({"wait_core_extra": bad + ["one tick"], "wait_tails_extra": bad + ["the oven"]},
                         eo.COLD_KEYS)
        self.assertEqual(ov["wait_core_extra"], ["one tick"])
        self.assertEqual(ov["wait_tails_extra"], ["the oven"])

    def test_a_composed_phrase_that_claims_is_refused(self):
        # "they are" + the existing tail "now" composes "they are now".
        ov = eo.validate({"wait_core_extra": ["they are"]}, eo.COLD_KEYS)
        self.assertEqual(ov["wait_core_extra"], [])

    def test_okay_unlocked_stays_held_after_apply_startup(self):
        env = dict(RIG, EXPERIMENT_OVERRIDES=json.dumps({"overrides": {
            "wait_core_extra": ["unlocked", "done", "all set", "i turned on"],
            "wait_tails_extra": ["unlocked"]}}))
        eo.apply_startup(env)
        for text in ("Okay, unlocked.", "Done.", "All set.", "I turned on the light.",
                     "One sec, unlocked."):
            self.assertFalse(self.lr.is_acknowledgement(text), text)


class TestHot(unittest.TestCase):
    def setUp(self):
        from app import live_service
        self.ls = live_service
        self.orig = {k: getattr(live_service, a) for k, (_m, a, _l, _h) in eo.HOT_KEYS.items()}

    def tearDown(self):
        for k, (_m, a, _l, _h) in eo.HOT_KEYS.items():
            setattr(self.ls, a, self.orig[k])

    def test_session_open_applies_then_restores_defaults(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "experiment.json")
            with open(path, "w") as f:
                json.dump({"run_id": "r1", "arm": "B", "overrides": {"ack_lag_s": 0.2, "live_service_tier": "flex"}}, f)
            with self.assertLogs("app.experiment_overrides", logging.INFO) as logs:
                out = eo.apply_session(path, RIG)
            self.assertEqual(out["arm"], "B")
            self.assertEqual(self.ls.ACK_LAG_S, 0.2)
            line = next(r.getMessage() for r in logs.records if r.getMessage().startswith("🧪 experiment {"))
            self.assertEqual(json.loads(line.split("🧪 experiment ", 1)[1])["arm"], "B")
            self.assertTrue(any("need a restart" in r.getMessage() for r in logs.records))
            os.unlink(path)
            eo.apply_session(path, RIG)
            self.assertEqual(self.ls.ACK_LAG_S, self.orig["ack_lag_s"])

    def test_mode_off_changes_nothing(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "experiment.json")
            with open(path, "w") as f:
                json.dump({"arm": "B", "overrides": {"ack_lag_s": 0.0}}, f)
            self.assertIsNone(eo.apply_session(path, {"EXPERIMENT_MODE": "false"}))
            self.assertEqual(self.ls.ACK_LAG_S, self.orig["ack_lag_s"])


if __name__ == "__main__":
    unittest.main()
