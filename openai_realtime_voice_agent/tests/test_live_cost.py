"""The GPT-Live cost model and the hard spend cutoff.

These are the numbers an operator is trusting, so they are asserted against
the published rate ($0.05 per connected session minute, billed per second) and
against the real incident that motivated the whole on-demand lifecycle.
"""
import json
import os
import tempfile
import unittest

from app.live_cost import (
    DEFAULT_MAX_SESSION_MINUTES_PER_DAY,
    DEFAULT_MAX_USD_PER_DAY,
    USD_PER_SESSION_MINUTE,
    LiveSpendLedger,
    SessionCharge,
    projected_cost_usd,
    session_cost_usd,
)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def charge(seconds, at, device="kitchen", reason="idle"):
    return SessionCharge(device_id=device, session_id="sess_1", seconds=seconds,
                         reason=reason, closed_at=at)


class TestCostModel(unittest.TestCase):
    def test_published_rate(self):
        self.assertEqual(USD_PER_SESSION_MINUTE, 0.05)
        self.assertAlmostEqual(session_cost_usd(60.0), 0.05, places=10)
        self.assertAlmostEqual(session_cost_usd(3600.0), 3.00, places=8)

    def test_billed_per_second_not_rounded_up_to_whole_minutes(self):
        # 61 s is 61 seconds of voice, not two minutes.
        self.assertAlmostEqual(session_cost_usd(61.0), 61 * 0.05 / 60, places=10)
        self.assertLess(session_cost_usd(61.0), session_cost_usd(120.0))
        self.assertAlmostEqual(session_cost_usd(1.0), 0.05 / 60, places=10)

    def test_a_day_of_always_connected_idle_sessions(self):
        # ~407 session-minutes: two mostly idle, always-connected devices.
        self.assertAlmostEqual(session_cost_usd(407 * 60), 20.35, places=2)

    def test_zero_and_negative_seconds_cost_nothing(self):
        self.assertEqual(session_cost_usd(0.0), 0.0)
        self.assertEqual(session_cost_usd(-5.0), 0.0)

    def test_projection_is_priced_and_rounded_for_display(self):
        projected = projected_cost_usd(90.0, sessions=3)
        self.assertEqual(projected["sessions"], 3)
        self.assertEqual(projected["session_minutes"], 1.5)
        self.assertAlmostEqual(projected["usd"], 0.075, places=6)


