"""Placeholder arguments are removed; real values, including zero, are not.

Driven by the verbatim payloads from the second canary's failed
"turn on Atrium Lamp" (``tests/live_fixtures.py``) and by the set of argument
shapes this household's own Home Assistant was measured to reject
(``fx.HA_REJECTED_SLOTS``). Both halves matter: dropping too little reproduces
the canary, and dropping too much silently changes what the user asked for.
"""
import json
import unittest

from app.tool_arguments import (
    PLACEHOLDER_ZEROS,
    canonical_tool_name,
    sanitize_tool_arguments,
    tool_schema_index,
)

from tests import live_fixtures as fx


HA_TURN_OFF = {"name": "intent__HassTurnOff", "parameters": {"type": "object", "properties": {
    "name": {"type": "string"}, "area": {"type": "string"}, "floor": {"type": "string"},
    "domain": {"type": "array", "items": {"type": "string"}},
    "device_class": {"type": "array", "items": {"type": "string"}}}}}
HA_LIGHT_SET = {"name": "light__HassLightSet", "parameters": {"type": "object", "properties": {
    "name": {"type": "string"}, "domain": {"type": "array", "items": {"type": "string"}},
    "brightness": {"type": "integer", "minimum": 0, "maximum": 100}}}}
HA_LIVE_CONTEXT = {"name": "homeassistant__GetLiveContext", "parameters": {"type": "object", "properties": {
    "domain": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}}}}


class TestArgumentsFitTheSchemaTypes(unittest.TestCase):
    """Home Assistant's MCP tools type domain and device_class as arrays;
    "domain": "light" was rejected and the kitchen lights stayed on (Kitchen
    2026-10-28). Values are reshaped to the declared type, never changed."""

    def test_a_scalar_domain_becomes_a_list(self):
        clean, _ = sanitize_tool_arguments("intent__HassTurnOff", {"area": "Kitchen", "domain": "light"},
                                           HA_TURN_OFF)
        self.assertEqual(clean, {"area": "Kitchen", "domain": ["light"]})

    def test_a_scalar_device_class_becomes_a_list(self):
        clean, _ = sanitize_tool_arguments("intent__HassTurnOn",
                                           {"name": "Atrium Lamp", "device_class": "shade"}, HA_TURN_OFF)
        self.assertEqual(clean["device_class"], ["shade"])

    def test_a_numeric_string_becomes_the_declared_integer(self):
        clean, _ = sanitize_tool_arguments("light__HassLightSet",
                                           {"name": "Atrium Lamp", "brightness": "40"}, HA_LIGHT_SET)
        self.assertEqual(clean["brightness"], 40)

    def test_a_shape_the_schema_already_allows_is_untouched(self):
        clean, _ = sanitize_tool_arguments("homeassistant__GetLiveContext", {"domain": "light"},
                                           HA_LIVE_CONTEXT)
        self.assertEqual(clean, {"domain": "light"})
        clean, _ = sanitize_tool_arguments("intent__HassTurnOff", {"area": "Kitchen", "domain": ["light"]},
                                           HA_TURN_OFF)
        self.assertEqual(clean["domain"], ["light"])

    def test_a_huge_numeric_string_is_exact_and_never_crashes(self):
        clean, _ = sanitize_tool_arguments("light__HassLightSet",
                                           {"name": "Atrium Lamp", "brightness": "9" * 400}, HA_LIGHT_SET)
        self.assertEqual(clean["brightness"], int("9" * 400))
        clean, _ = sanitize_tool_arguments("light__HassLightSet",
                                           {"name": "Atrium Lamp", "brightness": "9007199254740993"},
                                           HA_LIGHT_SET)
        self.assertEqual(clean["brightness"], 9007199254740993)

    def test_a_required_placeholder_is_not_reshaped(self):
        schema = {"name": "x__Tool", "parameters": {"type": "object", "required": ["domain"],
                                                    "properties": {"domain": {"type": "array"}}}}
        clean, _ = sanitize_tool_arguments("x__Tool", {"domain": ""}, schema)
        self.assertEqual(clean["domain"], "")

    def test_a_value_that_is_not_a_plain_form_is_left_for_the_server_to_reject(self):
        clean, _ = sanitize_tool_arguments("light__HassLightSet",
                                           {"name": "Atrium Lamp", "brightness": "bright"}, HA_LIGHT_SET)
        self.assertEqual(clean["brightness"], "bright")


