"""US-060 AC-17/AC-18 (Henrik 2026-10-17): the stop-word guard over his reply. A stop word cuts him at once; a back-channel never."""
import asyncio

import pytest
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame, UserStartedSpeakingFrame

from app import eko_vakt
from test_gemini_egen_tur import _bygg

HANS = "Det här är ett ljudprov på svenska. Jag berättar en lång historia om ett gammalt torp i skogen."


@pytest.mark.parametrize("hort,dom", [
    ("Vänta.", "stopp"), ("Vänta lite.", "stopp"), ("Stopp!", "stopp"), ("Tyst.", "stopp"), ("Björn.", "stopp"),
    ("Stå.", "stopp"), ("Tis.", "stopp"), ("Välta?", "stopp"),            # how the local speech-to-text spells a short stop word
    ("Ja.", "medhall"), ("Okej.", "medhall"), ("Aa!", "medhall"), ("Precis.", "medhall"),
    ("M. M.", "medhall"), ("Åååååå.", "medhall"), ("Mmmm.", "medhall"), ("Åh åh.", "medhall"), ("Mm.", "medhall"),      # 'M. M.' and 'Åh åh.' were cut before (measured 2026-10-17)
    ("Anta.", "stopp"), ("Det är bra, stå inte där.", "annat"), ("Vi kan anta det.", "annat"),   # a misspelling counts only as the FIRST word ("Tack till elever och personal", "eko"),      # Henrik's own "vänta" was written 'Änta'/'Anta'; noise phrases are no speech
    ("Det här är ett ljudprov", "eko"), ("Böj en klock.", "annat"), ("", "inget"),
])
def test_stopp_dom(hort, dom):
    assert eko_vakt.stopp_dom(hort, bool(hort), HANS) == dom


def test_a_stt_that_failed_cuts_nothing():
    assert eko_vakt.stopp_dom("Vänta.", False, HANS) == "inget"


def _hogt(i):
    return InputAudioRawFrame(audio=(8000).to_bytes(2, "little", signed=True) * 320, sample_rate=16000, num_channels=1)  # -12 dBFS, 20 ms


async def _tala(service, verdict, genererar=True, antal=36):
    service.svarar = lambda: True
    service._genererar = genererar
    anrop, klipp = [], []

    async def dom(pcm, rate):
        anrop.append(len(pcm))
        return verdict
    service.stopp_dom = dom
    service.on_stopp_klipp = klipp.append

    async def inget():
        return None
    service._handle_interruption = inget
    for i in range(antal):
        await service._send_user_audio(_hogt(i))
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    await service._send_user_audio(_hogt(99))
    return anrop, klipp


