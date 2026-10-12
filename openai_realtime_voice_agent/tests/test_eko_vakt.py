"""US-060 layer 2: his own words heard back are ignored, other speech is an interruption."""
from app import eko_vakt as ev

BJORN = "Jag sätter på radion i kontoret nu. Det är P3 som spelar, och den går ganska högt."


def test_his_own_words_are_echo():
    assert ev.ar_eko("sätter på radion i kontoret", BJORN)
    assert ev.ar_eko("det är p3 som spelar", BJORN)


def test_an_inexact_local_transcript_of_his_voice_is_still_echo():
    assert ev.ar_eko("jag sätter på radion i kontoret nu", BJORN)  # exact short words, one long word a little off is fine
    assert ev.ar_eko("jag sätter på radion i kontorett", BJORN)  # a long word within the likeness


def test_other_speech_is_an_interruption():
    assert not ev.ar_eko("vänta stopp", BJORN)
    assert not ev.ar_eko("nej lägg till mjölk på listan", BJORN)


def test_nothing_intelligible_is_not_an_interruption():
    assert ev.ar_eko(None, BJORN) and ev.ar_eko("", BJORN) and ev.ar_eko("...", BJORN)
    assert ev.dom("", True, BJORN)  # it listened and heard no words: noise, ignored


def test_a_speech_to_text_that_missed_is_a_person_not_echo():
    # A's FAIL on #65 (a): timeout/error = we do not know what was said = in doubt it is him
    assert not ev.dom(None, False, "jag fixar det")
    assert not ev.dom("", False, BJORN)
    assert ev.dom("sätter på radion", True, BJORN)  # a clear echo is still ignored


def test_a_short_interruption_is_not_hushed_by_a_similar_long_word():
    # A's FAIL on #65 (b): 'vänta' against his 'väntar', 'sluta' against 'slutar', 'ja' against 'jag', 'nej' against 'nej det går inte'
    assert not ev.ar_eko("vänta", "jag väntar på svaret nu")
    assert not ev.ar_eko("sluta", "jag slutar nu")
    assert not ev.ar_eko("ja", "jag fixar det")
    assert not ev.ar_eko("nej", "jag tror inte det")
    assert ev.ar_eko("nej", "nej det går inte")  # the same word, exactly: his own


def test_before_he_has_said_anything_any_speech_is_a_person():
    assert not ev.ar_eko("hallå", "")


def test_a_mixed_utterance_below_the_share_is_an_interruption():
    # one of four words is his: 25 % < 60 %
    assert not ev.ar_eko("stopp vänta lite radion", BJORN)


def test_svar_keeps_the_current_reply_and_the_last_two():
    s = ev.Svar()
    for t in ("ett svar", "två svar", "tre svar"):
        s.mata(t)
        s.klar()
    s.mata("fyra pågår")
    assert "ett" not in s.text() and "två" in s.text() and "tre" in s.text() and "fyra" in s.text()


def test_the_tail_after_a_reply():
    assert ev.i_svans(100.0, nu=101.0) and not ev.i_svans(100.0, nu=102.0) and not ev.i_svans(0.0, nu=1.0)


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("BARGE_IN", raising=False)
    assert not ev.paa()
    monkeypatch.setenv("BARGE_IN", "1")
    assert ev.paa()
    monkeypatch.setenv("ANTI_EKO", "0")
    assert not ev.nat_paa()


def test_what_the_stt_writes_of_noise_is_never_a_person():
    # the live capture 2026-10-12: 'Tack till elever och personal vid Säkerhetssäkerheten.' from the room floor
    assert ev.dom("Tack till elever och personal vid Värmland.", True, BJORN)
    assert not ev.dom("vänta", True, BJORN)
