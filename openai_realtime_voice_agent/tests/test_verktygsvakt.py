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


def test_while_he_speaks_only_a_stop_word_cuts_him():
    """Kontoret live 2026-10-12 21:01 and 2026-10-13 07:03: every one of these was his own voice coming back, was taken
    for a person, cut his reply and started a new one on top of it."""
    hans = "Räven rymde från jägaren och hittade en jävla massa mat. Slut."
    for eko in ("Fåla.", "Lämmar sa mord.", "Det finns där.", "Det är en stor.", "Tänd i köket.", "Det är en klockan.",
                "Tack till elever och personer som hjälpte med videon.", "Välkommen till mig."):
        assert eko_vakt.dom(eko, True, hans, under_tal=True), eko
    for eko in ("Björn.", "Vad sluts?", "Tack.", "Tack så mycket.", "Hör du?"):   # written of his echo in the replay
        assert eko_vakt.dom(eko, True, hans, under_tal=True), eko
    for person in ("Vänta.", "vänta lite", "Stopp!", "Björn, vänta", "sluta", "Tyst!"):
        assert not eko_vakt.dom(person, True, hans, under_tal=True), person
    assert eko_vakt.dom(None, False, hans, under_tal=True)        # a failed speech-to-text does not cut him


def test_just_after_he_stopped_a_short_answer_still_gets_through():
    """A on #65 and #70: the rule above must not eat the answer to his own question."""
    fraga = "Vilket rum vill du tända, köket eller sovrummet?"
    assert not eko_vakt.dom("badrummet", True, fraga)              # a person
    assert not eko_vakt.dom("släck lampan nu", True, fraga)
    assert not eko_vakt.dom(None, False, fraga)                    # in doubt it is the person
    assert eko_vakt.dom("Vilket rum vill du tända", True, fraga)   # his own words are still his own


def test_the_candidate_started_while_his_voice_was_in_the_room():
    assert eko_vakt.under_tal(99.0, 1.0, True)        # he is replying now
    assert eko_vakt.under_tal(1.5, 0.8, False)        # 0.8 s of speech that began 0.7 s after his last audio
    assert not eko_vakt.under_tal(3.0, 0.8, False)    # began 2.2 s after he went quiet: a person answering


def test_when_the_canceller_works_a_loud_sound_is_a_person_whatever_it_is_written_as():
    """Henrik 2026-10-13 09:48: his "vänta" over the reply was written 'Tack.' and 'Tack så mycket.' and ignored. The
    canceller had taken his echo 30 and 34 dB down just before; what was loud then was not Björn."""
    hans = "Jag är inte här för att berätta sagor. Jag är här för att hålla ordning på huset."
    assert not eko_vakt.dom("Tack.", True, hans, under_tal=True, erle_fore=29.9)
    assert not eko_vakt.dom("Tack så mycket.", True, hans, under_tal=True, erle_fore=34.8)
    assert not eko_vakt.dom(None, False, hans, under_tal=True, erle_fore=30.0)        # the speech-to-text failed: still a person
    assert eko_vakt.dom("hålla ordning på huset", True, hans, under_tal=True, erle_fore=30.0)   # his own words stay his
    assert not eko_vakt.dom("Tack till elever och personer som hjälpte med videon.", True, hans, under_tal=True, erle_fore=33.8)
    # not measured (the start of a reply) or poorly cancelled: the stop-word rule, as before
    for erle in (None, 4.0, 19.9):
        assert eko_vakt.dom("Tack.", True, hans, under_tal=True, erle_fore=erle)
        assert not eko_vakt.dom("Vänta.", True, hans, under_tal=True, erle_fore=erle)
    # and it only counts while he speaks
    assert not eko_vakt.dom("köket", True, hans, under_tal=False, erle_fore=30.0)


