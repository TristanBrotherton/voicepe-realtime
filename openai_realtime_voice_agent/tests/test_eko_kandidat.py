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


def _hog(b):
    """A frame of speech-level audio (about -20 dBFS); b makes frames tell apart."""
    from pipecat.frames.frames import InputAudioRawFrame
    return InputAudioRawFrame(audio=bytes([b, 0x30]) * 320, sample_rate=16000, num_channels=1)


async def _mata(service, antal):
    for b in range(antal):
        await service._send_user_audio(_hog(b + 1))
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
    assert held and held[1].audio == _hog(1).audio  # the first frame of the speech is in the turn
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


@pytest.mark.asyncio
async def test_speech_under_the_level_floor_is_noise_even_when_the_stt_writes_words():
    """The echo capture 2026-10-12: the room floor (-42..-45 dBFS) fooled the VAD for 4.8 s and the STT wrote 'Björn.'"""
    from pipecat.frames.frames import InputAudioRawFrame

    async def dom(pcm, rate):
        return False  # the words are not his own

    service, turns, sent, pushed, starter, barge = _bygg(["start"] + [None] * 7, lambda: True, dom)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    tyst = InputAudioRawFrame(audio=(b"\x10\x00" * 320), sample_rate=16000, num_channels=1)  # about -66 dBFS
    for _ in range(6):
        await service._send_user_audio(tyst)
        await asyncio.sleep(0.01)
    assert barge == [] and starter == [] and turns.resets == 1


@pytest.mark.asyncio
async def test_loud_speech_over_the_floor_still_interrupts():
    from pipecat.frames.frames import InputAudioRawFrame

    async def dom(pcm, rate):
        return False

    service, turns, sent, pushed, starter, barge = _bygg(["start"] + [None] * 7, lambda: True, dom)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    hogt = InputAudioRawFrame(audio=(b"\x00\x30" * 320), sample_rate=16000, num_channels=1)  # about -20 dBFS
    for _ in range(6):
        await service._send_user_audio(hogt)
        await asyncio.sleep(0.01)
    assert barge == [1] and starter == [1]


@pytest.mark.asyncio
async def test_loud_over_a_working_canceller_cuts_him_without_the_detector_or_the_text():
    """Henrik's shouted "stopp" (Kontoret 2026-10-13 12:08): the turn detector never called it speech, so nothing was
    judged and nothing stopped. The level alone cuts him, and the turn ends on its own when the detector stays silent."""
    async def dom(pcm, rate):
        raise AssertionError("the speech-to-text was asked")
    slut = []

    async def tur_slut():
        slut.append(1)
        service._held = None
    hogt = [False, False, True, True, True, True, True, True]
    service, turns, sent, pushed, starter, barge = _bygg([None] * 8, lambda: True, dom)
    service.niva_person = lambda: hogt.pop(0)
    service.on_user_turn_end = tur_slut
    turns.silence_s = 0.0
    await _mata(service, 4)
    assert barge == [1] and starter == [1]                 # cut once, although it stayed loud
    assert any(isinstance(f, UserStartedSpeakingFrame) for f in pushed)
    assert service._held and service._held[-1].audio == _hog(4).audio and slut == []
    service._niva_slut = 0.001                             # the wait for the detector is over
    await _mata(service, 2)
    assert slut == [1]                                     # the turn was ended and decided


@pytest.mark.asyncio
async def test_the_detector_takes_over_the_end_of_a_turn_opened_on_level():
    async def dom(pcm, rate):
        return False
    hogt = [True] + [False] * 9
    service, turns, sent, pushed, starter, barge = _bygg([None, "start", None, None], lambda: True, dom)
    service.niva_person = lambda: hogt.pop(0)
    service.on_user_turn_end = lambda: asyncio.sleep(0)
    await _mata(service, 2)
    assert barge == [1] and service._niva_slut == 0.0      # the detector said "start": its own "end" ends the turn
