"""Regression checks for accidental-wake instruction precedence."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.turn_admission import turn_admission_instructions


class TestTurnAdmission(unittest.TestCase):
    def test_accidental_wake_silence_overrides_clarification(self) -> None:
        operator = "If you are unsure what the user means, ask a clarifying question."
        policy = turn_admission_instructions()
        effective = operator + policy
        normalized = " ".join(policy.split())

        self.assertTrue(effective.endswith(policy))
        self.assertIn("THIS OVERRIDES EVERY CLARIFICATION RULE ABOVE", policy)
        self.assertIn(
            "produce no spoken output, call no tool, and ask no question", normalized
        )
        self.assertIn("Unclear audio is not an incomplete request", normalized)

    def test_legitimate_incomplete_request_may_still_be_clarified(self) -> None:
        policy = turn_admission_instructions()

        self.assertIn("plausible request", policy)
        self.assertIn("missing one necessary detail", policy)