class TestTheCanaryPayloads(unittest.TestCase):
    def test_hass_light_set_as_gpt_live_sent_it(self):
        clean, removed = sanitize_tool_arguments(
            "light__HassLightSet", json.loads(fx.LIGHT_SET_ARGUMENTS),
            fx.LIGHT_SET_TOOL,
        )
        self.assertEqual(clean, {
            "name": "Atrium Lamp", "area": "Kitchen",
            "domain": ["light"], "brightness": 100,
        })
        self.assertEqual(sorted(removed), ["color", "floor", "temperature"])

    def test_hass_turn_on_as_gpt_live_sent_it(self):
        clean, removed = sanitize_tool_arguments(
            "intent__HassTurnOn", json.loads(fx.TURN_ON_ARGUMENTS), fx.TURN_ON_TOOL,
        )
        self.assertEqual(clean, {
            "name": "Atrium Lamp", "area": "Kitchen", "domain": ["light"],
        })
        self.assertEqual(sorted(removed), ["device_class", "floor"])

    def test_nothing_home_assistant_rejects_survives(self):
        """Every measured rejection, applied to the canary's own payloads."""
        for tool, parameter, value, message in fx.HA_REJECTED_SLOTS:
            with self.subTest(tool=tool, parameter=parameter, message=message):
                arguments = {"name": "Atrium Lamp", parameter: value}
                clean, _removed = sanitize_tool_arguments(tool, arguments)
                if parameter == "name":
                    # The only target, and it is empty. Removing it would widen
                    # the call to every match, so it is kept and rejected.
                    self.assertEqual(clean.get("name"), value)
                    continue
                self.assertNotIn(parameter, clean,
                                 f"Home Assistant would answer: {message}")


class TestNeverBroadenACall(unittest.TestCase):
    """Dropping the last target turns "nothing" into "everything"."""

    def test_an_empty_only_target_is_kept_so_the_call_is_rejected(self):
        clean, removed = sanitize_tool_arguments(
            "intent__HassTurnOff", {"name": "", "domain": ["light"]})
        self.assertEqual(clean, {"name": "", "domain": ["light"]})
        self.assertEqual(removed, [],
                         "removing the only target would turn off every light")

    def test_all_three_targets_empty_are_all_kept(self):
        arguments = {"name": "", "area": "  ", "floor": "", "domain": ["light"]}
        clean, removed = sanitize_tool_arguments("intent__HassTurnOff", arguments)
        self.assertEqual(clean, arguments)
        self.assertEqual(removed, [])

    def test_an_empty_target_beside_a_real_one_is_dropped(self):
        """The canary's case: floor="" with a real name and area."""
        clean, removed = sanitize_tool_arguments(
            "intent__HassTurnOn",
            {"name": "Atrium Lamp", "area": "Kitchen", "floor": ""})
        self.assertEqual(clean, {"name": "Atrium Lamp", "area": "Kitchen"})
        self.assertEqual(removed, ["floor"])

    def test_an_area_only_call_keeps_its_area(self):
        clean, removed = sanitize_tool_arguments(
            "intent__HassTurnOn", {"name": "", "area": "Kitchen", "floor": ""})
        self.assertEqual(clean, {"area": "Kitchen"})
        self.assertEqual(sorted(removed), ["floor", "name"])

    def test_a_non_target_placeholder_is_dropped_even_with_no_real_target(self):
        clean, removed = sanitize_tool_arguments(
            "light__HassLightSet", {"name": "", "color": "", "brightness": 100})
        self.assertEqual(clean, {"name": "", "brightness": 100})
        self.assertEqual(removed, ["color"])

    def test_a_sanitized_call_keeps_the_targeting_the_user_asked_for(self):
        clean, _ = sanitize_tool_arguments(
            "light__HassLightSet", json.loads(fx.LIGHT_SET_ARGUMENTS),
            fx.LIGHT_SET_TOOL,
        )
        self.assertEqual(clean["name"], "Atrium Lamp")
        self.assertEqual(clean["area"], "Kitchen")
        self.assertEqual(clean["domain"], ["light"])
        self.assertEqual(clean["brightness"], 100)


