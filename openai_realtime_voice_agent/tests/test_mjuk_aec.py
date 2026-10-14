"""Software echo canceller (SOFT_AEC=1, raawr US-060, path B)."""
import numpy as np
import pytest

from app.mjuk_aec import FRAME, RATE, MjukAec

pytest.importorskip("aec_audio_processing")


@pytest.fixture(autouse=True)
def _flaggan(monkeypatch):
    monkeypatch.setenv("SOFT_AEC", "1")


def _tal(sek, seed=1):
    """Speech-like: band-limited noise with syllable-rate amplitude changes."""
    r = np.random.default_rng(seed)
    n = int(sek * RATE)
    x = np.convolve(r.normal(0, 1, n), np.ones(4) / 4, "same")
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * np.arange(n) / RATE) ** 2
    return (x * env * 6000).astype(np.int16)


def _eko(ref, tap_ms=(150, 190, 240), amp=(0.5, 0.25, 0.12)):
    ut = np.zeros(len(ref) + RATE, dtype=np.float32)
    for ms, g in zip(tap_ms, amp):
        d = int(ms * RATE / 1000)
        ut[d:d + len(ref)] += g * ref
    return ut[:len(ref)].astype(np.int16)


def _kor(aec_pa, sek=12.0, forst=0.02, vid=0.0):
    ref = _tal(sek)
    mic = _eko(ref)
    klock = [0.0]
    aec = MjukAec(klocka=lambda: klock[0]) if aec_pa else None
    ut, steg = [], int(forst * RATE)
    for i in range(0, len(ref) - steg, steg):
        t = i / RATE
        klock[0] = t
        if aec:
            aec.ref(ref[i:i + steg].tobytes(), RATE, t)                 # sent now, played later
            klock[0] = t + forst
            ut.append(aec.process(mic[i:i + steg].tobytes(), t + forst))
        else:
            ut.append(mic[i:i + steg].tobytes())
    return mic, np.frombuffer(b"".join(ut), dtype=np.int16)


def _db(x):
    return 20 * np.log10(max(np.sqrt(np.mean(x.astype(np.float64) ** 2)), 1e-3) / 32768)


def test_the_echo_is_taken_down_when_the_reference_leads(monkeypatch):
    monkeypatch.setenv("SOFT_TAL_DB", "0")
    mic, ut = _kor(True)
    a, b = 6 * RATE, 11 * RATE
    erle = _db(mic[a:b]) - _db(ut[a - FRAME * 3:b - FRAME * 3])
    assert erle >= 12, f"only {erle:.1f} dB"


