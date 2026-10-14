"""Runtime selector normalization, Live config, voice policy and the parity gate."""
import unittest

from app.voice_runtime import (
    DEFAULT_LIVE_BACKEND_MODEL,
    DEFAULT_LIVE_MODEL,
    LIVE,
    REALTIME,
    LiveConfig,
    LiveRuntimeBlocked,
    check_live_deployable,
    live_parity_gaps,
    live_voice_for,
    normalize_runtime,
    parity_report_lines,
    resolve_voice_runtime,
)


class TestRuntimeSelector(unittest.TestCase):
    def test_missing_or_empty_means_realtime_for_legacy_installs(self):
        self.assertEqual(resolve_voice_runtime({}), REALTIME)
        self.assertEqual(resolve_voice_runtime({"VOICE_RUNTIME": ""}), REALTIME)
        self.assertEqual(resolve_voice_runtime({"VOICE_RUNTIME": "   "}), REALTIME)

    def test_live_aliases(self):
        for raw in ("live", "LIVE", " Live ", "gpt-live", "gpt_live", "gpt-live-1"):
            self.assertEqual(normalize_runtime(raw), LIVE, raw)

    def test_realtime_aliases(self):
        for raw in ("realtime", "gpt-realtime", "legacy"):
            self.assertEqual(normalize_runtime(raw), REALTIME, raw)

    def test_unknown_values_fall_back_to_realtime(self):
        self.assertEqual(normalize_runtime("gpt-realtime-2"), REALTIME)
        self.assertEqual(normalize_runtime("bananas"), REALTIME)


class TestLiveVoicePolicy(unittest.TestCase):
    def test_documented_live_voices_pass_through(self):
        self.assertEqual(live_voice_for("marin"), "marin")
        self.assertEqual(live_voice_for("Cedar"), "cedar")
        self.assertEqual(live_voice_for("quartz"), "quartz")
        self.assertEqual(live_voice_for("Vesper"), "vesper")

    def test_realtime_only_voice_is_not_guessed(self):
        # Falls back to the server default instead of substituting a voice.
        self.assertIsNone(live_voice_for("alloy"))
        self.assertIsNone(live_voice_for(""))


class TestLiveConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = LiveConfig.from_env({}, configured_voice="marin")
        self.assertEqual(cfg.model, DEFAULT_LIVE_MODEL)
        self.assertEqual(cfg.backend_model, DEFAULT_LIVE_BACKEND_MODEL)
        self.assertEqual(cfg.reasoning_effort, "low")
        self.assertIsNone(cfg.service_tier)
        self.assertEqual(cfg.voice, "marin")
        self.assertFalse(cfg.acknowledge_gaps)
        self.assertIsNone(cfg.max_output_tokens)
        self.assertEqual(cfg.ignored, [])

    def test_overrides_and_validation(self):
        cfg = LiveConfig.from_env({
            "LIVE_MODEL": "gpt-live-1",
            "LIVE_BACKEND_MODEL": "gpt-6-sol",
            "LIVE_REASONING_EFFORT": "HIGH",
            "LIVE_SERVICE_TIER": "priority",
            "LIVE_BACKEND_INSTRUCTIONS": "  be terse ",
            "LIVE_ACKNOWLEDGE_GAPS": "true",
        }, configured_voice="cedar", max_output_tokens=8)
        self.assertEqual(cfg.backend_model, "gpt-6-sol")
        self.assertEqual(cfg.reasoning_effort, "high")
        self.assertEqual(cfg.service_tier, "priority")
        self.assertEqual(cfg.backend_instructions, "be terse")
        self.assertTrue(cfg.acknowledge_gaps)
        self.assertEqual(cfg.max_output_tokens, 16, "Responses requires at least 16")

    def test_invalid_effort_and_tier_fall_back(self):
        cfg = LiveConfig.from_env({"LIVE_REASONING_EFFORT": "max", "LIVE_SERVICE_TIER": "turbo"})
        self.assertEqual(cfg.reasoning_effort, "low")
        self.assertIsNone(cfg.service_tier)

    def test_realtime_only_options_are_reported_as_ignored(self):
        cfg = LiveConfig.from_env({
            "TURN_DETECTION_TYPE": "server_vad", "NOISE_REDUCTION": "far_field",
            "OPENAI_SPEED": "1.2", "TRANSCRIPTION_LANGUAGE": "nl",
        })
        self.assertEqual(
            cfg.ignored, ["turn_detection_type", "noise_reduction", "transcription_language", "openai_speed"]
        )


