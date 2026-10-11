"""Kontoret 2026-10-11: 'Stäng av radion' -> HassTurnOff on the speaker, HA answered failed, the radio played on and
Björn said it was done. A speaker is paused, and a failed list is never reported as done."""
import json
from types import SimpleNamespace

import pytest

from app.providers import GEMINI, ProviderOptions, build_service
from app.providers.tool_registration import ha_misslyckades, ha_utfall, media_paus

FEL = {"response_type": "action_done",
       "data": {"targets": [], "success": [], "failed": [{"name": "Kontor", "id": "media_player.home_assistant_voice_0a6bc9"}]}}


def test_turn_off_on_a_speaker_becomes_pause():
    assert media_paus("intent__HassTurnOff", {"domain": ["media_player"], "area": "kontor"}) == \
        ("HassMediaPause", {"area": "kontor"})
    assert media_paus("intent__HassTurnOff", {"domain": "media_player", "name": "Kontor"}) == \
        ("HassMediaPause", {"name": "Kontor"})


def test_a_mixed_list_is_not_turned_into_a_pause():
    # B's FAIL on #62: 'stäng av lamporna och radion' must still turn the lights off
    assert media_paus("intent__HassTurnOff", {"domain": ["light", "media_player"], "area": "Kontor"}) is None
    assert media_paus("HassTurnOff", '{"domain": ["light", "media_player"], "area": "Kontor"}') is None
    assert media_paus("HassTurnOff", {"domain": ["media_player", "media_player"], "area": "Kontor"}) == \
        ("HassMediaPause", {"area": "Kontor"})


def test_other_turn_offs_are_left_alone():
    assert media_paus("intent__HassTurnOff", {"domain": ["light"], "area": "kontor"}) is None
    assert media_paus("intent__HassTurnOff", {"area": "kontor"}) is None
    assert media_paus("intent__HassTurnOn", {"domain": ["media_player"]}) is None


@pytest.mark.parametrize("form", [FEL, json.dumps(FEL), {"result": json.dumps(FEL)}, [{"text": json.dumps(FEL)}]])
def test_failed_is_found_in_every_shape(form):
    assert ha_utfall(form) == ([], ["Kontor"])
    msg = ha_misslyckades(form)
    assert msg["failed"] == ["Kontor"] and "INTE" in msg["error"]


def test_empty_failed_or_unknown_shape_is_untouched():
    assert ha_misslyckades({"data": {"success": [{"name": "Tak"}], "failed": []}}) is None
    assert ha_misslyckades("Klart.") is None
    assert ha_misslyckades({"result": "ingen sensor"}) is None


def test_partial_success_says_both():
    msg = ha_misslyckades({"data": {"success": [{"name": "Tak"}], "failed": [{"name": "Ringen"}]}})
    assert msg["error"].startswith("Delvis: klart för Tak. INTE klart för Ringen")


def _service():
    opts = ProviderOptions(api_key="AIza-test", model="models/gemini-3.8-live", voice="Charon",
                           instructions="x", language="sv-SE")
    return build_service(GEMINI, opts, [])


async def _call(service, name, arguments):
    got = []

    async def cb(result, *a, **k):
        got.append(result)
    await service._functions[name].handler(SimpleNamespace(arguments=arguments, result_callback=cb, function_name=name))
    return got[0]


@pytest.mark.asyncio
async def test_stang_av_radion_runs_pause_and_a_failed_answer_is_reported():
    service = _service()
    ran = []

    async def turn_off(params):
        ran.append(("off", params.function_name))
        await params.result_callback("{}")

    async def pause(params):
        ran.append(("pause", params.function_name, dict(params.arguments)))
        await params.result_callback(json.dumps(FEL))
    service.register_function("intent__HassTurnOff", turn_off)
    service.register_function("media_player__HassMediaPause", pause)
    got = await _call(service, "intent__HassTurnOff", {"domain": ["media_player"], "area": "kontor"})
    assert ran == [("pause", "media_player__HassMediaPause", {"area": "kontor"})]  # TurnOff never ran
    assert "INTE" in got["error"] and got["failed"] == ["Kontor"]  # a failed pause is said, not acknowledged


def test_arguments_as_a_json_string_or_junk_do_not_raise():
    assert media_paus("HassTurnOff", '{"domain": ["media_player"], "area": "kontor"}') == ("HassMediaPause", {"area": "kontor"})
    assert media_paus("HassTurnOff", "inte json") is None
    assert media_paus("HassTurnOff", None) is None
