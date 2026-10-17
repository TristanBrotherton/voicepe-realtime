"""Google refuses the connection because the account's money is used: the speaker moves to the next engine."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.providers import build_service
from test_gemini_provider import OPENAI_SHAPE, _options

TAK = "1011 None. Your project has exceeded its monthly spending cap. Please go to AI Studio"
# What an engine says when the money is used: Google's cap (live 2026-10-13), a plain 402, a prepaid account run dry.
UTAN_PENGAR = [TAK, "402 Payment Required", "429 Your prepayment credits are depleted. Please go to AI Studio to manage your project and billing."]


class Klocka:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("text", UTAN_PENGAR)
def test_what_an_engine_without_money_says_is_money(text):
    from app.provider_failures import Failure, classify
    assert classify(text) is Failure.MONEY


@pytest.mark.parametrize("text", UTAN_PENGAR)
@pytest.mark.asyncio
async def test_a_connect_refused_for_money_is_remembered_and_cleared_by_the_next_session(monkeypatch, text):
    service = build_service("gemini", _options(), OPENAI_SHAPE)

    async def nekad(self, config):
        raise RuntimeError(text)
    monkeypatch.setattr("pipecat.services.google.gemini_live.llm.GeminiLiveLLMService._connection_task_handler", nekad)
    with pytest.raises(RuntimeError):
        await service._connection_task_handler(None)
    assert service.vagran == "tak"

    async def ingen(self, session):
        return None
    monkeypatch.setattr("pipecat.services.google.gemini_live.llm.GeminiLiveLLMService._handle_session_ready", ingen)
    await service._handle_session_ready(object())
    assert service.vagran is None


@pytest.mark.asyncio
async def test_any_other_connect_error_is_not_money(monkeypatch):
    service = build_service("gemini", _options(), OPENAI_SHAPE)

    async def nere(self, config):
        raise RuntimeError("1006 connection closed abnormally")
    monkeypatch.setattr("pipecat.services.google.gemini_live.llm.GeminiLiveLLMService._connection_task_handler", nere)
    with pytest.raises(RuntimeError):
        await service._connection_task_handler(None)
    assert service.vagran is None


@pytest.mark.asyncio
async def test_the_wake_that_google_refuses_for_money_moves_the_speaker_to_the_backup():
    from app.provider_router import ProviderRouter
    from app.providers.sovlage import SovlageMixin
    from app.websocket_handler import ConnectionRecovery

    class Nekad(SovlageMixin):
        sover = True
        vagran = None

        async def vakna(self):
            self.vagran = "tak"
            return False

    router = ProviderRouter("gemini", "xai", probe=lambda e: True)
    flytt = AsyncMock()
    r = ConnectionRecovery(Nekad(), provider="gemini", router=router, on_failover=flytt)
    await r.vakna()
    await asyncio.sleep(0.05)
    flytt.assert_awaited_once()
    assert router.current() == "xai"


@pytest.mark.parametrize("text", UTAN_PENGAR)
@pytest.mark.asyncio
async def test_money_running_out_in_the_middle_of_a_session_moves_the_speaker_at_once(text):
    """Not on a wake: the session is up and the engine's error frame says the money is used. No strikes, no retry."""
    from app.provider_router import ProviderRouter
    from app.websocket_handler import ConnectionRecovery

    class Tjanst:
        _websocket = object()

        async def reset_conversation(self):
            raise AssertionError("an engine without money is not reconnected")

    router = ProviderRouter("gemini", "xai", probe=lambda e: True)
    flytt = AsyncMock()
    r = ConnectionRecovery(Tjanst(), provider="gemini", router=router, on_failover=flytt)
    assert await r.handle_error(text) is True
    await asyncio.sleep(0.05)
    flytt.assert_awaited_once()
    assert router.current() == "xai"


@pytest.mark.asyncio
async def test_the_probe_cannot_call_an_engine_without_money_healthy():
    """The probe is a GET on the model list: 200 for an account with no money. openai, then gemini, then xai:
    gemini said its money is used; when openai fails next, the probe's "ok" for gemini must not send the house there."""
    from app.provider_router import MONEY_HOLD_S, ProviderRouter

    klocka = Klocka()
    fragad = []
    router = ProviderRouter("openai", "gemini", extra=["xai"], clock=klocka, probe=lambda e: fragad.append(e) or True)
    assert await router.report_failure("openai", "insufficient_quota") == "gemini"
    router.note_success("gemini")                       # gemini worked for a while...
    klocka.t += 300
    assert await router.report_failure("gemini", TAK) == "xai"      # ...then its money ran out
    assert router.status()["out_of_money"] == ["gemini", "openai"]
    klocka.t += router.cooldown_s + 1
    assert router.current() == "openai"                 # cooldown over: the primary is tried again
    router.note_success("openai")                       # and it works (its money came back)
    fragad.clear()
    klocka.t += 60
    assert await router.report_failure("openai", "insufficient_quota") == "xai"   # not gemini: it has no money
    assert "gemini" not in fragad                       # and the probe was not even asked about it
    # A completed turn is the proof that the money is back; so is the hold running out.
    router2 = ProviderRouter("openai", "gemini", clock=klocka, probe=lambda e: True)
    router2._active = "gemini"
    await router2.report_failure("gemini", TAK)
    assert await router2._known_healthy("gemini") is False
    router2.note_success("gemini")
    assert await router2._known_healthy("gemini") is True
    router2._broke_at["gemini"] = klocka.t
    klocka.t += MONEY_HOLD_S + 1
    assert await router2._known_healthy("gemini") is True
