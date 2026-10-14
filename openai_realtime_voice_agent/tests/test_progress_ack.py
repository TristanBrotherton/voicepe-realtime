"""Generic, safe and personalized progress acknowledgements."""
import ast
import asyncio
import re
import threading
import unittest
from pathlib import Path

import app.progress_ack as progress_ack_module
from app.main import SafeRealtimeLLMService
from app.progress_ack import (
    GENERIC_CUES,
    PERSON_LOCATION_CUE,
    early_progress_phrase,
    generic_progress_phrase,
    looks_like_instant_local,
    looks_like_person_location,
    looks_like_request,
    progress_phrase,
)
from app.speaker_context import SpeakerProbe
from app.spoken_prompts import PROMPTS
from app.turn_timeline import TurnTimeline

# A spoken phrase must never carry a person's name or a gender: this add-on is
# public and the household it runs in is not.
GENDERED = re.compile(r"\b(?:she|he|her|hers|him|his)\b", re.IGNORECASE)
CAPITALISED_WORD = re.compile(r"[A-Z][a-z]+")


class _Probe:
    def __init__(self, label="male"):
        self.label = label

    def gate_speaker(self):
        return self.label

    def name_for(self, label):
        return {"male": "Sam", "female": "Alex"}.get(label)


class _Prompts:
    enabled = True
    progress_delay_s = 0.01

    def __init__(self):
        self.spoken = []

    async def say_text(self, text, device_id):
        self.spoken.append((text, device_id))
        return True


class TestPhraseSelection(unittest.TestCase):
    def test_request_gate_rejects_asides(self):
        self.assertFalse(looks_like_request("wow"))
        self.assertFalse(looks_like_request("thank you"))
        self.assertTrue(looks_like_request("where is Alex"))
        self.assertTrue(looks_like_request("turn on the kitchen lights"))
        self.assertTrue(looks_like_instant_local("What time is it?"))
        self.assertTrue(looks_like_instant_local("What's the date?"))
        self.assertTrue(looks_like_instant_local("find out the current time in Tokyo"))
        self.assertFalse(looks_like_instant_local("Where is Alex?"))

    def test_lookup_personalizes_only_when_name_supplied(self):
        self.assertEqual(progress_phrase("ask_openclaw", {}, "Sam"),
                         "Let me check that, Sam.")
        self.assertEqual(progress_phrase("web_search", {}, None),
                         "Let me check that.")

    def test_early_lookup_phrases_are_narrow_and_contextual(self):
        self.assertEqual(early_progress_phrase("search the web for today's weather"),
                         "Let me check, one second.")
        self.assertEqual(early_progress_phrase("where is Alex right now"),
                         "I’ll find them, let me look.")
        self.assertIsNone(early_progress_phrase("what time is it"))
        self.assertIsNone(early_progress_phrase("find out the current time in Tokyo"))
        self.assertIsNone(early_progress_phrase("look up the time in Tokyo"))
        self.assertIsNone(early_progress_phrase("find out the date in Tokyo"))
        self.assertIsNone(early_progress_phrase("search the web for the time in Paris"))
        self.assertIsNone(early_progress_phrase("check online for the date in London"))
        self.assertIsNone(early_progress_phrase("could you check this for me"))

    def test_car_climate_is_matched_on_tokens_not_on_an_installation_id(self):
        """Keyed on the name's tokens, so no script id is a public default."""
        for name in ("script__car_car_climate_on", "intent_script__CarClimateOn",
                     "car_climate_on"):
            with self.subTest(name=name):
                self.assertEqual(progress_phrase(name),
                                 "Starting the car climate.")
        self.assertEqual(progress_phrase("climate__HassClimateSetTemperature",
                                         {"area": "Kitchen"}),
                         "Adjusting the Kitchen.")

    def test_shopping_uses_structured_terms_without_claiming_success(self):
        phrase = progress_phrase("todo__HassListAddItem",
                                 {"item": "cookies", "list": "shopping list"}, "Alex")
        self.assertEqual(phrase, "Adding cookies to the shopping list, Alex.")
        self.assertNotIn("Added", phrase)

    def test_lights_use_area_and_domain(self):
        self.assertEqual(
            progress_phrase("intent__HassTurnOn", {"area": "Kitchen", "domain": ["light"]}),
            "I’ll turn on the Kitchen lights for you.",
        )
        self.assertEqual(
            progress_phrase("intent__HassTurnOff", {"name": "bedside lamp", "domain": ["light"]}),
            "I’ll turn off the bedside lamp for you.",
        )

    def test_entity_ids_and_long_text_are_not_spoken(self):
        self.assertEqual(
            progress_phrase("intent__HassTurnOn", {"name": "light.kitchen_main"}),
            "I’ll turn on that for you.",
        )
        phrase = progress_phrase("todo__HassListAddItem", {"item": "x" * 80})
        self.assertEqual(phrase, "Adding that to the shopping list.")

    def test_new_wake_clears_previous_speaker_before_personalizing(self):
        probe = SpeakerProbe("Sam", "Alex")
        probe.current_label = "male"
        probe.current_f0 = 120.0
        probe.current_name = "Sam"
        probe.current_method = "voiceprint"
        probe._verdict_at = 1.0
        probe.start_capture()
        self.assertEqual(probe.gate_speaker(), "unknown")
        self.assertIsNone(probe.progress_name())

    def test_progress_name_requires_fresh_enrolled_voiceprint(self):
        import time
        probe = SpeakerProbe("Sam", "Alex")
        probe.current_label = "male"
        probe.current_name = "Sam"
        probe.current_method = "pitch(vp=nonmatch:0.22)"
        probe._verdict_at = time.monotonic()
        self.assertIsNone(probe.progress_name(), "pitch must not name a guest")
        probe.current_method = "voiceprint"
        self.assertEqual(probe.progress_name(), "Sam")