class TestParityGate(unittest.TestCase):
    def test_gaps_block_until_acknowledged(self):
        cfg = LiveConfig.from_env({})
        self.assertTrue(live_parity_gaps(cfg), "the cost sensor gap is explicit")
        with self.assertRaises(LiveRuntimeBlocked):
            check_live_deployable(cfg)
        cfg.acknowledge_gaps = True
        check_live_deployable(cfg)  # no raise

    def test_report_lists_every_item_with_a_status(self):
        lines = parity_report_lines()
        self.assertTrue(any("unsupported" in line for line in lines))
        self.assertTrue(any("Home Assistant tools" in line for line in lines))


class TestDiagnosticCapture(unittest.TestCase):
    def test_raw_audio_capture_is_off_unless_asked_for_and_is_capped(self):
        self.assertEqual(LiveConfig.from_env({}).audio_capture_ms, 0)
        self.assertEqual(LiveConfig.from_env({"LIVE_AUDIO_CAPTURE_MS": "2000"}).audio_capture_ms, 2000)
        self.assertEqual(LiveConfig.from_env({"LIVE_AUDIO_CAPTURE_MS": "999999"}).audio_capture_ms, 60000)
        self.assertEqual(LiveConfig.from_env({"LIVE_AUDIO_CAPTURE_MS": "-1"}).audio_capture_ms, 0)
        self.assertEqual(LiveConfig.from_env({"LIVE_AUDIO_CAPTURE_MS": "lots"}).audio_capture_ms, 0)


class TestOutputLeadDefault(unittest.TestCase):
    """GPT-Live delivers at ~1.0x real time, so the relay must supply the lead."""

    def test_live_defaults_to_the_measured_lead_realtime_keeps_zero(self):
        from app.voice_runtime import LIVE_OUTPUT_LEAD_MS, resolve_output_lead_ms

        self.assertEqual(resolve_output_lead_ms(None, LIVE), LIVE_OUTPUT_LEAD_MS)
        self.assertEqual(resolve_output_lead_ms("", LIVE), LIVE_OUTPUT_LEAD_MS)
        self.assertEqual(resolve_output_lead_ms(None, REALTIME), 0,
                         "the Realtime default must not change")
        self.assertEqual(resolve_output_lead_ms("", REALTIME), 0)

    def test_an_explicit_value_always_wins_including_zero(self):
        from app.voice_runtime import resolve_output_lead_ms

        self.assertEqual(resolve_output_lead_ms("0", LIVE), 0)
        self.assertEqual(resolve_output_lead_ms("250", LIVE), 250)
        self.assertEqual(resolve_output_lead_ms("400", REALTIME), 400)

    def test_out_of_range_is_clamped_and_nonsense_falls_back(self):
        from app.voice_runtime import LIVE_OUTPUT_LEAD_MS, resolve_output_lead_ms

        self.assertEqual(resolve_output_lead_ms("9999", LIVE), 2000)
        self.assertEqual(resolve_output_lead_ms("-5", LIVE), 0)
        self.assertEqual(resolve_output_lead_ms("soon", LIVE), LIVE_OUTPUT_LEAD_MS)
        self.assertEqual(resolve_output_lead_ms("soon", REALTIME), 0)


