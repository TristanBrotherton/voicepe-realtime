"""Google refuses the connection because the account's money is used: the speaker moves to the next engine."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.providers import build_service
from test_gemini_provider import OPENAI_SHAPE, _options

TAK = "1011 None. Your project has exceeded its monthly spending cap. Please go to AI Studio"


@pytest.mark.asyncio
async def test_a_connect_refused_for_money_is_remembered_and_cleared_by_the_next_session(monkeypatch):
    service = build_service("gemini", _options(), OPENAI_SHAPE)

    async def nekad(self, config):
        raise RuntimeError(TAK)
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