def test_the_guards_stay_on_for_the_turn_an_interruption_starts():
    """A on #72: an interruption turns the phase to listening, then thinking, and never idle -- so "replying or just
    after" was false exactly when that turn ended and the model called a tool. A cut on his own echo (the level alone
    cuts him) was then a turn of his own voice with the tools unguarded."""
    hans = "räven gick i skogen och sedan kom en uggla och sa åt honom"
    eko = "räven gick i skogen och sedan"
    # the phase is thinking, it never went idle, and he was cut 2 s ago
    assert eko_vakt.vakt_oppen("thinking", 0.0, 2.0) is True
    assert eko_vakt.veto_tur("thinking", 0.0, 2.0, eko, hans) is True                  # the turn is dropped
    assert "your own last reply" in eko_vakt.veto_verktyg("thinking", 0.0, 2.0, eko, hans)   # a tool does not run on it
    assert "unclear speech" in eko_vakt.veto_verktyg("thinking", 0.0, 2.0, "", hans)   # nor on speech nobody could write
    assert "unclear speech" in eko_vakt.veto_verktyg("listening", 0.0, 0.3, None, hans)
    # a real order after the interruption still runs
    assert eko_vakt.veto_verktyg("thinking", 0.0, 2.0, "tänd lampan i köket", hans) is None
    assert eko_vakt.veto_tur("thinking", 0.0, 2.0, "tänd lampan i köket", hans) is False
    # no interruption, or one long ago: outside his reply the guards are off, as before (A on #70)
    assert eko_vakt.vakt_oppen("thinking", 0.0, None) is False
    assert eko_vakt.veto_verktyg("thinking", 0.0, None, eko, hans) is None
    assert eko_vakt.veto_verktyg("thinking", 0.0, eko_vakt.TOOL_VAKT_S + 1, eko, hans) is None
    assert eko_vakt.veto_tur("thinking", 0.0, eko_vakt.TOOL_VAKT_S + 1, eko, hans) is False
    # while he replies and just after, as before
    assert eko_vakt.veto_verktyg("replying", 0.0, None, eko, hans) is not None
    import time as _t
    assert eko_vakt.veto_tur("idle", _t.monotonic() - 0.5, None, eko, hans) is True


def test_the_handler_asks_the_guards_through_the_window_that_covers_an_interruption():
    """The two places that decide (the turn veto, the tool guard) must not carry a window of their own again."""
    import inspect
    from app import websocket_handler
    src = inspect.getsource(websocket_handler)
    assert "eko_vakt.veto_tur(" in src and "eko_vakt.veto_verktyg(" in src
    assert "eko_vakt.vakt_verktyg(" not in src and "eko_vakt.liknar_svar(" not in src


@pytest.mark.parametrize("text", ["Vänta.", "Välta!", "Änta!", "Mänta.", "Tack.", "Stopp stopp", "Nej vänta lite", "vänta nu Björn", "", None])
def test_a_hold_on_word_or_no_words_at_all_asks_for_nothing(text):
    assert eko_vakt.bara_avbrott(text)


@pytest.mark.parametrize("text", ["Hjälp!", "Tänd.", "Släck", "Köket.", "Råd.", "Stå.", "Men vad är status på huset?", "Tänd lampan",
                                   "Vänta, vad sa du om huset?", "nästa låt"])
def test_any_other_word_said_over_him_is_a_turn_to_answer_also_alone(text):
    """Henrik 2026-10-13: one word that interrupts him must not be forced into "vänta"; "hjälp" and "tänd" must work."""
    assert not eko_vakt.bara_avbrott(text)


@pytest.mark.asyncio
async def test_a_bare_interruption_stops_him_and_nothing_is_answered_or_closed():
    """Kontoret 2026-10-13 16:20: "vänta" cut him, was written 'Tack.' / 'Välta!', and three seconds later he answered
    the word (or the session closed on the 'Tack.'). Henrik: "det flyter inte på som ett samtal"."""
    from app import bana0
    hant = []

    async def stt(pcm, timeout):
        return "Tack."

    async def fel(*a):
        raise AssertionError("the turn went on")

    async def vanta():
        hant.append("vanta")

    async def avsluta():
        hant.append("stangd")

    def tur(avbrott):
        return bana0.tur(b"\0" * 3200, stt=stt, timeout_stt=1.0, timeout_comms=1.0, skicka_svar_till_modellen=fel,
                         skapa_svar=fel, avsluta=avsluta, bara_avbrott=lambda t: avbrott and eko_vakt.bara_avbrott(t),
                         vanta=vanta)
    assert await tur(True) == "vanta" and hant == ["vanta"]          # he was cut: silent, listening, the session stays
    assert await tur(False) == "nej" and hant == ["vanta", "stangd"]  # nobody cut him: "tack" closes, as before