class TestLiveCostBounds(unittest.TestCase):
    """The cost bounds survive the trip from add-on options to the gate."""

    def test_defaults_are_the_on_demand_bounds(self):
        config = LiveConfig.from_env({})
        self.assertEqual(config.idle_close_s, 10.0)
        self.assertEqual(config.followup_s, 8.0)
        self.assertEqual(config.max_silent_s, 45.0)
        self.assertEqual(config.max_session_s, 300.0)
        self.assertEqual(config.max_session_minutes_per_day, 60.0)
        self.assertEqual(config.max_usd_per_day, 3.0)

    def test_the_options_reach_the_session_policy(self):
        config = LiveConfig.from_env({
            "LIVE_IDLE_CLOSE_S": "6", "LIVE_FOLLOWUP_S": "4",
            "LIVE_MAX_SILENT_S": "20", "LIVE_MAX_SESSION_S": "90",
        })
        policy = config.session_policy()
        self.assertEqual(policy.idle_s, 6.0)
        self.assertEqual(policy.followup_s, 4.0)
        self.assertEqual(policy.max_silent_s, 20.0)
        self.assertEqual(policy.max_session_s, 90.0)

    def test_the_options_reach_the_spend_ledger(self):
        config = LiveConfig.from_env({
            "LIVE_MAX_SESSION_MINUTES_PER_DAY": "15", "LIVE_MAX_USD_PER_DAY": "0.75",
        })
        ledger = config.spend_ledger(path=None)
        self.assertEqual(ledger.max_session_minutes_per_day, 15.0)
        self.assertEqual(ledger.max_usd_per_day, 0.75)

    def test_a_typo_clamps_instead_of_failing_startup(self):
        config = LiveConfig.from_env({
            "LIVE_MAX_SESSION_S": "999999",   # above OpenAI's own 60-min cap
            "LIVE_IDLE_CLOSE_S": "soon",
            "LIVE_MAX_USD_PER_DAY": "-4",
        })
        self.assertEqual(config.max_session_s, 3300.0)
        self.assertEqual(config.idle_close_s, 10.0)
        self.assertEqual(config.max_usd_per_day, 0.0)

    def test_the_session_maximum_stays_under_the_server_cap(self):
        config = LiveConfig.from_env({"LIVE_MAX_SESSION_S": "3300"})
        self.assertLess(config.max_session_s, 3600.0)

    def test_the_worst_case_session_is_priced(self):
        config = LiveConfig.from_env({"LIVE_MAX_SESSION_S": "300"})
        self.assertAlmostEqual(config.worst_case_session_usd, 0.25, places=6)

    def test_zero_caps_are_allowed_and_mean_uncapped(self):
        config = LiveConfig.from_env({
            "LIVE_MAX_SESSION_MINUTES_PER_DAY": "0", "LIVE_MAX_USD_PER_DAY": "0",
        })
        self.assertIsNone(config.spend_ledger(path=None).remaining_seconds())


class TestMainInstallsCostControls(unittest.TestCase):
    """app/main.py arms the gate under live and leaves realtime alone."""

    def app(self, runtime, config=None):
        from app.main import Application

        app = object.__new__(Application)
        app.voice_runtime = runtime
        app.live_config = config
        app.websocket_handler = type("H", (), {
            "live_session_policy": None, "live_spend_ledger": None,
            "budget_refusal_speaker": None,
        })()
        return app

    def test_live_installs_the_policy_the_ledger_and_the_refusal_speaker(self):
        config = LiveConfig.from_env({"LIVE_MAX_USD_PER_DAY": "1.5"})
        app = self.app(LIVE, config)
        app._install_live_cost_controls()
        handler = app.websocket_handler
        self.assertIsNotNone(handler.live_session_policy)
        self.assertEqual(handler.live_spend_ledger.max_usd_per_day, 1.5)
        self.assertIsNotNone(handler.budget_refusal_speaker)

    def test_realtime_installs_nothing(self):
        app = self.app(REALTIME, None)
        app._install_live_cost_controls()
        self.assertIsNone(app.websocket_handler.live_session_policy)
        self.assertIsNone(app.websocket_handler.live_spend_ledger)

    def test_disabling_both_caps_is_reported_as_an_error(self):
        config = LiveConfig.from_env({
            "LIVE_MAX_SESSION_MINUTES_PER_DAY": "0", "LIVE_MAX_USD_PER_DAY": "0",
        })
        app = self.app(LIVE, config)
        with self.assertLogs("app.main", level="ERROR") as logs:
            app._install_live_cost_controls()
        self.assertTrue(any("BOTH GPT-Live spend caps are disabled" in line
                            for line in logs.output))


if __name__ == "__main__":
    unittest.main()
