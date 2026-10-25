"""Spoken device names fitted to the entity Home Assistant has (app/ha_names.py).

The home below is fictional; its shapes are the real failure cases: a toggle
helper asked for as a "switch", spelled-out letters, a partial name, a plural,
a room asked for with a kind of device, and names that only look alike.
"""
import unittest

from app.ha_household import parse_overview
from app.ha_names import entities_from_overview, is_entity_tool, resolve_target, words

PROMPT = """\
- names: Lab PE Test Switch
  domain: input_boolean
  areas: Studio
- names: Lab PE Live Test Light
  domain: light
  areas: Studio
- names: Porch Charger, Car charger
  domain: switch
  areas: Driveway
- names: Quiet Mode (Visitors), Visitor mode
  domain: input_boolean
- names: Attic Light
  domain: light
  areas: Attic
- names: Attic Fan Plug Switch 3F21
  domain: switch
  areas: Attic
- names: Green Glow
  domain: scene
- names: Battery Scheduler
  domain: switch
- names: Home Battery Percentage
  domain: sensor
- names: Dryer status, Laundry status
  domain: sensor
- names: Washer status, Laundry status
  domain: sensor
- names: Garden Lamp
  domain: light
  areas: Garden
"""

E = entities_from_overview(parse_overview(PROMPT))
FLOORS = {"studio": ["Ground floor", "downstairs"], "attic": ["Top floor"], "garden": [], "driveway": []}


def resolve(args, tool="intent__HassTurnOn"):
    return resolve_target(tool, args, E, FLOORS)


class TestWords(unittest.TestCase):
    def test_spelled_out_letters_and_fillers(self):
        self.assertEqual(words("the Lab P.E. test switch"), ["lab", "pe", "test", "switch"])


class TestResolveTarget(unittest.TestCase):
    def test_a_switch_that_is_a_helper_gets_the_helpers_domain(self):
        """The reported case: domain 'switch' for an input_boolean matched nothing."""
        out, note = resolve({"name": "Lab PE Test Switch", "domain": "switch", "area": "Studio"},
                            tool="homeassistant__GetLiveContext")
        self.assertEqual(out, {"name": "Lab PE Test Switch", "domain": "input_boolean", "area": "Studio"})
        self.assertIn("domain switch -> input_boolean", note)

    def test_domain_keeps_its_list_shape(self):
        out, _ = resolve({"name": "Lab PE Test Switch", "domain": ["switch"]})
        self.assertEqual(out["domain"], ["input_boolean"])

    def test_spelled_out_letters_get_home_assistants_spelling(self):
        out, _ = resolve({"name": "lab P.E. test switch"})
        self.assertEqual(out["name"], "Lab PE Test Switch")

    def test_a_partial_name_only_one_entity_holds(self):
        out, _ = resolve({"name": "the test switch", "domain": ["switch"]})
        self.assertEqual(out, {"name": "Lab PE Test Switch", "domain": ["input_boolean"]})

    def test_a_plural_and_a_wrong_area(self):
        out, note = resolve({"name": "lab pe live test lights", "area": "Cellar"})
        self.assertEqual(out, {"name": "Lab PE Live Test Light"})
        self.assertIn("area Cellar dropped", note)

    def test_a_floor_the_entity_is_not_on_is_dropped(self):
        out, _ = resolve({"name": "lab pe live test lights", "floor": "Top floor"})
        self.assertNotIn("floor", out)
        out, _ = resolve({"name": "lab pe live test lights", "floor": "downstairs"})
        self.assertEqual(out["floor"], "downstairs")

    def test_a_misspelt_alias(self):
        out, _ = resolve({"name": "visiter mode"})
        self.assertEqual(out["name"], "Quiet Mode (Visitors)")

    def test_an_exact_name_or_alias_that_fits_is_left_alone(self):
        for args in ({"name": "Attic Light", "domain": ["light"]}, {"name": "car charger"},
                     {"name": "attic light"}):
            self.assertEqual(resolve(args), (args, None), args)

    def test_a_room_with_a_kind_of_device_is_left_to_home_assistant(self):
        for name in ("attic lights", "garden lights", "studio light"):
            args = {"name": name, "domain": ["light"]}
            self.assertEqual(resolve(args), (args, None), name)

    def test_lookalikes_of_another_domain_are_not_guessed(self):
        """'Garden glow' must not become the Green Glow scene, nor 'attic
        light switch' a plug of another domain."""
        for args in ({"name": "garden glow", "domain": ["light"]},
                     {"name": "attic light switch", "domain": ["light"]}):
            out, note = resolve(args)
            self.assertNotIn(out.get("name"), ("Green Glow", "Attic Fan Plug Switch 3F21"), args)

    def test_a_word_several_entities_share_is_not_guessed(self):
        args = {"name": "battery"}
        self.assertEqual(resolve(args, tool="homeassistant__GetLiveContext"), (args, None))

    def test_a_name_two_entities_share_is_not_guessed(self):
        args = {"name": "laundry status"}
        self.assertEqual(resolve(args, tool="homeassistant__GetLiveContext"), (args, None))

    def test_tools_whose_name_is_not_an_entity_are_never_touched(self):
        for tool in ("intent__HassListAddItem", "intent__HassStartTimer", "intent__HassBroadcast", "web_search"):
            self.assertFalse(is_entity_tool(tool), tool)
            args = {"name": "lab pe test swich"}
            self.assertEqual(resolve(args, tool=tool), (args, None))

    def test_no_entities_or_no_name_changes_nothing(self):
        self.assertEqual(resolve_target("intent__HassTurnOn", {"name": "x"}, []), ({"name": "x"}, None))
        self.assertEqual(resolve({"area": "Attic", "domain": ["light"]}),
                         ({"area": "Attic", "domain": ["light"]}, None))


if __name__ == "__main__":
    unittest.main()


class TestServiceWiring(unittest.TestCase):
    def test_the_live_service_fits_the_arguments_before_the_guards(self):
        from types import SimpleNamespace
        from app.ha_household import HouseholdDirectory
        from app.live_service import OpenAILiveLLMService

        directory = HouseholdDirectory(None, None)
        directory._entities = E
        service = SimpleNamespace(router=SimpleNamespace(household=directory))
        with self.assertLogs("app.live_service", "INFO") as logs:
            out = OpenAILiveLLMService._resolve_ha_target(
                service, "homeassistant__GetLiveContext", {"name": "Lab PE Test Switch", "domain": "switch"})
        self.assertEqual(out, {"name": "Lab PE Test Switch", "domain": "input_boolean"})
        self.assertIn("domain switch -> input_boolean", logs.output[0])
        # No router (or no directory): untouched.
        self.assertEqual(OpenAILiveLLMService._resolve_ha_target(
            SimpleNamespace(router=None), "intent__HassTurnOn", {"name": "x"}), {"name": "x"})
