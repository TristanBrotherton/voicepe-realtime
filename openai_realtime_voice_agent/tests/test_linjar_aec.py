"""The long linear canceller with a double-talk guard (SOFT_AEC_MOTOR=linjar, raawr US-060)."""
import numpy as np

from app.linjar_aec import LinjarAec, N

RATE = 16000


def _rost(n, seed, hz=4.0):
    """Speech-like: band-limited noise in syllables (`hz` on/off)."""
    r = np.random.default_rng(seed)
    x = np.convolve(r.standard_normal(n), np.hanning(9), "same")
    return x * (0.6 + 0.4 * np.sign(np.sin(2 * np.pi * hz * np.arange(n) / RATE + seed)))


def _rum(ref, seed=7):
    """His echo: 110 ms late, a 60 ms decaying tail, 6 dB down."""
    r = np.random.default_rng(seed)
    h = np.zeros(int(0.17 * RATE))
    svans = r.standard_normal(int(0.06 * RATE)) * np.exp(-np.arange(int(0.06 * RATE)) / 200)
    h[int(0.11 * RATE):] = svans / np.sqrt(np.sum(svans ** 2)) * 0.5
    return np.convolve(ref, h)[:len(ref)]


def _db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)


def _kor(mic, ref, **kw):
    a, ut = LinjarAec(**kw), []
    for i in range(0, len(mic) - N + 1, N):
        a.process_reverse_stream(np.clip(ref[i:i + N], -32768, 32767).astype(np.int16).tobytes())
        ut.append(np.frombuffer(a.process_stream(np.clip(mic[i:i + N], -32768, 32767).astype(np.int16).tobytes()), np.int16))
    o = np.concatenate(ut).astype(float)
    return np.concatenate([o[N:], np.zeros(N)])   # the suppressor's 10 ms taken back out


def _par(s=10):
    ref = _rost(s * RATE, 1) * 3000
    return ref, _rum(ref) + np.random.default_rng(3).standard_normal(s * RATE) * 10


def test_tar_ekot():
    ref, mic = _par()
    ut = _kor(mic, ref)
    s = slice(4 * RATE, 10 * RATE)
    assert _db(mic[s]) - _db(ut[s]) > 15


def test_vakten_bromsar_inte_nar_bara_ekot_hors():
    # Only his echo: the guard must learn as fast as no guard. With the leak estimate floored at 0.005 it throttled the
    # step while the filter was still improving (28 dB against 37 at 4-6 s here; on the saved Kontor pairs 9 dB
    # against 17, raawr US-060 trim/jamfor_1000.txt).
    ref, mic = _par()
    s = slice(4 * RATE, 6 * RATE)
    med, utan = (_db(mic[s]) - _db(_kor(mic, ref, golv_db=0.0, vakt=v)[s]) for v in (True, False))
    assert med > utan - 3, (med, utan)


def _over_ekot(vakt, golv_db, gg=1.0):
    """Someone speaks for 2 s over his echo, `gg` times as loud. Returns (dB of echo taken in the 0.5 s after, dB the
    voice lost)."""
    ref, eko = _par()
    rost = np.zeros(len(eko))
    v = _rost(2 * RATE, 5, 2.7)       # syllables at another pace than his: two voices do not share an envelope
    rost[5 * RATE:7 * RATE] = v * np.sqrt(np.mean(eko ** 2)) / np.sqrt(np.mean(v ** 2)) * gg
    ut = _kor(eko + rost, ref, vakt=vakt, golv_db=golv_db)
    efter = slice(7 * RATE, int(7.5 * RATE))
    return _db(eko[efter]) - _db(ut[efter]), _db(ut[5 * RATE:7 * RATE]) - _db(rost[5 * RATE:7 * RATE])


def test_en_rost_over_ekot_slar_inte_ut_filtret():
    # A voice twice as loud as his echo: the guard holds the filter still, so it cancels right after (15 dB measured);
    # without it the filter adapts on the voice and lets his echo through (2 dB). The suppressor is off to see the filter.
    med, utan = _over_ekot(True, 0.0, 2.0)[0], _over_ekot(False, 0.0, 2.0)[0]
    assert med > 10, med
    assert med > utan + 8, (med, utan)


def test_rosten_over_ekot_blir_kvar():
    # A voice as loud as his echo comes through, a few dB down. That the floor saves WORDS shows only on real audio
    # (AEC3 took them, this did not: app/linjar_aec.py); a synthetic voice loses ~4 dB at any floor.
    assert _over_ekot(True, -24.0)[1] > -8


def test_mjuk_aec_valjer_linjar(monkeypatch):
    monkeypatch.setenv("SOFT_AEC", "1")
    monkeypatch.setenv("SOFT_AEC_MOTOR", "linjar")
    from app.mjuk_aec import MjukAec
    assert isinstance(MjukAec().ap, LinjarAec)


def test_matt_parar_utgangen_med_ramen_fore(monkeypatch):
    # The output is the previous frame: where his echo starts, the first loud frame in must not be measured against
    # the quiet frame out before it (that read as 60 dB taken off).
    monkeypatch.setenv("SOFT_AEC", "1")
    monkeypatch.setenv("SOFT_AEC_MOTOR", "linjar")
    from app.mjuk_aec import MjukAec
    a = MjukAec(klocka=lambda: 0.0)
    a.ref_slut = 10.0                                    # the speaker is playing: the canceller runs
    tyst, hogt = np.zeros(N, np.int16), np.full(N, 8000, np.int16)
    a.process(tyst.tobytes(), t=0.01)
    a.process(hogt.tobytes(), t=0.02)
    _, in_db, ut_db, _ = a.matt[-1]
    assert in_db < -80 and ut_db < -80                   # the quiet frame, in and out


def test_ekot_tas_efter_en_lika_stark_rost():
    # B on #87: the double-talk tests guarded only downwards. A voice as loud as his echo: right after it the filter
    # must still take his echo (16.4 dB with the leak floor 0.3; 13.9 at 0.5, 10.8 at 1.0, trim/jamfor_1000.txt).
    taget = _over_ekot(True, 0.0, 1.0)[0]
    assert taget > 14, taget