class TestPersonLocationCueIsGeneric(unittest.TestCase):
    """The person-location cue is chosen by request shape, never by a name.

    It used to key on one household member's given name and a gendered
    pronoun, which is how a real person's name came to be in a public
    repository. The information the cue actually depends on is the *shape* of
    the request — "where is <someone>" is a slow lookup whoever is being asked
    about — so one pre-warmed phrase now serves everybody, and the public
    add-on carries nobody's name.
    """

    PRONOUN_REQUESTS = ("where is she", "where's he right now",
                        "Where are they?", "find her", "please locate them")
    NAMED_REQUESTS = ("where is Alex", "Where's Morgan right now?",
                      "where is Priya", "find Jordan", "locate Wei")
    NOT_PEOPLE = ("where is my phone", "where are the car keys",
                  "where is the nearest pharmacy", "find my glasses",
                  "where is the kitchen light")

    def test_pronoun_person_location_requests_still_get_the_cue(self):
        for transcript in self.PRONOUN_REQUESTS:
            with self.subTest(transcript=transcript):
                self.assertTrue(looks_like_person_location(transcript))
                self.assertEqual(early_progress_phrase(transcript),
                                 PERSON_LOCATION_CUE)

    def test_every_name_gets_the_same_cue_so_no_name_is_special_cased(self):
        for transcript in self.NAMED_REQUESTS:
            with self.subTest(transcript=transcript):
                self.assertTrue(looks_like_person_location(transcript))
        self.assertEqual(
            {early_progress_phrase(t) for t in self.NAMED_REQUESTS},
            {PERSON_LOCATION_CUE},
            "the cue must not depend on who is being asked about")

    def test_requests_about_things_and_places_get_no_person_cue(self):
        for transcript in self.NOT_PEOPLE:
            with self.subTest(transcript=transcript):
                self.assertFalse(looks_like_person_location(transcript))
                self.assertIsNone(early_progress_phrase(transcript))

    def test_the_cue_is_one_of_the_pre_warmed_prompts(self):
        """An un-warmed phrase is a synthesis round trip in the dead air."""
        self.assertIn(PERSON_LOCATION_CUE, PROMPTS.values())

    def test_no_spoken_prompt_is_gendered(self):
        for key, value in PROMPTS.items():
            with self.subTest(prompt=key):
                self.assertIsNone(GENDERED.search(value))

    def test_no_phrase_this_module_can_speak_carries_a_personal_name(self):
        """Sweep every tool name the module mentions, not a chosen few.

        A name literal in one branch is exactly how the leak happened, so the
        guard is the whole dispatch table: with no arguments and no speaker,
        every phrase must be free of gendering and of any capitalised word
        other than its own first.
        """
        source = Path(progress_ack_module.__file__).read_text(encoding="utf-8")
        names = sorted({
            node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,}", node.value)
        })
        self.assertGreater(len(names), 20,
                           "the sweep found almost no tool names; it is "
                           "inspecting the wrong thing")
        for name in names + ["", "script__something_unknown"]:
            phrase = progress_phrase(name)
            with self.subTest(tool=name, phrase=phrase):
                self.assertIsNone(GENDERED.search(phrase))
                self.assertEqual(
                    [found.start() for found in CAPITALISED_WORD.finditer(phrase)
                     if found.start()], [],
                    "a capitalised word mid-phrase is a name the runtime "
                    "should not know")

    def test_the_early_cues_carry_no_name_either(self):
        for transcript in self.PRONOUN_REQUESTS + self.NAMED_REQUESTS + (
                "search the web for today's weather", "look up the news"):
            phrase = early_progress_phrase(transcript)
            with self.subTest(transcript=transcript):
                self.assertIsNotNone(phrase)
                self.assertIsNone(GENDERED.search(phrase))
                self.assertEqual(
                    [found.start() for found in CAPITALISED_WORD.finditer(phrase)
                     if found.start()], [])


