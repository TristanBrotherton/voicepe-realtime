"""Sleep mode (raawr INKAST 2026-10-04): connected to the cloud only during a conversation.

The cost: xAI bills per connected minute, and the agent reconnected a quiet
session every 900 s, all night. Each test here fails on 0.25.6.
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import ErrorFrame
from pipecat.processors.frame_processor import FrameDirection

from app.providers import GEMINI, OPENAI, XAI, ProviderOptions, build_service
from app.providers.sovlage import SovlageMixin
from app.websocket_handler import ConnectionRecovery


def _service(provider):
    opts = ProviderOptions(api_key="k", model="gpt-realtime-2", voice="cedar", instructions="Du är Björn.")
    if provider == GEMINI:
        opts = ProviderOptions(api_key="k", model="gemini-live-2.5-flash-preview", voice="Charon",
                               instructions="Du är Björn.")
    return build_service(provider, opts, [])


@pytest.mark.parametrize("provider", [OPENAI, XAI, GEMINI])
def test_varje_motor_borjar_sovande_och_kopplar_inte_upp(provider, monkeypatch):
    service = _service(provider)
    assert isinstance(service, SovlageMixin)
    assert service.sover is True
    connected = []
    base = type(service).__mro__[type(service).__mro__.index(SovlageMixin) + 1]
    monkeypatch.setattr(base, "_connect", lambda self, *a, **k: connected.append(1), raising=False)
    asyncio.run(service._connect())  # what pipecat's start() calls
    assert connected == []


def test_av_med_moln_sovlage_0(monkeypatch):
    monkeypatch.setenv("MOLN_SOVLAGE", "0")
    assert _service(XAI).sover is False


class Fake(SovlageMixin):
    def __init__(self):
        self.sover = True
        self.calls = []

    async def _ateranslut(self, forut):
        self.calls.append(("upp", forut))

    async def _disconnect(self):
        self.calls.append("ner")


@pytest.mark.asyncio
async def test_vakna_kopplar_upp_en_gang_och_sova_kopplar_ner():
    s = Fake()
    assert await s.vakna() is True
    assert await s.vakna() is False  # already awake: no second connect
    assert await s.sova("tyst") is True
    assert await s.sova("tyst") is False
    assert await s.vakna() is True
    # The second wake keeps the conversation (OpenAI: reset_conversation re-seeds it).
    assert s.calls == [("upp", False), "ner", ("upp", True)]


def _recovery(service, phase="idle"):
    class Phase:
        pass

    p = Phase()
    p.phase = phase
    r = ConnectionRecovery(service, phase_emitter=p, provider=XAI)
    return r


@pytest.mark.asyncio
async def test_fel_medan_den_sover_rapporteras_aldrig_och_ateransluts_aldrig():
    s = Fake()
    r = _recovery(s)
    r._route_error = AsyncMock()
    r.push_frame = AsyncMock()
    r._refresh_task = r._sov_task = object()  # no background loops in this test
    await r.process_frame(ErrorFrame(error="realtime receive loop ended — connection closed"),
                          FrameDirection.UPSTREAM)
    r._route_error.assert_not_awaited()
    await r.force_reconnect("wedge")
    assert r._recover_task is None


def test_tyst_nog_forst_efter_sov_efter_s(monkeypatch):
    monkeypatch.setenv("SOV_EFTER_S", "30")
    s = Fake()
    s.sover = False
    r = _recovery(s)
    now = time.monotonic()
    r._last_input_audio = r._last_wake = now - 31
    assert r._tyst_nog(now) is True
    r._last_input_audio = now - 5  # he spoke 5 s ago
    assert r._tyst_nog(now) is False
    r._last_input_audio = now - 31
    r._last_wake = now - 5  # woke 5 s ago
    assert r._tyst_nog(now) is False
    r._last_wake = now - 31
    r._phase_emitter.phase = "replying"  # still answering
    assert r._tyst_nog(now) is False


@pytest.mark.asyncio
async def test_sovloopen_kopplar_ner_efter_samtalet(monkeypatch):
    monkeypatch.setenv("SOV_EFTER_S", "5")
    s = Fake()
    s.sover = False
    r = _recovery(s)
    r.SOV_CHECK_S = 0.01
    r._last_input_audio = r._last_wake = time.monotonic() - 10
    task = asyncio.create_task(r._sov_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    assert s.sover is True and s.calls == ["ner"]


@pytest.mark.asyncio
async def test_vakningen_vacker_motorn():
    s = Fake()
    r = _recovery(s)
    await r.vakna()
    assert s.sover is False and s.calls == [("upp", False)]


@pytest.mark.asyncio
async def test_openai_lasloopens_slut_nar_den_sover_ar_inget_fel():
    service = _service(OPENAI)
    service.sover = True
    service.push_error = AsyncMock()

    class Ws:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    service._websocket = Ws()
    await asyncio.wait_for(service._receive_task_handler(), 1)
    service.push_error.assert_not_awaited()
