"""US-060 layer 2 in the Gemini engine: speech over his reply is held back until it is judged."""
import asyncio

import pytest
from pipecat.frames.frames import UserStartedSpeakingFrame

from app.providers import build_service
from test_gemini_provider import OPENAI_SHAPE, _ScriptedTurns, _frame, _options, _wire


def _bygg(events, vakt, dom):
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    turns = _ScriptedTurns(events)
    sent = _wire(service, turns)
    pushed, starter, barge = [], [], []

    async def push(frame, direction=None):
        pushed.append(frame)
    service.push_frame = push
    service.on_user_turn_start = lambda: starter.append(1)
    service.on_barge_in = lambda: barge.append(1)
    service.eko_vakt = vakt
    service.eko_dom = dom
    service.EKO_MIN_S = 0.04  # two 20 ms frames
    return service, turns, sent, pushed, starter, barge


async def _mata(service, antal):
    for b in range(antal):
        await service._send_user_audio(_frame(b + 1))
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_his_own_voice_over_his_reply_is_dropped_and_opens_no_turn():
    domda = []

    async def dom(pcm, rate):
        domda.append(len(pcm))
        return True
    service, turns, sent, pushed, starter, barge = _bygg(["start"] + [None] * 7, lambda: True, dom)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    await _mata(service, 6)
    assert len(domda) == 1  # judged once
    assert not any(isinstance(f, UserStartedSpeakingFrame) for f in pushed)
    assert starter == [] and barge == [] and getattr(service, "_held", None) is None and sent == []
    assert turns.resets == 1  # the detector starts over


@pytest.mark.asyncio
async def test_a_person_over_his_reply_interrupts_and_the_turn_keeps_the_start():
    async def dom(pcm, rate):
        return False
    service, turns, sent, pushed, starter, barge = _bygg(["start"] + [None] * 9, lambda: True, dom)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    await _mata(service, 6)
    assert barge == [1] and starter == [1]
    assert any(isinstance(f, UserStartedSpeakingFrame) for f in pushed)
    held = service._held
    assert held and held[1].audio == _frame(1).audio  # the first frame of the speech is in the turn
    n = len(held)
    await _mata(service, 2)
    assert len(service._held) > n  # the rest of the speech joins the same turn


@pytest.mark.asyncio
async def test_when_he_is_not_replying_a_start_is_a_start_at_once():
    async def dom(pcm, rate):
        raise AssertionError("judged although he was not replying")
    service, turns, sent, pushed, starter, barge = _bygg(["start", None, None], lambda: False, dom)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    await _mata(service, 3)
    assert starter == [1] and barge == []


@pytest.mark.asyncio
async def test_a_failing_judge_lets_the_speech_through():
    async def dom(pcm, rate):
        raise RuntimeError("stt down")
    service, turns, sent, pushed, starter, barge = _bygg(["start"] + [None] * 6, lambda: True, dom)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    await _mata(service, 6)
    assert barge == [1] and starter == [1]


@pytest.mark.asyncio
async def test_no_net_no_change():
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    assert service.eko_vakt is None and service.eko_dom is None