class TestWhatIsKept(unittest.TestCase):
    def test_a_meaningful_zero_is_kept(self):
        """0 is off, closed and muted. Only an out-of-domain zero is noise."""
        for tool, parameter in (
            ("light__HassLightSet", "brightness"),
            ("intent__HassSetPosition", "position"),
            ("media_player__HassSetVolume", "volume_level"),
            ("climate__HassClimateSetTemperature", "temperature"),
            ("fan__HassFanSetSpeed", "percentage"),
        ):
            with self.subTest(tool=tool, parameter=parameter):
                clean, removed = sanitize_tool_arguments(
                    tool, {"name": "X", parameter: 0})
                self.assertEqual(clean.get(parameter), 0)
                self.assertEqual(removed, [])

    def test_only_colour_temperature_treats_zero_as_a_placeholder(self):
        self.assertEqual(PLACEHOLDER_ZEROS, frozenset({("HassLightSet", "temperature")}))
        # Same parameter name, different tool: 0 degrees is a real setpoint.
        clean, _ = sanitize_tool_arguments(
            "climate__HassClimateSetTemperature", {"name": "X", "temperature": 0})
        self.assertEqual(clean["temperature"], 0)
        clean, _ = sanitize_tool_arguments(
            "light__HassLightSet", {"name": "X", "temperature": 0})
        self.assertNotIn("temperature", clean)

    def test_false_is_a_value_not_an_absence(self):
        clean, removed = sanitize_tool_arguments("web_search", {"q": "x", "deep": False})
        self.assertEqual(clean["deep"], False)
        self.assertEqual(removed, [])

    def test_a_required_parameter_is_never_removed(self):
        schema = {"name": "x", "parameters": {"type": "object",
                                              "properties": {"item": {"type": "string"}},
                                              "required": ["item"]}}
        clean, removed = sanitize_tool_arguments("todo__HassListAddItem",
                                                 {"item": "", "list": ""}, schema)
        self.assertEqual(clean, {"item": ""},
                         "an empty required value is left for an honest error")
        self.assertEqual(removed, ["list"])

    def test_non_empty_values_are_passed_through_untouched(self):
        arguments = {"name": "Atrium Lamp", "domain": ["light"], "brightness": 50,
                     "color": "warm white", "nested": {"a": 1}}
        clean, removed = sanitize_tool_arguments("light__HassLightSet", arguments)
        self.assertEqual(clean, arguments)
        self.assertEqual(removed, [])

    def test_the_input_mapping_is_not_modified(self):
        arguments = {"name": "X", "floor": ""}
        snapshot = dict(arguments)
        sanitize_tool_arguments("intent__HassTurnOn", arguments)
        self.assertEqual(arguments, snapshot)


class TestPlaceholderShapes(unittest.TestCase):
    def test_every_placeholder_shape_is_removed(self):
        arguments = {"name": "X", "s": "", "ws": "   \t", "lst": [], "obj": {},
                     "tpl": ()}
        clean, removed = sanitize_tool_arguments("intent__HassTurnOn", arguments)
        self.assertEqual(clean, {"name": "X"})
        self.assertEqual(sorted(removed), ["lst", "obj", "s", "tpl", "ws"])

    def test_none_is_removed(self):
        clean, removed = sanitize_tool_arguments("intent__HassTurnOn",
                                                 {"name": "X", "floor": None})
        # None is not a placeholder shape this module claims to know about, so
        # it is passed through rather than guessed at.
        self.assertIn("floor", clean)
        self.assertEqual(removed, [])

    def test_no_arguments_is_handled(self):
        self.assertEqual(sanitize_tool_arguments("x", None), ({}, []))
        self.assertEqual(sanitize_tool_arguments("x", {}), ({}, []))


class TestHelpers(unittest.TestCase):
    def test_canonical_tool_name_strips_one_namespace(self):
        self.assertEqual(canonical_tool_name("light__HassLightSet"), "HassLightSet")
        self.assertEqual(canonical_tool_name("HassTurnOn"), "HassTurnOn")
        self.assertEqual(canonical_tool_name(""), "")

    def test_the_schema_index_is_keyed_by_the_declared_tool_name(self):
        index = tool_schema_index([fx.LIGHT_SET_TOOL, fx.TURN_ON_TOOL, {"no": "name"}])
        self.assertEqual(set(index), {"light__HassLightSet", "intent__HassTurnOn"})
        self.assertEqual(tool_schema_index(None), {})

    def test_a_missing_or_malformed_schema_still_sanitizes(self):
        for schema in (None, {}, {"parameters": "nonsense"},
                       {"parameters": {"required": "nonsense"}}):
            with self.subTest(schema=schema):
                clean, removed = sanitize_tool_arguments(
                    "intent__HassTurnOn", {"name": "X", "floor": ""}, schema)
                self.assertEqual(clean, {"name": "X"})
                self.assertEqual(removed, ["floor"])


if __name__ == "__main__":
    unittest.main()