@pytest.mark.asyncio
async def test_a_stop_word_over_his_reply_cuts_him(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    anrop, klipp = await _tala(service, "stopp")
    assert len(anrop) == 1 and anrop[0] >= 0.6 * 32000        # written out once, after >= 0.6 s of speech
    assert any(isinstance(f, InterruptionFrame) for f in pushed) and any(isinstance(f, UserStartedSpeakingFrame) for f in pushed)
    assert klipp == [True]                                    # the engine is still generating: the tail is muted
    assert len(sent) == 37                                    # the engine still hears all of it


@pytest.mark.asyncio
async def test_an_unwritable_sound_must_be_louder_than_speech_to_cut(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    service.STOPP_NIVA_INGET_DBFS = -5.0      # the test frames are -12 dBFS: a bang that loud is not enough, speech is
    anrop, klipp = await _tala(service, "inget")
    assert klipp == [] and not any(isinstance(f, InterruptionFrame) for f in pushed)
    service2, sent2, pushed2 = _bygg(monkeypatch, ["start"] + [None] * 60)
    service2.STOPP_NIVA_INGET_DBFS = -5.0
    anrop2, klipp2 = await _tala(service2, "annat")
    assert klipp2 == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("dom", ["annat", "inget"])
async def test_loud_speech_that_is_no_back_channel_cuts_him_too(monkeypatch, dom):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    anrop, klipp = await _tala(service, dom)    # -12 dBFS: well over the -30 floor
    assert klipp == [True] and any(isinstance(f, InterruptionFrame) for f in pushed)


@pytest.mark.asyncio
async def test_a_back_channel_never_cuts_him(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    anrop, klipp = await _tala(service, "medhall")
    assert len(anrop) == 1 and klipp == [] and pushed == []


@pytest.mark.asyncio
async def test_the_tail_is_not_muted_when_the_engine_has_sent_it_all(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    anrop, klipp = await _tala(service, "stopp", genererar=False)
    assert klipp == [False]    # a mute that outlives the reply would swallow the answer to the next question


@pytest.mark.asyncio
async def test_the_mute_ends_with_the_cut_replys_turn(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    slut = []
    service.on_stopp_slut = lambda: slut.append(1)
    await _tala(service, "stopp")
    service._stopp_tystnad_slut()
    service._stopp_tystnad_slut()
    assert slut == [1]         # once


@pytest.mark.asyncio
async def test_nothing_is_judged_when_he_is_not_replying(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    anrop = []

    async def dom(pcm, rate):
        anrop.append(1)
        return "stopp"
    service.stopp_dom = dom
    service.svarar = lambda: False
    for i in range(36):
        await service._send_user_audio(_hogt(i))
        await asyncio.sleep(0)
    assert anrop == [] and not any(isinstance(f, InterruptionFrame) for f in pushed)


@pytest.mark.asyncio
@pytest.mark.parametrize("dom", ["eko", "medhall"])
async def test_his_own_words_and_back_channels_never_cut_however_loud(monkeypatch, dom):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    anrop, klipp = await _tala(service, dom)
    assert klipp == [] and not any(isinstance(f, InterruptionFrame) for f in pushed)


@pytest.mark.asyncio
async def test_quiet_room_noise_over_his_reply_is_not_a_stop_word(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    service.svarar = lambda: True
    service._genererar = True

    async def dom(pcm, rate):
        return "stopp"
    service.stopp_dom = dom
    for i in range(36):   # -90 dBFS: under the floor, whatever the speech-to-text made of it
        await service._send_user_audio(InputAudioRawFrame(audio=bytes([3, 0]) * 320, sample_rate=16000, num_channels=1))
        await asyncio.sleep(0)
    await service._send_user_audio(InputAudioRawFrame(audio=bytes([3, 0]) * 320, sample_rate=16000, num_channels=1))
    assert not any(isinstance(f, InterruptionFrame) for f in pushed)


@pytest.mark.asyncio
async def test_the_mute_also_ends_with_an_empty_turn_complete_after_a_tool_call(monkeypatch):
    """G on #95: the early return for the empty turn_complete skipped the end of the mute; the answer to the new turn was swallowed."""
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    slut = []
    service.on_stopp_slut = lambda: slut.append(1)
    await _tala(service, "stopp")
    assert service._stopp_tyst is True
    service._answer_after_tool = True

    class Msg:
        pass
    await service._handle_msg_turn_complete(Msg())
    assert slut == [1] and service._stopp_tyst is False


@pytest.mark.asyncio
async def test_a_tool_call_after_a_cut_ends_the_mute(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 60)
    slut = []
    service.on_stopp_slut = lambda: slut.append(1)
    await _tala(service, "stopp")

    async def inget(m):
        return None
    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
    monkeypatch.setattr(GeminiLiveLLMService, "_handle_msg_tool_call", lambda self, m: inget(m))
    await service._handle_msg_tool_call(object())
    assert slut == [1]


@pytest.mark.asyncio
async def test_a_short_stop_word_that_ended_before_the_verdict_still_closes_the_turn(monkeypatch):
    """G on #95: 'vänta' (0.4 s) ends before the speech-to-text answers. The cut reopens 'listening'; the engine-cut rule
    (AVBROTT_UTAN_TAL_S with no local speech) must close it again, or the device would stay in 'listening'."""
    from pipecat.frames.frames import UserStoppedSpeakingFrame
    service, sent, pushed = _bygg(monkeypatch, ["start"] + [None] * 30 + ["end"] + [None] * 80)
    service.STOPP_MIN_S = 0.2
    anrop, klipp = await _tala(service, "stopp", antal=34)
    assert klipp == [True]
    import time as _t
    service._avbrott_t = _t.monotonic() - 2.0     # the 1.2 s without local speech have passed
    await service._send_user_audio(_hogt(100))
    assert any(isinstance(f, UserStoppedSpeakingFrame) for f in pushed)