class TestGenericCuesAreFiniteAndSafe(unittest.TestCase):
    """The cues a Live-voice install can play: pre-rendered, nameless, never a claim."""

    def test_every_generic_cue_is_a_pre_warmed_prompt(self):
        for key, text in GENERIC_CUES.items():
            with self.subTest(cue=key):
                self.assertEqual(PROMPTS[key], text)

    def test_the_selection_only_ever_returns_a_generic_cue(self):
        source = Path(progress_ack_module.__file__).read_text(encoding="utf-8")
        names = sorted({
            node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,}", node.value)
        })
        for name in names + ["", "script__something_unknown", "script__car_car_climate_on"]:
            for transcript in ("", "turn off the bedside lamp", "add cookies to the shopping list",
                               "set a timer for ten minutes", "where is Alex"):
                phrase = generic_progress_phrase(name, transcript)
                with self.subTest(tool=name, transcript=transcript):
                    self.assertIn(phrase, GENERIC_CUES.values())
                    self.assertIsNone(GENDERED.search(phrase))
                    self.assertEqual(
                        [found.start() for found in CAPITALISED_WORD.finditer(phrase)
                         if found.start()], [])

    def test_generic_cues_follow_the_tool_shape_and_claim_nothing(self):
        self.assertEqual(generic_progress_phrase("intent__HassTurnOff", "turn off the lamp"),
                         "I’ll turn that off for you.")
        self.assertEqual(generic_progress_phrase("intent__HassTurnOn"),
                         "I’ll turn that on for you.")
        self.assertEqual(generic_progress_phrase("todo__HassListAddItem"),
                         "Adding that to the list.")
        self.assertEqual(generic_progress_phrase("web_search"), "Let me check that.")
        self.assertEqual(generic_progress_phrase("", "turn off the hallway lights"),
                         "I’ll turn that off for you.")
        self.assertEqual(generic_progress_phrase("", "could you check this"),
                         "Let me check that.")
        for text in GENERIC_CUES.values():
            with self.subTest(text=text):
                for claim in ("done", "turned", "added", "set."):
                    self.assertNotIn(claim, text.lower())


