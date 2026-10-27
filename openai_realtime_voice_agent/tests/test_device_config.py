"""Per-device settings for several Voice PEs on one add-on instance."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.device_config import DeviceDirectory
from app.device_registry import device_id_from_websocket
from app.live_cost import LiveSpendLedger
from app.websocket_handler import WebSocketHandler

DEVICES = [
    {"name": "hallway", "port": 8082, "announce_port": 8091, "follow_up_listen_seconds": 4,
     "timer_ring_entity": "switch.hallway_timer_ringing", "live_max_usd_per_day": 1,
     "live_max_session_minutes_per_day": 20, "voice": "cedar"},
    {"name": "study", "port": 8084, "playback_prebuffer_ms": 250, "room": "office"},
]


def directory(entries=DEVICES):
    return DeviceDirectory.from_json(json.dumps(entries))


class FakeWS:
    def __init__(self, port, query="", host="192.0.2.9"):
        self.url = SimpleNamespace(query=query)
        self.client = SimpleNamespace(host=host)
        self.scope = {"server": ("0.0.0.0", port)}


class TestDirectory(unittest.TestCase):
    def test_empty_option_is_no_devices(self):
        for text in ("", "[]", "not json", '{"name": "x"}'):
            self.assertFalse(DeviceDirectory.from_json(text))

    def test_entries_parse_with_overrides(self):
        d = directory()
        self.assertEqual([p.name for p in d.profiles], ["hallway", "study"])
        self.assertEqual(d.get("hallway").follow_up_listen_seconds, 4)
        self.assertIsNone(d.get("study").follow_up_listen_seconds)
        self.assertTrue(d.get("hallway").has_own_caps)
        self.assertFalse(d.get("study").has_own_caps)

    def test_bad_entries_are_dropped_not_fatal(self):
        d = directory([{"port": 8082}, {"name": "a", "port": 80}, {"name": "a", "port": 8090},
                       {"name": "b", "port": "x"}])
        self.assertEqual([p.name for p in d.profiles], ["a", "b"])
        self.assertIsNone(d.get("a").port, "a port below 1024 is ignored")
        self.assertIsNone(d.get("b").port)

    def test_a_port_is_matched_to_one_device_only(self):
        d = directory([{"name": "a", "port": 8082}, {"name": "b", "port": 8082}])
        self.assertEqual(d.by_port(8082).name, "a")
        self.assertIsNone(d.get("b").port)

    def test_ports_and_timer_mappings(self):
        d = directory()
        self.assertEqual(d.extra_ports(8080), [8082, 8084])
        self.assertEqual(d.extra_ports(8082), [8084])
        self.assertEqual(d.announce_ports(), [8091])
        self.assertEqual(d.by_announce_port(8091).name, "hallway")
        self.assertEqual(d.timer_ring_mappings(), "hallway=switch.hallway_timer_ringing")


class TestIdentityByPort(unittest.TestCase):
    PORTS = {8082: "hallway", 8084: "study"}

    def test_the_port_names_the_device(self):
        self.assertEqual(device_id_from_websocket(FakeWS(8084), self.PORTS), "study")

    def test_an_explicit_device_id_wins(self):
        self.assertEqual(device_id_from_websocket(FakeWS(8084, "device_id=hallway"), self.PORTS), "hallway")

    def test_an_unconfigured_port_falls_back_to_the_ip(self):
        self.assertEqual(device_id_from_websocket(FakeWS(8080), self.PORTS), "192.0.2.9")
        self.assertEqual(device_id_from_websocket(FakeWS(8080)), "192.0.2.9")


class TestPerDeviceHandler(unittest.TestCase):
    def setUp(self):
        self.handler = WebSocketHandler(follow_up_ms=8000, playback_prebuffer_ms=100,
                                        wake_open_delay_ms=700)
        self.handler.device_directory = directory()

    def test_hello_uses_the_device_overrides(self):
        hallway = self.handler.hello_payload("hallway")
        self.assertEqual(hallway["follow_up_ms"], 4000)
        self.assertEqual(hallway["playback_prebuffer_ms"], 100)
        study = self.handler.hello_payload("study")
        self.assertEqual(study["follow_up_ms"], 8000)
        self.assertEqual(study["playback_prebuffer_ms"], 250)
        self.assertEqual(self.handler.hello_payload("192.0.2.9"), self.handler.hello_payload())

    def test_a_device_with_its_own_caps_has_its_own_ledger(self):
        instance = LiveSpendLedger(max_session_minutes_per_day=100, max_usd_per_day=5, path=None)
        self.handler.live_spend_ledger = instance
        made = []

        def factory(profile):
            made.append(profile.name)
            return LiveSpendLedger(max_session_minutes_per_day=profile.live_max_session_minutes_per_day,
                                   max_usd_per_day=profile.live_max_usd_per_day, path=None)

        self.handler.device_ledger_factory = factory
        own = self.handler.ledger_for("hallway")
        self.assertIsNot(own, instance)
        self.assertEqual((own.max_session_minutes_per_day, own.max_usd_per_day), (20, 1))
        self.assertIs(self.handler.ledger_for("hallway"), own, "one ledger per device")
        self.assertIs(self.handler.ledger_for("study"), instance)
        self.assertIs(self.handler.ledger_for("192.0.2.9"), instance)
        self.assertEqual(made, ["hallway"])

    def test_without_devices_everything_is_the_instance(self):
        handler = WebSocketHandler(follow_up_ms=8000)
        handler.live_spend_ledger = object()
        self.assertEqual(handler.follow_up_ms_for("anything"), 8000)
        self.assertIs(handler.ledger_for("anything"), handler.live_spend_ledger)


class TestApplicationPerDevice(unittest.TestCase):
    def setUp(self):
        from app.main import Application
        self.app = Application.__new__(Application)
        self.app.device_directory = directory()

    def test_room_comes_from_the_device(self):
        self.assertEqual(self.app._room_for("study"), "office")
        self.assertEqual(self.app._room_for("hallway"), "hallway")
        with patch.dict(os.environ, {"INSTANCE_NAME": "Kitchen"}):
            self.assertEqual(self.app._room_for("192.0.2.9"), "kitchen")

    def test_device_ledger_takes_its_caps_and_its_own_file(self):
        self.app.live_config = SimpleNamespace(max_session_minutes_per_day=100, max_usd_per_day=5)
        with patch("app.live_cost.LEDGER_PATH", None):
            ledger = self.app._device_ledger(self.app.device_directory.get("hallway"))
        self.assertEqual((ledger.max_session_minutes_per_day, ledger.max_usd_per_day), (20, 1))
        from app.device_config import DeviceProfile
        with patch("app.live_cost.LEDGER_PATH", None):
            half = self.app._device_ledger(DeviceProfile(name="x", live_max_usd_per_day=2))
        self.assertEqual((half.max_session_minutes_per_day, half.max_usd_per_day), (100, 2))

    def test_device_voice_replaces_the_live_voice(self):
        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class Cfg:
            voice: str = "vesper"

        self.app.live_config = Cfg()
        self.assertEqual(self.app._live_config_for(SimpleNamespace(device_id="hallway")).voice, "cedar")
        self.assertEqual(self.app._live_config_for(SimpleNamespace(device_id="study")).voice, "vesper")


class TestPortChecks(unittest.TestCase):
    def test_colliding_ports_are_dropped_not_fatal(self):
        d = directory([
            {"name": "main", "port": 8080},                        # the instance's own port
            {"name": "a", "port": 8082, "announce_port": 8090},    # 8090 is the instance announce port
            {"name": "b", "port": 8091, "announce_port": 8082},    # its announce port is a's port
            {"name": "c", "port": 8084, "announce_port": 8092},
            {"name": "d", "port": 8086, "announce_port": 8092},    # c already has 8092
        ]).checked(8080, 8090)
        self.assertIsNone(d.get("main").port, "a device must not take the instance's websocket_port")
        self.assertEqual((d.get("a").port, d.get("a").announce_port), (8082, None))
        self.assertEqual((d.get("b").port, d.get("b").announce_port), (8091, None))
        self.assertEqual(d.get("c").announce_port, 8092)
        self.assertIsNone(d.get("d").announce_port)
        self.assertEqual(d.extra_ports(8080), [8082, 8084, 8086, 8091])

    def test_the_main_port_never_names_unknown_devices(self):
        d = directory([{"name": "kitchen", "port": 8080}]).checked(8080, None)
        ports = {p.port: p.name for p in d.profiles if p.port is not None}
        self.assertEqual(device_id_from_websocket(FakeWS(8080), ports), "192.0.2.9")


class TestBusyPort(unittest.IsolatedAsyncioTestCase):
    async def test_a_busy_device_port_does_not_stop_the_others(self):
        from app.main import Application
        app = Application.__new__(Application)
        app.device_directory = directory()
        served = []

        class Server:
            def __init__(self, port, fail):
                self.port, self.fail = port, fail

            async def serve(self):
                if self.fail:
                    # What uvicorn raises when it cannot bind; an OSError for the
                    # instance port (a SystemExit would stop the test loop).
                    raise SystemExit(1) if self.port != 8080 else OSError(48, "in use")
                served.append(self.port)

        await app._serve_ports([(8080, Server(8080, False)), (8082, Server(8082, True)),
                                (8084, Server(8084, False))], 8080)
        self.assertEqual(sorted(served), [8080, 8084])
        with self.assertRaises(OSError):
            await app._serve_ports([(8080, Server(8080, True))], 8080)


class TestCaseInsensitiveNames(unittest.TestCase):
    def test_names_match_in_any_case(self):
        d = directory([{"name": "Hallway", "follow_up_listen_seconds": 4}])
        self.assertEqual(d.get("hallway").name, "hallway")
        self.assertEqual(d.get("HALLWAY").follow_up_listen_seconds, 4)
        handler = WebSocketHandler(follow_up_ms=8000)
        handler.device_directory = d
        self.assertEqual(handler.follow_up_ms_for("Hallway"), 4000)


class TestBothCapsDisabled(unittest.TestCase):
    def test_a_device_with_both_caps_off_is_flagged(self):
        from app.device_config import DeviceProfile
        from app.main import Application
        app = Application.__new__(Application)
        app.live_config = SimpleNamespace(max_session_minutes_per_day=0, max_usd_per_day=0)
        with patch("app.live_cost.LEDGER_PATH", None), self.assertLogs("app.main", "ERROR") as logs:
            app._device_ledger(DeviceProfile(name="x", live_max_usd_per_day=0))
        self.assertTrue(any("BOTH GPT-Live spend caps are disabled" in line for line in logs.output))


class TestSettingsResolvedAtAccept(unittest.IsolatedAsyncioTestCase):
    async def test_prompts_use_the_connected_devices_lead_buffer(self):
        from app.device_registry import DeviceConnection
        from app.main import Application
        handler = WebSocketHandler(output_lead_buffer_ms=300)
        handler.device_directory = directory([{"name": "hallway", "output_lead_buffer_ms": 600}])
        connection = DeviceConnection(device_id="hallway", websocket=None,
                                      profile=handler.device_directory.get("hallway"))
        await handler.devices.add(connection)
        self.assertEqual(handler.output_lead_ms_for("hallway"), 600)
        self.assertEqual(handler.output_lead_ms_for("other"), 300)
        app = Application.__new__(Application)
        app.websocket_handler = handler
        plans = []
        handler.send_bytes_to = lambda data, device_id: _true()
        handler.serializer_for = lambda device_id: None
        with patch("app.output_lead_buffer.prompt_send_plan",
                   side_effect=lambda pcm, lead: plans.append(lead) or []):
            await app._play_prompt(b"\0" * 4800, "hallway")
        self.assertEqual(plans, [600])


async def _true():
    return True


if __name__ == "__main__":
    unittest.main()