def test_a_muted_reply_is_not_him_speaking():
    from app.raw_audio_serializer import RawAudioSerializer
    s = RawAudioSerializer.__new__(RawAudioSerializer)
    assert s.tystad() is False
    s.tysta_svar(5.0)
    assert s.tystad() is True
    s._tysta_tills = 1.0
    assert s.tystad() is False


def test_a_turn_long_after_the_last_reply_with_no_wake_is_the_room_not_a_question():
    from app.websocket_handler import tur_utan_vackning
    assert tur_utan_vackning(2944.6, 3775.9, 60.0) is True       # Kontoret 18:08: he answered the room
    assert tur_utan_vackning(1430.5, 5411.5, 60.0) is True       # and 18:35
    assert tur_utan_vackning(4.6, 3804.8, 60.0) is False         # a follow-up a few seconds after his reply
    assert tur_utan_vackning(2944.6, 1.2, 60.0) is False         # he was just woken
    assert tur_utan_vackning(-1.0, 5000.0, 60.0) is False        # never idle yet
    assert tur_utan_vackning(2944.6, 3775.9, 0.0) is False       # switched off


def test_the_turn_start_asks_whether_anyone_woke_him():
    import inspect
    from app import websocket_handler
    src = inspect.getsource(websocket_handler)
    assert "if tur_utan_vackning(sedan_idle, sedan_vakning, None, serializer.sedan_svar()):" in src and "_somna_utan_vackning()" in src
    from app.websocket_handler import tur_utan_vackning
    assert tur_utan_vackning(2944.6, 3775.9, 60.0, 2.0) is False     # he is replying: the turn cuts him off, it is no stray


@pytest.mark.asyncio
async def test_after_vanta_the_mic_stays_open_and_he_says_nothing_until_the_person_goes_on():
    """Henrik 2026-10-13: "om jag säger vänta så ska den inte stänga av mikrofonen"."""
    import asyncio
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, UserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection
    from app.phase_emitter import PhaseEmitter
    skickat, tyst = [], []

    async def send(fas):
        skickat.append(fas)
    em = PhaseEmitter(send, idle_debounce_s=0.01)

    async def push(frame, direction=None):
        pass
    em.push_frame = push

    async def inget_mer():
        tyst.append(1)
    await em._emit("thinking")
    await em.hall_lyssnar(0.15, inget_mer)
    assert skickat[-1] == "listening"
    await em.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)   # the muted tail of the old reply
    await em.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.05)
    assert skickat[-1] == "listening" and tyst == []                 # the device still listens: mic open, nothing said
    await em.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)  # the person goes on
    await asyncio.sleep(0.2)
    assert tyst == [] and "idle" not in skickat                      # a normal turn from here: the wait was called off
    await em.hall_lyssnar(0.05, inget_mer)                           # cut off again, and this time nothing more is said
    await asyncio.sleep(0.15)
    assert tyst == [1] and skickat[-1] == "idle"                     # the turn is dropped and the device closes its mic


@pytest.mark.asyncio
async def test_when_he_is_cut_off_his_own_stop_does_not_close_the_mic():
    """Kontoret 2026-10-13 18:59: idle went to the device 1.15 s after the cut, in the middle of the person's turn, and the
    device closes its mic on an idle that comes while it listens."""
    import asyncio
    from pipecat.frames.frames import BotStoppedSpeakingFrame, UserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection
    from app.phase_emitter import PhaseEmitter
    skickat = []

    async def send(fas):
        skickat.append(fas)
    em = PhaseEmitter(send, idle_debounce_s=0.01)

    async def push(frame, direction=None):
        pass
    em.push_frame = push
    await em._emit("replying")
    await em.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)   # the person cuts him off
    await em.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)    # ...so he stops
    await asyncio.sleep(0.1)
    assert skickat[-1] == "listening" and "idle" not in skickat