class TestProgressController(unittest.IsolatedAsyncioTestCase):
    def make_service(self, transcript="where is Alex", speaker="male"):
        service = object.__new__(SafeRealtimeLLMService)
        service.spoken_prompts = _Prompts()
        service.turn_timeline = TurnTimeline("kitchen")
        service.turn_timeline.begin("w1")
        service.speaker_probe = _Probe(speaker)
        service._last_user_text = transcript
        return service

    async def test_structured_tool_replaces_generic_before_playback(self):
        service = self.make_service("add cookies to the shopping list")
        service.schedule_progress_ack()
        service._set_progress_context("todo__HassListAddItem",
                                      {"item": "cookies", "list": "shopping list"})
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken,
                         [("Adding cookies to the shopping list, Sam.", "kitchen")])

    async def test_uncertain_current_speaker_stays_neutral(self):
        service = self.make_service(speaker="unknown")
        service.schedule_progress_ack()
        service._set_progress_context("web_search", {"query": "weather"})
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken,
                         [("Let me check that.", "kitchen")])

    async def test_cancel_before_deadline_is_silent(self):
        service = self.make_service()
        service.schedule_progress_ack()
        service.cancel_progress_ack()
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken, [])

    async def test_aside_does_not_arm(self):
        service = self.make_service("wow")
        service.schedule_progress_ack()
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken, [])

    async def test_instant_local_request_does_not_arm(self):
        service = self.make_service("what time is it")
        service.schedule_progress_ack()
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken, [])

    async def test_tool_free_thinking_does_not_guess_intent(self):
        service = self.make_service("could you check the time for me")
        service.schedule_progress_ack()
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken, [])
        service.cancel_progress_ack()

    async def test_obvious_web_lookup_gets_early_prewarmed_cue(self):
        service = self.make_service("search the web for today's weather in Lisbon")
        service.schedule_progress_ack()
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken,
                         [("Let me check, one second.", "kitchen")])

    async def test_person_location_gets_early_contextual_cue(self):
        service = self.make_service("where is Alex")
        service.schedule_progress_ack()
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken,
                         [("I’ll find them, let me look.", "kitchen")])

    async def test_late_silent_tool_never_plays(self):
        service = self.make_service("what is the current time in Tokyo")
        service.schedule_progress_ack()
        service.spoken_prompts.is_silent = lambda name: name.endswith("GetDateTime")
        await asyncio.sleep(0.03)
        service._set_progress_context("llm__GetDateTime", {})
        await asyncio.sleep(0.01)
        self.assertEqual(service.spoken_prompts.spoken, [])

    async def test_late_getdatetime_after_find_out_stays_silent(self):
        for transcript in (
            "find out the current time in Tokyo",
            "look up the time in Tokyo",
            "find out the date in Tokyo",
            "search the web for the time in Paris",
            "check online for the date in London",
        ):
            with self.subTest(transcript=transcript):
                service = self.make_service(transcript)
                service.schedule_progress_ack()
                service.spoken_prompts.is_silent = lambda name: name.endswith("GetDateTime")
                await asyncio.sleep(0.03)
                service._set_progress_context("llm__GetDateTime", {})
                await asyncio.sleep(0.01)
                self.assertEqual(service.spoken_prompts.spoken, [])

    async def test_silent_tool_cancels_phase_level_ack(self):
        service = self.make_service("what is the current time in Tokyo")
        service.schedule_progress_ack()
        service.spoken_prompts.is_silent = lambda name: name.endswith("GetDateTime")
        service._set_progress_context("llm__GetDateTime", {})
        await asyncio.sleep(0.03)
        self.assertEqual(service.spoken_prompts.spoken, [])


class TestSpeakerVerdictRace(unittest.IsolatedAsyncioTestCase):
    async def test_old_classification_cannot_name_new_wake(self):
        probe = SpeakerProbe("Sam", "Alex")
        started = threading.Event()
        release = threading.Event()

        def delayed(_data):
            started.set()
            release.wait(timeout=2)
            return "male", "Sam", 0.91, "voiceprint"

        probe._identify = delayed
        probe._capture_gen = 1
        old = asyncio.create_task(probe._classify(b"old", 1))
        await asyncio.to_thread(started.wait, 1)
        probe.start_capture()
        release.set()
        await old
        self.assertEqual(probe.gate_speaker(), "unknown")
        self.assertIsNone(probe.progress_name())

if __name__ == "__main__":
    unittest.main()
