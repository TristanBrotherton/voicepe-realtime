"""Loop guard (0.25.1): an identical call (same tool, same arguments, 0.25.6)
runs at most 3 times per user turn, any tools 12 times in total, on every
engine. Probe 2026-10-02: Grok called GetLiveContext up to 48 times in one
turn when the answer was not there."""
import logging
from types import SimpleNamespace

import pytest

from app.phase_emitter import TurnLiveness
from app.providers import GEMINI, OPENAI, XAI, ProviderOptions, build_service


def _service(provider):
    if provider == GEMINI:
        opts = ProviderOptions(api_key="AIza-test", model="models/gemini-2.5-flash-native-audio-latest",
                               voice="Charon", instructions="x", language="sv-SE")
    else:
        opts = ProviderOptions(api_key="k", model="m", voice="rex", instructions="x",
                               turn_detection_type="server_vad")
    service = build_service(provider, opts, [])
    service.turn_liveness = TurnLiveness()
    return service


def _register(service, name, ran):
    async def handler(params):
        ran.append(name)
        await params.result_callback({"result": "ingen sensor"})
    service.register_function(name, handler)


async def _call(service, name, arguments=None):
    got = []

    async def cb(result, *a, **k):
        got.append(result)
    await service._functions[name].handler(
        SimpleNamespace(arguments=arguments or {}, result_callback=cb, function_name=name))
    return got[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [OPENAI, GEMINI, XAI])
async def test_the_same_tool_stops_after_three_runs_per_turn(provider, caplog):
    caplog.set_level(logging.INFO)
    service = _service(provider)
    ran = []
    _register(service, "homeassistant__GetLiveContext", ran)
    results = [await _call(service, "homeassistant__GetLiveContext") for _ in range(5)]
    assert len(ran) == 3  # HA is not called a fourth time
    assert results[3]["result"].startswith(
        "Stopp: du har redan anropat homeassistant__GetLiveContext med samma argument 3 gånger")
    assert "⏱ tool-loop stopp homeassistant__GetLiveContext 4" in caplog.text
    service.turn_liveness.user_started()  # a new turn starts from zero
    await _call(service, "homeassistant__GetLiveContext")
    assert len(ran) == 4


@pytest.mark.asyncio
async def test_any_tools_stop_after_twelve_runs_per_turn():
    service = _service(XAI)
    ran = []
    for i in range(7):
        _register(service, f"t{i}", ran)
    for i in range(7):
        await _call(service, f"t{i}")
        await _call(service, f"t{i}")
    assert len(ran) == 12
    assert "Stopp" in (await _call(service, "t0", {"x": 1}))["result"]


@pytest.mark.asyncio
async def test_different_arguments_all_run_identical_ones_stop_at_the_fourth():
    """Review of PR #3: four rooms in one sentence are four HassTurnOn, and the
    fourth was refused while the model could still say "klart"."""
    service = _service(XAI)
    ran = []
    _register(service, "HassTurnOn", ran)
    _register(service, "GetLiveContext", ran)
    for area in ("Kontoret", "Köket", "Hallen", "Sovrummet"):
        result = await _call(service, "HassTurnOn", {"area": area, "domain": ["light"]})
        assert "Stopp" not in str(result)
    assert ran.count("HassTurnOn") == 4
    args = ({"name": "temp"}, {"name": " Temp "}, '{"name": "temp"}', {"name": "temp"})
    results = [await _call(service, "GetLiveContext", a) for a in args]
    assert ran.count("GetLiveContext") == 3
    assert "Stopp" in results[3]["result"]
