"""Every Home Assistant call goes to raawr-comms, with comms' key (raawr US-011).

The add-on holds no HA key: HA has no scoped tokens, so any key it held could
unlock the front door. These tests pin that down two ways:

  * statically: nothing in the add-on reads SUPERVISOR_TOKEN or
    LONGLIVED_TOKEN, and config.yaml no longer asks HA for a token;
  * by running each HA call site with poisoned SUPERVISOR_TOKEN /
    LONGLIVED_TOKEN in the environment and a fake HA transport, and checking
    that every request went to HA_API_URL with X-Raawr-Nyckel and nothing
    else that could authenticate to HA.
"""
import pathlib
import re
from types import SimpleNamespace

import httpx
import pytest
import yaml

from app import enrollment, ha_sensors, mcp_service, play_media_tool, search_home_tool, timers

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMS = "http://comms.test:3500/kanal/rost/kontoret/api"
NYCKEL = "kontorets-comms-nyckel"
GIFT = "gammal-ha-token-som-inte-far-anvandas"
# test_timer_targeting.py replaces timers._set_ring for good; keep the real one.
SET_RING = timers._set_ring


def test_no_module_reads_a_home_assistant_token():
    token = r"SUPERVISOR_TOKEN|LONGLIVED_TOKEN|longlived_token"
    readers = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in (ROOT / "app").rglob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(rf"(environ|getenv).*({token})", line)
    ] + [
        f"root/run.sh:{n}"
        for n, line in enumerate((ROOT / "root" / "run.sh").read_text().splitlines(), 1)
        if re.search(token, line) and not line.lstrip().startswith("#")
    ]
    assert readers == []


def test_the_add_on_asks_home_assistant_for_no_key():
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert config["homeassistant_api"] is False
    assert "longlived_token" not in config["options"]
    assert "longlived_token" not in config["schema"]
    assert config["schema"]["comms_nyckel"] == "password"


@pytest.fixture
def ha(monkeypatch):
    """A fake HA behind comms; records every request the add-on makes."""
    monkeypatch.setenv("HA_API_URL", COMMS + "/")
    monkeypatch.setenv("COMMS_NYCKEL", NYCKEL)
    monkeypatch.setenv("SUPERVISOR_TOKEN", GIFT)
    monkeypatch.setenv("LONGLIVED_TOKEN", GIFT)
    monkeypatch.setenv("INSTANCE_NAME", "kontor")
    monkeypatch.setenv("TIMER_RING_ENTITY", "switch.kontor_timer_ringing")
    monkeypatch.setenv("WAKE_SOUND_ENTITY", "switch.kontor_wake_sound")
    monkeypatch.setattr(play_media_tool, "_config_entry_id", None, raising=False)

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/api/states"):
            return httpx.Response(200, json=[
                {"entity_id": "sensor.rocket_battery", "state": "33",
                 "attributes": {"friendly_name": "Rocket battery level"}},
                {"entity_id": "media_player.kontor", "state": "idle",
                 "attributes": {"friendly_name": "kontor", "mass_player_type": "player"}},
            ])
        if path.endswith("/api/config/config_entries/entry"):
            return httpx.Response(200, json=[{"entry_id": "ma1"}])
        if path.endswith("/api/services/music_assistant/search"):
            return httpx.Response(200, json={"service_response": {
                "radio": [{"name": "Sveriges Radio P3", "uri": "library://radio/4", "favorite": True}]}})
        return httpx.Response(200, json=[])

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


def _params(**arguments):
    said = []

    async def result_callback(text):
        said.append(text)

    return SimpleNamespace(arguments=arguments, result_callback=result_callback), said


def _assert_all_through_comms(seen):
    assert seen, "no HA call was made at all"
    for request in seen:
        assert str(request.url).startswith(COMMS + "/"), request.url
        assert request.headers.get("x-raawr-nyckel") == NYCKEL
        assert "authorization" not in request.headers
        assert GIFT not in str(request.headers)


@pytest.mark.asyncio
async def test_every_ha_call_site_goes_to_comms_with_comms_key(ha):
    params, said = _params(query="rocket battery")
    await search_home_tool.create_search_home_tool_handler()(params)
    assert "33" in said[0]

    params, said = _params(query="P3", media_type="radio")
    await play_media_tool.create_play_media_tool_handler()(params)
    assert said[0].startswith("Playing Sveriges Radio P3")

    assert await ha_sensors._post("sensor.voicepe_kontor_speaker", "none", {}) is True
    assert await SET_RING(True) is True
    await enrollment._set_wake_sound(False)

    _assert_all_through_comms(ha)
    calls = {(r.method, r.url.path, r.url.query.decode()) for r in ha}
    assert calls == {
        ("GET", "/kanal/rost/kontoret/api/states", ""),
        ("GET", "/kanal/rost/kontoret/api/config/config_entries/entry", "domain=music_assistant"),
        ("POST", "/kanal/rost/kontoret/api/services/music_assistant/search", "return_response=true"),
        ("POST", "/kanal/rost/kontoret/api/services/music_assistant/play_media", ""),
        ("POST", "/kanal/rost/kontoret/api/states/sensor.voicepe_kontor_speaker", ""),
        ("POST", "/kanal/rost/kontoret/api/services/switch/turn_on", ""),
        ("POST", "/kanal/rost/kontoret/api/services/switch/turn_off", ""),
    }


@pytest.mark.asyncio
async def test_mcp_goes_to_comms_with_comms_key(ha, monkeypatch):
    captured = {}

    def params(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(mcp_service, "StreamableHttpParameters", params)
    monkeypatch.setattr(mcp_service, "MCPClient", lambda server_params: server_params)
    await mcp_service.HomeAssistantMCPService().initialize()

    assert captured["url"] == COMMS + "/mcp"
    assert captured["headers"] == {"X-Raawr-Nyckel": NYCKEL}


@pytest.mark.asyncio
async def test_without_comms_configured_nothing_is_sent(ha, monkeypatch):
    monkeypatch.delenv("HA_API_URL")
    params, said = _params(query="rocket battery")
    await search_home_tool.create_search_home_tool_handler()(params)
    assert await ha_sensors._post("sensor.voicepe_kontor_speaker", "none", {}) is False
    assert await SET_RING(True) is False

    assert said == ["I cannot reach the house right now."]
    assert ha == []