def _tva_svar(med_stampel, hall_s=0.33):
    """Two replies 10 s apart. The device plays the first at once and holds the second `hall_s` (its jitter buffer)."""
    ref = _tal(16.0)
    n8, steg = 8 * RATE, 320
    mic = np.concatenate([_eko(ref[:n8]), _eko(ref[n8:], tap_ms=tuple(ms + int(hall_s * 1000) for ms in (150, 190, 240)))])
    klock = [0.0]
    aec = MjukAec(klocka=lambda: klock[0])
    aec.matt_max = 10 ** 6                                             # keep every frame: the test asks afterwards
    for i in range(0, len(ref) - steg, steg):
        t = i / RATE + (10.0 if i >= n8 else 0.0)
        klock[0] = t
        aec.ref(ref[i:i + steg].tobytes(), RATE, t)
        if med_stampel and i == n8 + (int((hall_s + 0.03) * RATE) // steg) * steg:
            aec.spelstart(hall_s, t)                                   # the device's play_stamp arrives
        aec.process(mic[i:i + steg].tobytes(), t + steg / RATE)
    return aec, 18.0


def test_the_reference_moves_with_the_device_when_it_holds_the_start_of_a_reply():
    """Kontoret 2026-10-13 07:03: the first reply after the wake came 470 ms after the reference (120-170 ms on the
    following ones) and was not cancelled at all. The device reports the wait (play_stamp)."""
    utan, t0 = _tva_svar(False)
    med, _ = _tva_svar(True)
    e_utan, e_med = utan.erle(t0 + 0.6, t0 + 2.6), med.erle(t0 + 0.6, t0 + 2.6)
    assert e_med is not None and e_med >= 12, f"with the stamp only {e_med} dB"
    assert e_utan is None or e_med >= e_utan + 6, f"the stamp did not help: {e_utan:.1f} -> {e_med:.1f} dB"


def test_erle_says_how_much_was_taken_off_and_nothing_when_nothing_played():
    aec, _ = _tva_svar(False, hall_s=0.0)
    assert aec.erle(4.0, 8.0) >= 12
    assert aec.erle(9.0, 17.5) is None             # the pause between the replies: no measurement


def test_while_he_speaks_the_gain_is_fixed_and_the_agc_stands_still(monkeypatch):
    """Live 2026-10-13: the AGC lifted what was left of his echo by up to 24 dB and the turn detector took it for speech."""
    monkeypatch.setenv("SOFT_TAL_DB", "12")
    mic, ut = _kor(True)
    monkeypatch.setenv("SOFT_TAL_DB", "0")
    _, utan = _kor(True)
    a, b = 6 * RATE, 11 * RATE
    assert 11.0 <= _db(ut[a:b]) - _db(utan[a:b]) <= 13.0


def test_it_is_only_off_by_flag(monkeypatch):
    monkeypatch.delenv("SOFT_AEC", raising=False)
    monkeypatch.delenv("AEC_SPARA", raising=False)
    assert MjukAec.skapa() is None
    monkeypatch.setenv("SOFT_AEC", "1")
    assert MjukAec.skapa() is not None


def test_no_reference_and_agc_off_means_the_mic_passes_untouched():
    aec = MjukAec(klocka=lambda: 100.0)
    aec.agc_max_db = 0
    pcm = _tal(0.1).tobytes()
    assert aec.process(pcm, 100.0) == pcm


def test_a_failure_switches_it_off_and_the_mic_goes_on():
    aec = MjukAec(klocka=lambda: 0.0)
    aec.ref(_tal(0.1).tobytes(), RATE, 0.0)
    aec.ap = None   # anything that throws
    pcm = _tal(0.05).tobytes()
    assert aec.process(pcm, 0.05) == pcm and aec.trasig


def _ram_db(x):
    return _db(x)


def test_the_agc_lifts_distant_speech_but_not_the_noise_floor():
    r = np.random.default_rng(5)
    brus = (r.normal(0, 1, 3 * RATE) * 10 ** (-50 / 20) * 32768).astype(np.int16)     # floor -50 dBFS
    tal = np.concatenate([_tal(2.0, seed=9) * 0.2, np.zeros(RATE // 2, dtype=np.int16)])   # far away: ~-36 dBFS
    tal = np.pad(tal, (0, max(0, len(brus) - len(tal))))[:len(brus)]
    aec = MjukAec(klocka=lambda: 0.0)
    ut_brus = np.frombuffer(aec.process(brus.tobytes(), 3.0), dtype=np.int16)
    assert _db(ut_brus) < -46, "noise alone must not be lifted"
    mix = (brus.astype(np.int32) + tal).clip(-32768, 32767).astype(np.int16)
    aec2 = MjukAec(klocka=lambda: 0.0)
    ut = np.frombuffer(aec2.process(mix.tobytes(), 3.0), dtype=np.int16)
    assert _db(ut[RATE:2 * RATE]) > _db(mix[RATE:2 * RATE]) + 6, "distant speech should be lifted"
    aec3 = MjukAec(klocka=lambda: 0.0)
    aec3.agc_max_db = 0
    assert np.array_equal(np.frombuffer(aec3.process(mix.tobytes(), 3.0), dtype=np.int16)[:RATE], mix[:RATE])


def test_aec_spara_saves_mic_and_reference_as_a_stereo_wav(monkeypatch, tmp_path):
    import wave
    monkeypatch.setenv("SOFT_AEC", "0")
    monkeypatch.setenv("AEC_SPARA", "1")
    monkeypatch.setenv("AEC_SPARA_KATALOG", str(tmp_path))
    klock = [0.0]
    aec = MjukAec.skapa()
    assert aec is not None and aec.ap is None            # record only: no canceller, the mic passes untouched
    aec.klocka = lambda: klock[0]
    ref = _tal(3.0)
    mic = _eko(ref)
    steg = int(0.02 * RATE)
    for i in range(0, len(ref) - steg, steg):
        t = i / RATE
        aec.ref(ref[i:i + steg].tobytes(), RATE, t)
        aec.process(mic[i:i + steg].tobytes(), t + 0.02)
    aec.process(b"\x00\x00" * steg, 20.0)               # long after: the spell is over, the file is written
    filer = list(tmp_path.glob("aec_*.wav"))
    assert len(filer) == 1
    with wave.open(str(filer[0])) as w:
        assert w.getnchannels() == 2 and w.getframerate() == RATE and w.getnframes() >= 2 * RATE


def test_the_canceller_is_kept_per_device_across_reconnects(monkeypatch):
    monkeypatch.setattr(MjukAec, "_per_enhet", {})
    a = MjukAec.for_enhet("kontoret")
    assert a is not None and MjukAec.for_enhet("kontoret") is a and MjukAec.for_enhet("koket") is not a


def test_mic_packets_that_arrive_in_clumps_do_not_move_the_reference():
    """Kontoret 2026-10-13 09:35, a clip through the reply path: the canceller took 2.3 dB with each mic packet timed
    by its arrival and 32 dB with a steady reference. The mic's time is counted from its own samples."""
    ref = _tal(10.0)
    mic = _eko(ref)
    steg = 320
    klock = [0.0]
    aec = MjukAec(klocka=lambda: klock[0])
    aec.matt_max = 10 ** 6
    r = np.random.default_rng(3)
    for i in range(0, len(ref) - steg, steg):
        t = i / RATE
        aec.ref(ref[i:i + steg].tobytes(), RATE, t)
        sen = float(r.choice([0.0, 0.0, 0.012, 0.025, 0.04]))          # the network holds some packets back
        aec.process(mic[i:i + steg].tobytes(), t + steg / RATE + sen)
    assert aec.erle(5.0, 9.5) >= 12, f"only {aec.erle(5.0, 9.5):.1f} dB"


def _matt(aec, n, mic, ren, ref=-18.0, t0=0.0):
    aec.matt += [(t0 + (len(aec.matt) + 1) * 0.01, mic, ren, ref) for _ in range(n)]


def test_louder_than_his_own_echo_over_a_working_canceller_is_a_person():
    def laget(ren_fore, ren_nu, svar_start=-10.0):
        aec = MjukAec(klocka=lambda: 0.0)
        aec.svar_start = svar_start
        _matt(aec, 300, -25.0, ren_fore)          # 3 s of his echo: -25 in the mic, this much left after the canceller
        _matt(aec, 20, -15.0, ren_nu)             # then 0.2 s of this out of the canceller
        return aec

    assert laget(-55.0, -15.0).person(-36.0) is True       # 30 dB taken off before it, and louder than his echo gets
    assert laget(-55.0, -30.0).person(-36.0) is False      # over the fixed floor but not over his echo: it may be him, slipped through
    assert laget(-28.0, -15.0).person(-36.0) is False      # the canceller was not working: no trust
    assert laget(-55.0, -15.0, svar_start=2.0).person(-36.0) is False   # the reply began (or broke) 1.2 s ago
    assert laget(-55.0, -50.0).person(-36.0) is False      # nothing loud


def test_a_hole_in_what_was_sent_or_a_hold_on_the_device_starts_the_wait_over():
    aec = MjukAec(klocka=lambda: 0.0)
    ljud = (np.ones(FRAME * 2) * 1000).astype(np.int16).tobytes()
    aec.ref(ljud, RATE, 10.0)
    assert aec.svar_start == 10.0                 # a reply begins
    aec.ref(ljud, RATE, 10.02)
    assert aec.svar_start == 10.0                 # back to back: the same reply
    aec.ref(ljud, RATE, 10.2)
    assert aec.svar_start == 10.2                 # a hole of 0.16 s
    aec.spelstart(0.3, 11.0)
    assert aec.svar_start == 11.0                 # the device held the audio


def test_a_word_of_the_persons_in_the_window_does_not_hide_that_the_canceller_works():
    aec = MjukAec(klocka=lambda: 0.0)
    _matt(aec, 100, -25.0, -55.0)
    _matt(aec, 40, -12.0, -12.0)                  # the person's first word: loud in, loud out
    _matt(aec, 60, -25.0, -55.0)
    assert aec.erle(0.0, 2.0, 80, 150) >= 29.0    # as an energy sum this was 4 dB


def _sen_ref(folj, monkeypatch, sen_s=0.55):
    """The echo comes `sen_s` before the reference says it was played (the reference lags it by sen_s - 0.15 s). The
    reference is sent 1 s ahead of what the mic hears, as live."""
    monkeypatch.setenv("AEC_FOLJ", "1" if folj else "0")
    monkeypatch.setenv("SOFT_TAL_DB", "0")
    ref = _tal(14.0)
    s = int(sen_s * RATE)
    mic = np.concatenate([_eko(ref)[s:], np.zeros(s, np.int16)])
    klock = [0.0]
    aec = MjukAec(klocka=lambda: klock[0])
    steg, fore = 320, RATE
    for i in range(0, fore, steg):
        aec.ref(ref[i:i + steg].tobytes(), RATE, i / RATE)
    ut = []
    for i in range(0, len(ref) - fore - steg, steg):
        klock[0] = i / RATE
        aec.ref(ref[i + fore:i + fore + steg].tobytes(), RATE, (i + fore) / RATE)
        ut.append(aec.process(mic[i:i + steg].tobytes(), (i + steg) / RATE))
    ut = np.frombuffer(b"".join(ut), dtype=np.int16)
    a, b = 8 * RATE, 12 * RATE
    return _db(mic[a:b]) - _db(ut[a - FRAME * 3:b - FRAME * 3]), aec


def test_a_reference_that_lags_his_echo_is_found_and_moved(monkeypatch):
    """Kontoret 2026-10-14 08:07: after the playback stuttered, the reference sat 0.49 s after the echo for the rest of
    the session and the canceller took 0-3 dB. The tracker finds the echo and moves the reference so it leads again."""
    utan, _ = _sen_ref(False, monkeypatch)
    med, aec = _sen_ref(True, monkeypatch)
    assert utan < 6, f"the test does not reproduce the fault: {utan:.1f} dB without the tracker"
    assert med >= 12, f"with the tracker only {med:.1f} dB"
    assert 0.5 <= aec.skift_s <= 0.65, f"moved {aec.skift_s:.3f} s"


def test_the_tracker_leaves_a_reference_that_already_leads_alone(monkeypatch):
    monkeypatch.setenv("SOFT_TAL_DB", "0")
    _, aec = _sen_ref(True, monkeypatch, sen_s=0.0)
    assert aec.skift_s == 0.0


def test_an_earlier_move_that_now_leads_too_far_is_taken_back(monkeypatch):
    """The canceller is kept per device across sessions: a move made for one stuttering reply must not stay when the next
    session's echo is where it should be (it would then lead by the move plus its own 150 ms)."""
    monkeypatch.setenv("SOFT_TAL_DB", "0")
    monkeypatch.setenv("AEC_FOLJ", "1")
    aec = MjukAec(klocka=lambda: 0.0)
    aec.skift_s = 0.64
    ref = _tal(5.0)
    mic = np.concatenate([np.zeros(int(0.79 * RATE), np.int16), _eko(ref, tap_ms=(0, 40, 90))])[:len(ref)]   # echo 0.79 s after
    for i in range(0, len(ref) - FRAME, FRAME):
        aec._folj(mic[i:i + FRAME], ref[i:i + FRAME])
    assert abs(aec.skift_s) < 0.05, f"still moved {aec.skift_s:.3f} s"


def _skattningar(monkeypatch, lags):
    """Feed the tracker frames while eko_lag answers from `lags`, one per estimate (an estimate every second)."""
    import app.mjuk_aec as mj
    monkeypatch.setenv("AEC_FOLJ", "1")
    svar = list(lags)
    monkeypatch.setattr(mj, "eko_lag", lambda mic, ref: svar.pop(0) if svar else None)
    aec = MjukAec(klocka=lambda: 0.0)
    ram = np.ones(FRAME, np.int16)
    for _ in range(int((len(lags) + 1) * RATE / FRAME)):
        aec._folj(ram, ram)
    return aec


def test_one_estimate_that_says_the_reference_lags_does_not_move_it(monkeypatch):
    """One estimate is not trusted: a single one at -0.45 s amid normal echo leaves the reference where it is."""
    assert _skattningar(monkeypatch, [0.15, -0.45, 0.15, 0.15]).skift_s == 0.0


def test_two_estimates_that_disagree_do_not_move_it(monkeypatch):
    assert _skattningar(monkeypatch, [-0.45, -0.30, 0.15]).skift_s == 0.0


def test_two_estimates_in_a_row_that_agree_move_it(monkeypatch):
    aec = _skattningar(monkeypatch, [-0.45, -0.46])
    assert abs(aec.skift_s - (0.15 + 0.46)) < 1e-9, aec.skift_s
