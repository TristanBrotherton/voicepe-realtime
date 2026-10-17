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
    ("Anta.", "stopp"), ("Tack till elever och personal", "eko"),      # Henrik's own "vänta" was written 'Änta'/'Anta'; noise phrases are no speech
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
