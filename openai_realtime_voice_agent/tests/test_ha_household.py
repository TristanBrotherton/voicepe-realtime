"""0.18.30: the household directory, room context and the NO_REQUEST veto.

Failures these cover:
- "Turn on boost heat": the helper was not exposed and the backend
  had no list of devices, so it guessed name='boost heat', area='downstairs'.
- "Turn on the kitchen lights": a clean command judged NO_REQUEST, 20 s of
  silence.
- "Turn on the lights": Home Assistant's debug command filled the area with
  the room it resolved last, for any satellite.
"""
import os
import tempfile
import unittest

from app.ha_household import (
    HouseholdDirectory, dashboard_entities, entities_to_expose, mentions, overview_block, parse_overview, satellite_mac,
)
from app.live_router import (
    ACTION, FAILED, NOTHING, UNCLEAR_TARGET_TEXT, BackendRouter, LocalMatch, TurnRouter,
)
from app.tool_results import classify_result

from tests.test_live_router import TOOLS, FakeBackend, message, response

PROMPT = """You ARE equipped to answer questions.

Static Context: An overview of the areas and the devices in this smart home:
- names: boost heat
  domain: input_boolean
- names: Kitchen Main
  domain: light
  areas: Kitchen
- names: workshop light
  domain: light
  areas: Workshop
- names: outside temperature
  domain: sensor

When controlling Home Assistant always call the intent tools."""

AREAS = [{"area_id": "kitchen", "name": "Kitchen", "aliases": [], "floor_id": "ground_floor"},
         {"area_id": "workshop", "name": "Workshop", "aliases": [], "floor_id": "outside"},
         {"area_id": "loft", "name": "loft", "aliases": ["attic"], "floor_id": "second_floor"}]
FLOORS = [{"floor_id": "ground_floor", "name": "ground floor", "aliases": ["downstairs"], "level": 1},
          {"floor_id": "second_floor", "name": "second floor", "aliases": ["upstairs"], "level": 2},
          {"floor_id": "outside", "name": "outside", "aliases": [], "level": 1}]
