"""US-060 AC-17: the engine's own barge-in can be turned off per room (GEMINI_NO_INTERRUPTION); then only our guard cuts him."""
import pytest
from google.genai.types import ActivityHandling, LiveConnectConfig, RealtimeInputConfig, AutomaticActivityDetection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

from app import eko_vakt
from app.providers import build_service
from test_gemini_provider import OPENAI_SHAPE, _options


def test_the_switch_is_per_room_and_off_by_default(monkeypatch):
    monkeypatch.delenv("GEMINI_NO_INTERRUPTION", raising=False)
    assert not eko_vakt.ingen_avbrott("kontoret")
    monkeypatch.setenv("GEMINI_NO_INTERRUPTION", "kontoret")
    assert eko_vakt.ingen_avbrott("kontoret") and not eko_vakt.ingen_avbrott("koket")
    monkeypatch.setenv("GEMINI_NO_INTERRUPTION", "1")
    assert eko_vakt.ingen_avbrott("koket")


async def _config_efter(monkeypatch, rum, miljo, config):
    monkeypatch.setenv("GEMINI_NO_INTERRUPTION", miljo)
    o = _options(); o.rum = rum
    service = build_service("gemini", o, OPENAI_SHAPE)
    sett = []

    async def fake(self, c):
        sett.append(c)
    monkeypatch.setattr(GeminiLiveLLMService, "_connection_task_handler", fake)
    await service._connection_task_handler(config)
    return sett[0]


@pytest.mark.asyncio
async def test_the_connect_config_gets_no_interruption_in_that_room_only(monkeypatch):
    c = LiveConnectConfig(realtime_input_config=RealtimeInputConfig(automatic_activity_detection=AutomaticActivityDetection()))
    c1 = await _config_efter(monkeypatch, "kontoret", "kontoret", c)
    assert c1.realtime_input_config.activity_handling == ActivityHandling.NO_INTERRUPTION
    assert c1.realtime_input_config.automatic_activity_detection is not None       # the turn settings are kept
    c2 = await _config_efter(monkeypatch, "koket", "kontoret", LiveConnectConfig())
    assert c2.realtime_input_config is None                                        # another room: untouched


@pytest.mark.asyncio
async def test_a_config_with_no_input_settings_gets_one(monkeypatch):
    c = await _config_efter(monkeypatch, "kontoret", "1", LiveConnectConfig())
    assert c.realtime_input_config.activity_handling == ActivityHandling.NO_INTERRUPTION