class TestSpendLedger(unittest.TestCase):
    def ledger(self, clock, **kwargs):
        kwargs.setdefault("path", None)
        return LiveSpendLedger(clock=clock, **kwargs)

    def test_defaults_are_a_real_cap(self):
        self.assertEqual(DEFAULT_MAX_SESSION_MINUTES_PER_DAY, 60.0)
        self.assertEqual(DEFAULT_MAX_USD_PER_DAY, 3.0)
        # The two defaults agree: 60 session-minutes IS $3.00.
        self.assertAlmostEqual(session_cost_usd(60 * 60), DEFAULT_MAX_USD_PER_DAY, places=8)

    def test_records_and_prices_sessions(self):
        clock = Clock()
        ledger = self.ledger(clock)
        ledger.record(charge(30.0, clock()))
        ledger.record(charge(90.0, clock()))
        self.assertEqual(ledger.sessions_total, 2)
        self.assertAlmostEqual(ledger.window_seconds, 120.0)
        self.assertAlmostEqual(ledger.window_usd, 0.1, places=8)

    def test_window_rolls_off_old_charges(self):
        clock = Clock()
        ledger = self.ledger(clock)
        ledger.record(charge(600.0, clock()))
        self.assertAlmostEqual(ledger.window_seconds, 600.0)
        clock.advance(25 * 3600)
        self.assertEqual(ledger.window_seconds, 0.0)
        self.assertTrue(ledger.check().allowed, "a new day starts with a fresh budget")

    def test_session_minute_cap_is_a_hard_cutoff(self):
        clock = Clock()
        ledger = self.ledger(clock, max_session_minutes_per_day=10.0, max_usd_per_day=0.0)
        ledger.record(charge(9 * 60, clock()))
        self.assertTrue(ledger.check().allowed)
        ledger.record(charge(1 * 60, clock()))
        verdict = ledger.check()
        self.assertFalse(verdict.allowed)
        self.assertIn("cutoff reached", verdict.reason)
        self.assertIn("10", verdict.reason)

    def test_dollar_cap_is_a_hard_cutoff(self):
        clock = Clock()
        ledger = self.ledger(clock, max_session_minutes_per_day=0.0, max_usd_per_day=0.50)
        ledger.record(charge(9 * 60, clock()))  # $0.45
        self.assertTrue(ledger.check().allowed)
        ledger.record(charge(2 * 60, clock()))  # $0.55 total
        self.assertFalse(ledger.check().allowed)

    def test_whichever_cap_binds_first_wins(self):
        clock = Clock()
        # 5 minutes of voice is $0.25, so the dollar cap binds first.
        ledger = self.ledger(clock, max_session_minutes_per_day=60.0, max_usd_per_day=0.10)
        ledger.record(charge(3 * 60, clock()))
        self.assertFalse(ledger.check().allowed)

    def test_the_open_session_counts_against_the_cutoff(self):
        # A session that never closes must not be able to outrun the cap.
        clock = Clock()
        ledger = self.ledger(clock, max_session_minutes_per_day=1.0, max_usd_per_day=0.0)
        self.assertTrue(ledger.check().allowed)
        self.assertTrue(ledger.check(open_seconds=30.0).allowed)
        self.assertFalse(ledger.check(open_seconds=61.0).allowed)

    def test_both_caps_zero_means_uncapped(self):
        clock = Clock()
        ledger = self.ledger(clock, max_session_minutes_per_day=0.0, max_usd_per_day=0.0)
        ledger.record(charge(10 * 3600, clock()))
        self.assertIsNone(ledger.remaining_seconds())
        self.assertTrue(ledger.check().allowed)

    def test_remaining_seconds_shrinks_as_spend_accrues(self):
        clock = Clock()
        ledger = self.ledger(clock, max_session_minutes_per_day=10.0, max_usd_per_day=0.0)
        self.assertAlmostEqual(ledger.remaining_seconds(), 600.0)
        ledger.record(charge(120.0, clock()))
        self.assertAlmostEqual(ledger.remaining_seconds(), 480.0)

    def test_a_zero_second_session_still_counts_as_a_cycle(self):
        clock = Clock()
        ledger = self.ledger(clock)
        ledger.record(charge(0.0, clock(), reason="open failed"))
        self.assertEqual(ledger.sessions_total, 1)
        self.assertEqual(ledger.window_seconds, 0.0)

    def test_snapshot_is_priced_and_names_what_is_excluded(self):
        clock = Clock()
        ledger = self.ledger(clock, max_session_minutes_per_day=10.0, max_usd_per_day=1.0)
        ledger.record(charge(60.0, clock()))
        snapshot = ledger.snapshot(open_seconds=30.0)
        self.assertEqual(snapshot["session_seconds"], 90.0)
        self.assertEqual(snapshot["session_minutes"], 1.5)
        self.assertAlmostEqual(snapshot["usd"], 0.075, places=6)
        self.assertEqual(snapshot["usd_per_session_minute"], 0.05)
        self.assertEqual(snapshot["cap_session_minutes"], 10.0)
        self.assertIn("billed separately", snapshot["note"])

    def test_a_restart_cannot_reset_the_days_budget(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live_spend.json")
            first = LiveSpendLedger(clock=clock, path=path,
                                    max_session_minutes_per_day=10.0, max_usd_per_day=0.0)
            first.record(charge(10 * 60, clock()))
            self.assertFalse(first.check().allowed)

            restarted = LiveSpendLedger(clock=clock, path=path,
                                        max_session_minutes_per_day=10.0, max_usd_per_day=0.0)
            self.assertAlmostEqual(restarted.window_seconds, 600.0)
            self.assertFalse(restarted.check().allowed, "the cutoff survives a restart")

    def test_persisted_charges_outside_the_window_are_dropped_on_load(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live_spend.json")
            with open(path, "w") as f:
                json.dump({"charges": [[clock() - 30 * 3600, 600.0],
                                       [clock() - 60, 60.0]]}, f)
            ledger = LiveSpendLedger(clock=clock, path=path)
            self.assertAlmostEqual(ledger.window_seconds, 60.0)

    def test_corrupt_persistence_fails_closed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live_spend.json")
            with open(path, "w") as f:
                f.write("{not json")
            ledger = LiveSpendLedger(clock=clock, path=path)
            self.assertEqual(ledger.window_seconds, 0.0)
            self.assertFalse(ledger.check().allowed)
            self.assertTrue(ledger.persistence_error)


if __name__ == "__main__":
    unittest.main()