DEVICES = [
    {"id": "dev-kitchen", "area_id": "kitchen", "connections": [["mac", "02:00:00:00:00:0a"]]},
    {"id": "dev-kitchen-dupe", "area_id": None, "connections": [["mac", "02:00:00:00:00:0a"]]},
    {"id": "dev-workshop", "area_id": "workshop", "connections": [["mac", "02:00:00:00:00:0b"]]},
]
ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.0.2.10    0x1         0x2         02:00:00:00:00:0A     *        enp2s0
192.0.2.20     0x1         0x2         02:00:00:00:00:0b     *        enp2s0
192.0.2.99     0x1         0x0         00:00:00:00:00:00     *        enp2s0
"""


def arp_file(test):
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        f.write(ARP)
    test.addCleanup(os.unlink, path)
    return path


async def directory(test, registry=(), exposed=None, sent=None, auto_expose=True):
    sent = sent if sent is not None else []

    async def command(payload):
        sent.append(payload)
        kind = payload["type"]
        if kind == "config/area_registry/list":
            return {"success": True, "result": AREAS}
        if kind == "config/floor_registry/list":
            return {"success": True, "result": FLOORS}
        if kind == "config/device_registry/list":
            return {"success": True, "result": DEVICES}
        if kind == "config/entity_registry/list_for_display":
            return {"success": True, "result": {"entity_categories": {"0": "config"}, "entities": [
                dict({"ei": e["entity_id"]}, **({"hb": True} if e.get("hidden_by") else {}),
                     **({"ec": 0} if e.get("entity_category") else {}))
                for e in registry if not e.get("disabled_by")]}}
        if kind == "lovelace/dashboards/list":
            return {"success": True, "result": [{"url_path": "hvac-control", "mode": "storage"}]}
        if kind == "lovelace/config":
            if payload.get("url_path") == "hvac-control":
                return {"success": True, "result": {"views": [{"cards": [
                    {"type": "tile", "entity": "input_boolean.boost_heat"}]}]}}
            return {"success": False, "error": {"code": "config_not_found"}}
        if kind == "homeassistant/expose_entity/list":
            return {"success": True, "result": {"exposed_entities": exposed or {}}}
        return {"success": True, "result": None}

    async def fetch():
        return PROMPT

    d = HouseholdDirectory(command, fetch, arp_path=arp_file(test), auto_expose=auto_expose)
    await d.refresh()
    return d


class TheOverviewComesFromHomeAssistant(unittest.TestCase):
    def test_entries_are_parsed_and_only_controllable_domains_are_listed(self):
        entries = parse_overview(PROMPT)
        self.assertEqual([e["names"] for e in entries],
                         ["boost heat", "Kitchen Main", "workshop light", "outside temperature"])
        block = overview_block(entries, AREAS, FLOORS)
        self.assertIn("- boost heat (input_boolean)", block)
        self.assertIn("- Kitchen Main (light, Kitchen)", block)
        self.assertNotIn("outside temperature", block)

    def test_floors_list_their_aliases_and_rooms(self):
        block = overview_block([], AREAS, FLOORS)
        self.assertIn("- ground floor (also: downstairs): Kitchen", block)
        self.assertIn("- second floor (also: upstairs): loft (also: attic)", block)

    def test_the_block_is_deterministic_for_the_cache(self):
        entries = parse_overview(PROMPT)
        self.assertEqual(overview_block(entries, AREAS, FLOORS),
                         overview_block(list(reversed(entries)), list(reversed(AREAS)), FLOORS))


class NewEntitiesAreExposed(unittest.IsolatedAsyncioTestCase):
    REGISTRY = [
        {"entity_id": "input_boolean.boost_heat"},
        {"entity_id": "light.new_lamp"},
        {"entity_id": "light.kitchen_main"},
        {"entity_id": "lock.front_door"},
        {"entity_id": "sensor.outside_temperature"},
        {"entity_id": "switch.hidden_relay", "hidden_by": "user"},
        {"entity_id": "switch.disabled", "disabled_by": "integration"},
        {"entity_id": "switch.identify", "entity_category": "config"},
        {"entity_id": "script.unexposed_on_purpose"},
        {"entity_id": "input_boolean.lock_proxy"},
        {"entity_id": "input_boolean.someone_is_home", "device_id": None},
    ]
    EXPOSED = {"light.kitchen_main": {"conversation": True},
               "script.unexposed_on_purpose": {"conversation": False}}

    def test_only_nameable_unexposed_controllable_entities_are_chosen(self):
        self.assertEqual(entities_to_expose(self.REGISTRY, self.EXPOSED, {"input_boolean.boost_heat"}),
                         ["input_boolean.boost_heat", "light.new_lamp"])

    def test_a_helper_off_every_dashboard_is_plumbing_and_stays_hidden(self):
        chosen = entities_to_expose(self.REGISTRY, self.EXPOSED, set())
        self.assertNotIn("input_boolean.lock_proxy", chosen, "a helper behind another automation is not a control")
        self.assertNotIn("input_boolean.boost_heat", chosen)
        self.assertEqual(dashboard_entities([{"cards": [{"entity": "input_boolean.boost_heat"},
                                                        {"tap_action": {"service": "script.morning"}}]}]),
                         {"input_boolean.boost_heat", "script.morning"})

    async def test_a_refresh_exposes_them_to_assist(self):
        sent = []
        await directory(self, self.REGISTRY, self.EXPOSED, sent)
        expose = [p for p in sent if p["type"] == "homeassistant/expose_entity"]
        self.assertEqual(expose, [{"type": "homeassistant/expose_entity", "assistants": ["conversation"],
                                   "entity_ids": ["input_boolean.boost_heat", "light.new_lamp"],
                                   "should_expose": True}])


class ExposureIsOptIn(unittest.IsolatedAsyncioTestCase):
    async def test_by_default_nothing_is_exposed(self):
        sent = []
        d = HouseholdDirectory(None, None)
        self.assertFalse(d.auto_expose)
        await directory(self, NewEntitiesAreExposed.REGISTRY, {}, sent, auto_expose=False)
        self.assertEqual([p for p in sent if p["type"].startswith("homeassistant/expose_entity")], [])


class TheSatellitesRoom(unittest.IsolatedAsyncioTestCase):
    def test_the_mac_comes_from_the_arp_table(self):
        path = arp_file(self)
        self.assertEqual(satellite_mac("192.0.2.10", path), "02:00:00:00:00:0a")
        self.assertIsNone(satellite_mac("192.0.2.99", path), "an incomplete entry is no MAC")
        self.assertIsNone(satellite_mac("192.0.2.77", path))

    async def test_each_satellite_maps_to_its_own_room_and_device(self):
        d = await directory(self)
        self.assertEqual(d.room_of("192.0.2.10").name, "Kitchen")
        self.assertEqual(d.room_of("192.0.2.20").name, "Workshop")
        self.assertEqual(d.device_id_of("192.0.2.10"), "dev-kitchen", "the device with an area wins")
        self.assertIsNone(d.room_of("192.0.2.77"))

    async def test_a_spoken_room_or_alias_is_found(self):
        d = await directory(self)
        self.assertEqual(d.spoken_room("turn on the attic lights").name, "loft")
        self.assertIsNone(d.spoken_room("turn on the lights"))
        self.assertTrue(mentions("Turn on the Kitchen lights.", "kitchen"))
        self.assertFalse(mentions("kitchenette", "kitchen"))


class ScriptedRecognizer:
    available = True

    def __init__(self, *matches, heard=None):
        self.matches = list(matches)
        self.sentences = []
        self.device_ids = []
        self.last_heard = heard

    async def recognize(self, text, device_id=None):
        self.sentences.append(text)
        self.device_ids.append(device_id)
        return self.matches.pop(0) if self.matches else None


class RoomContextIsTheAskingSatellites(unittest.IsolatedAsyncioTestCase):
    async def route(self, recognizer, text, ip, *responses):
        d = await directory(self)
        backend = FakeBackend(*responses)
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post,
                                          static_context=d.block),
                            recognizer=recognizer, device_id=ip, household=d, satellite_ip=ip)
        executed = []

        async def execute(name, args):
            executed.append((name, args))
            raw = {"success": True}
            return raw, classify_result(raw)
        return await router.route(text, execute), executed, backend

    async def test_ha_naming_another_room_is_corrected_to_the_satellites_room(self):
        rec = ScriptedRecognizer(LocalMatch("HassTurnOn", {"area": "Kitchen", "domain": "light"}, ["light"], 1),
                                 LocalMatch("HassTurnOn", {"area": "Workshop", "domain": "light"}, ["light"], 1))
        result, executed, _ = await self.route(rec, "Turn on the lights", "192.0.2.20")
        self.assertEqual(rec.sentences[1], "Turn on the lights in the Workshop")
        self.assertEqual(rec.device_ids[0], "dev-workshop", "HA gets its own device id, not the address")
        self.assertEqual((result.kind, executed), (ACTION, [("intent__HassTurnOn",
                                                            {"area": "Workshop", "domain": "light"})]))

    async def test_the_satellites_own_room_acts_at_once(self):
        rec = ScriptedRecognizer(LocalMatch("HassTurnOn", {"area": "Kitchen", "domain": "light"}, ["light"], 1))
        result, executed, _ = await self.route(rec, "Turn on the lights", "192.0.2.10")
        self.assertEqual((result.kind, len(rec.sentences)), (ACTION, 1))

    async def test_a_spoken_room_is_kept_from_any_satellite(self):
        rec = ScriptedRecognizer(LocalMatch("HassTurnOn", {"area": "Kitchen", "domain": "light"}, ["light"], 1))
        result, executed, _ = await self.route(rec, "Turn on the kitchen lights", "192.0.2.20")
        self.assertEqual(executed, [("intent__HassTurnOn", {"area": "Kitchen", "domain": "light"})])

    async def test_an_unknown_satellite_never_acts_on_a_guessed_room(self):
        rec = ScriptedRecognizer(LocalMatch("HassTurnOn", {"area": "Kitchen", "domain": "light"}, ["light"], 1))
        result, executed, backend = await self.route(rec, "Turn on the lights", "192.0.2.77",
                                                     response(message("Which room?")))
        self.assertEqual(executed, [])
        self.assertEqual(len(backend.bodies), 1, "the backend decides")

    async def test_the_backend_gets_the_device_list_and_the_room(self):
        rec = ScriptedRecognizer(None)
        _, _, backend = await self.route(rec, "Turn on boost heat", "192.0.2.10",
                                         response(message("Done.")))
        body = backend.bodies[0]
        self.assertIn("- boost heat (input_boolean)", body["instructions"])
        notes = [i["content"] for i in body["input"] if i.get("role") == "developer"]
        self.assertIn("voice satellite in the Kitchen", notes[0])


class ANamedDeviceIdStillFindsItsRoom(unittest.IsolatedAsyncioTestCase):
    async def test_the_room_comes_from_the_address_not_the_configured_id(self):
        d = await directory(self)
        backend = FakeBackend()
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post),
                            device_id="kitchen-voice", household=d, satellite_ip="192.0.2.10")
        self.assertEqual(router.room().name, "Kitchen")
        self.assertEqual(router._ha_device_id(), "dev-kitchen")


class ParsedCommandsAreNeverIgnored(unittest.IsolatedAsyncioTestCase):
    async def route(self, heard, answer):
        backend = FakeBackend(response(message(answer)))
        rec = ScriptedRecognizer(None, heard=heard)
        router = TurnRouter(BackendRouter("k", model="m", instructions="i", tools=TOOLS, post=backend.post),
                            recognizer=rec)

        async def execute(name, args):  # pragma: no cover - nothing runs
            raise AssertionError(name)
        return await router.route("Turn on the kitchen lights", execute), backend

    async def test_no_request_for_a_parsed_command_tells_the_user(self):
        result, backend = await self.route(
            {"intent": "HassTurnOn", "slots": {"name": "kitchen lights"}, "targets": 0, "unmatched": ["name"]},
            "NO_REQUEST")
        self.assertEqual((result.kind, result.text), (FAILED, UNCLEAR_TARGET_TEXT))
        note = [i["content"] for i in backend.bodies[0]["input"] if i.get("role") == "developer"][0]
        self.assertIn("parser guessed HassTurnOn (name='kitchen lights'); unmatched: name", note)
        self.assertIn("A hint only", note)
        self.assertNotIn("This is a request", note)

    async def test_no_request_without_a_parse_is_still_silence(self):
        result, _ = await self.route(None, "NO_REQUEST")
        self.assertEqual(result.kind, NOTHING)


if __name__ == "__main__":
    unittest.main()
