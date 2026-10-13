"""US-060: his own voice coming back through the mic must not run a house tool (Kontoret 2026-10-12 20:29: the loop
made Björn call HassTurnOn on his own question 'Vilken dörr vill du låsa?')."""
import pytest

from app import eko_vakt
from tests.test_ha_misslyckades import _call, _service

SVAR = "Vilken dörr vill du låsa? Är det hallen du vill att jag ska titta på?"


def test_his_own_words_back_are_caught_but_a_short_real_order_is_not():
    assert eko_vakt.vakt_verktyg("Vilken dörr vill du låsa?", SVAR, None) is not None
    assert eko_vakt.vakt_verktyg("Är det hallen du vill", SVAR, 30.0) is not None
    assert eko_vakt.vakt_verktyg("tänd lampan", SVAR, None) is None            # too few words: never an echo
    assert eko_vakt.vakt_verktyg("släck lampan i köket tack", SVAR, 2.0) is None  # other words: a person
    assert eko_vakt.vakt_verktyg("Vilken dörr vill du låsa?", "", None) is None   # he has said nothing: not an echo


def test_unclear_speech_right_after_an_interruption_is_held_but_later_it_is_not():
    assert eko_vakt.vakt_verktyg("", SVAR, 3.0) is not None
    assert eko_vakt.vakt_verktyg("", SVAR, 30.0) is None
    assert eko_vakt.vakt_verktyg("", SVAR, None) is None
    assert eko_vakt.vakt_verktyg("släck lampan", SVAR, 3.0) is None               # clear words of its own: let through


@pytest.mark.asyncio
async def test_a_tool_does_not_run_when_the_guard_says_no_and_runs_when_it_does_not():
    service = _service()
    ran = []

    async def tool(params):
        ran.append(params.function_name)
        await params.result_callback("ok")

    service.register_function("intent__HassTurnOn", tool)
    service.verktygsvakt = lambda: "ignored: echo"
    got = await _call(service, "intent__HassTurnOn", {"area": "hall"})
    assert ran == [] and "ignored" in str(got)
    service.verktygsvakt = lambda: None
    await _call(service, "intent__HassTurnOn", {"area": "hall"})
    assert ran == ["intent__HassTurnOn"]


def test_a_transcript_that_is_behind_the_audio_counts_as_his_own_voice():
    assert eko_vakt.utskrift_efter(0, 1.0) is True            # audio is playing, nothing written yet
    assert eko_vakt.utskrift_efter(0, 0.2) is False           # the reply has barely begun: nothing to compare yet
    assert eko_vakt.utskrift_efter(3, 8.0) is True            # 8 s of audio, 3 words: far behind
    assert eko_vakt.utskrift_efter(20, 8.0) is False          # caught up: the word comparison decides


def test_the_serializer_counts_the_seconds_of_the_current_reply():
    import asyncio
    from pipecat.frames.frames import OutputAudioRawFrame
    from app.raw_audio_serializer import RawAudioSerializer
    ser = RawAudioSerializer("kontoret", input_sample_rate=16000)
    asyncio.run(ser.serialize(OutputAudioRawFrame(audio=b"\x01\x00" * 24000, sample_rate=24000, num_channels=1)))
    assert abs(ser.svar_sek() - 1.0) < 0.01
    ser._svar_sist -= 3.0
    assert ser.svar_sek() == 0.0                              # a new reply begins after a pause


def test_the_cut_holds_a_moment_after_the_next_turn_is_decided():
    from app.raw_audio_serializer import RawAudioSerializer
    ser = RawAudioSerializer("kontoret", input_sample_rate=16000)
    ser.tysta_slut()
    assert ser._tysta_tills == 0.0                            # never cut: nothing to hold
    ser.tysta_svar()
    ser.tysta_slut()
    assert 0 < ser._tysta_tills - __import__("time").monotonic() <= 0.7


@pytest.mark.asyncio
async def test_a_turn_that_is_his_own_last_reply_is_dropped_and_the_model_is_never_asked():
    from app import bana0
    anrop = []

    async def stt(pcm, t):
        return "Vilken dörr vill du låsa?"

    async def aldrig(*a, **k):
        anrop.append("modellen fragades")

    async def kasta():
        anrop.append("kastad")

    svar_text = "Vilken dörr vill du låsa? Är det hallen du vill"
    bana = await bana0.tur(b"\x00" * 3200, stt=stt, timeout_stt=1.0, timeout_comms=1.0,
                           skicka_svar_till_modellen=aldrig, skapa_svar=aldrig,
                           eko_veto=lambda t: eko_vakt.liknar_svar(t, svar_text), kasta_tur=kasta)
    assert bana == "eko" and anrop == ["kastad"]
    anrop.clear()
    bana = await bana0.tur(b"\x00" * 3200, stt=stt, timeout_stt=1.0, timeout_comms=1.0,
                           skicka_svar_till_modellen=aldrig, skapa_svar=aldrig,
                           eko_veto=lambda t: False, kasta_tur=kasta)
    assert bana == "modell" and "modellen fragades" in anrop and "kastad" not in anrop


def test_answering_his_choice_question_with_a_word_from_it_is_not_an_echo():
    """A on #70: 'Vilket rum vill du tända, köket eller sovrummet?' -> 'köket' / 'tänd köket' must reach him and the house."""
    fraga = "Vilket rum vill du tända, köket eller sovrummet?"
    for svar in ("köket", "tänd köket", "ja köket tack"):
        assert not eko_vakt.liknar_svar(svar, fraga)
        assert eko_vakt.vakt_verktyg(svar, fraga, None) is None
    assert eko_vakt.liknar_svar("Vilket rum vill du tända", fraga)   # his own sentence back is still caught


def test_one_or_two_odd_words_over_his_reply_do_not_cut_him_but_a_stop_word_does():
    """Kontoret 2026-10-12 live test: 'Fåla.' (the STT's spelling of his own voice) cut his reply."""
    hans = "en räv gick i skogen"
    assert eko_vakt.dom("Fåla.", True, hans)           # ignored
    assert not eko_vakt.dom("Vänta.", True, hans)      # a person
    assert not eko_vakt.dom("släck lampan nu", True, hans)
