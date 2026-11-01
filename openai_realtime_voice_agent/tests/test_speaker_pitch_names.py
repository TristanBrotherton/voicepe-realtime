"""Pitch alone names nobody when it could be the wrong person (app/speaker_context.py)."""
import unittest
from unittest import mock

from app import speaker_context
from app.speaker_context import SpeakerProbe, router_note, verdict_text

MALE, FEMALE = "Alex", "Sam"


def patched(print_verdict, pitch, enrolled=True):
    patches = (
        mock.patch.object(speaker_context.voiceprint, "identify", return_value=print_verdict),
        mock.patch.object(speaker_context.voiceprint, "available", return_value=enrolled),
        mock.patch.object(speaker_context, "classify_gender", return_value=pitch),
    )
    for patch in patches:
        patch.start()
    return patches


def identify(print_verdict, pitch, enrolled=True):
    """_identify's (label, name, metric, method) for a voice print and pitch verdict."""
    probe = SpeakerProbe(MALE, FEMALE)
    patches = patched(print_verdict, pitch, enrolled)
    try:
        return probe, probe._identify(b"")
    finally:
        for patch in patches:
            patch.stop()


async def run_verdict(print_verdict, pitch, enrolled=True):
    probe = SpeakerProbe(MALE, FEMALE)
    patches = patched(print_verdict, pitch, enrolled)
    try:
        await probe._classify(b"")
    finally:
        for patch in patches:
            patch.stop()
    return probe


class PitchNames(unittest.TestCase):
    def name(self, print_verdict, pitch, enrolled=True):
        return identify(print_verdict, pitch, enrolled)[1][1]

    def test_a_voice_print_match_still_names(self):
        self.assertEqual(self.name(("match", "alex", 0.45), ("female", 210.0, 90)), MALE)

    def test_an_uncertain_print_for_the_other_person_names_nobody(self):
        self.assertIsNone(self.name(("uncertain", "alex", 0.34), ("female", 210.0, 90)))

    def test_an_uncertain_print_that_pitch_agrees_with_names(self):
        self.assertEqual(self.name(("uncertain", "alex", 0.34), ("male", 108.0, 90)), MALE)
        self.assertEqual(self.name(("uncertain", "sam", 0.34), ("female", 210.0, 90)), FEMALE)

    def test_an_agreeing_print_does_not_rescue_a_pitch_near_the_gap(self):
        self.assertIsNone(self.name(("uncertain", "alex", 0.34), ("male", 140.0, 90)))
        self.assertIsNone(self.name(("uncertain", "sam", 0.34), ("female", 170.0, 90)))

    def test_an_unknown_print_names_nobody(self):
        self.assertIsNone(self.name(("unknown", None, 0.2), ("female", 258.0, 90)))
        self.assertIsNone(self.name(("unknown", None, 0.2), ("male", 108.0, 90)))

    def test_too_little_audio_for_an_enrolled_print_names_nobody(self):
        self.assertIsNone(self.name(("unavailable", None, 0.0), ("female", 210.0, 90)))

    def test_pitch_only_setup_names_away_from_the_gap(self):
        self.assertEqual(self.name(("unavailable", None, 0.0), ("male", 108.0, 90), enrolled=False), MALE)
        self.assertEqual(self.name(("unavailable", None, 0.0), ("female", 210.0, 90), enrolled=False), FEMALE)
        self.assertIsNone(self.name(("unavailable", None, 0.0), ("male", 140.0, 90), enrolled=False))
        self.assertIsNone(self.name(("unavailable", None, 0.0), ("female", 170.0, 90), enrolled=False))

    def test_the_voice_type_stays_for_tool_gating(self):
        _, (label, name, _, method) = identify(("unknown", None, 0.2), ("male", 108.0, 90))
        self.assertEqual((label, name), ("male", None))
        self.assertTrue(method.startswith("pitch(vp=unknown"))


class EveryNameFlow(unittest.IsolatedAsyncioTestCase):
    """The two reported turns: no flow says the name pitch guessed."""

    async def test_no_flow_names_anyone(self):
        for print_verdict, pitch in ((("uncertain", "alex", 0.34), ("female", 210.0, 90)),
                                     (("unknown", None, 0.25), ("female", 258.0, 90))):
            probe = await run_verdict(print_verdict, pitch)
            self.assertIsNone(probe.current_name)
            self.assertIsNone(probe.verdict_name())
            self.assertIsNone(probe.progress_name())
            self.assertNotIn(FEMALE, router_note(probe))
            self.assertIn("not identified", router_note(probe))
            voice = verdict_text(probe, probe.current_label, probe.current_name, probe.current_f0)
            self.assertIn("no names", voice)
            self.assertNotIn("matches", voice)

    async def test_a_memory_note_keeps_working_without_a_name(self):
        probe = await run_verdict(("unknown", None, 0.25), ("female", 258.0, 90))
        self.assertEqual(probe.memory_author(), "a household member")
        probe = await run_verdict(("match", "sam", 0.5), ("female", 210.0, 90))
        self.assertEqual(probe.memory_author(), FEMALE)
        self.assertIsNone(SpeakerProbe(MALE, FEMALE).memory_author())

    async def test_a_name_the_checks_agree_on_reaches_the_voice_and_backend_only(self):
        probe = await run_verdict(("uncertain", "sam", 0.34), ("female", 210.0, 90))
        self.assertEqual(probe.verdict_name(), FEMALE)
        self.assertIn(FEMALE, verdict_text(probe, probe.current_label, probe.current_name, 210.0))
        self.assertIn("may be wrong", router_note(probe))
        self.assertIsNone(probe.progress_name())

    async def test_a_stale_verdict_gives_no_name(self):
        probe = await run_verdict(("match", "sam", 0.5), ("female", 210.0, 90))
        self.assertEqual(probe.verdict_name(), FEMALE)
        probe._verdict_at = 0.0
        self.assertIsNone(probe.verdict_name())


if __name__ == "__main__":
    unittest.main()
