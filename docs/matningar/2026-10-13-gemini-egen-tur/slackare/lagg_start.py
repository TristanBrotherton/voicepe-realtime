"""Is the echo's delay against the reference different at the start of a reply? GCC-PHAT in 0.4 s windows."""
import numpy as np
from numpy.fft import rfft, irfft
from ns import db, las, R, S
def lagg(m, r):
    n = 32768; X = rfft(m.astype(np.float64), n) * np.conj(rfft(r.astype(np.float64), n)); c = irfft(X / (np.abs(X) + 1e-9), n)
    k = int(np.argmax(np.abs(c[:8000]))); return k / 16, np.abs(c[k]) / np.median(np.abs(c[:8000]))
for d, k in (("prov1700", "150626"), ("prov1700", "151403"), ("prov1700", "150543"), ("prov1700", "150122"), ("prov1700", "150605"), ("prov1900", "165909")):
    mic, ref = las(f"{S}/{d}/aec_20261013_{k}.wav"); nz = np.nonzero(ref)[0]; a = nz[0]
    ut = []
    for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 4.0):
        i = a + int(t * R); m, r = mic[i:i + 6400], ref[i:i + 6400]
        if len(m) < 6400 or np.sqrt(np.mean(r.astype(float) ** 2)) < 200: ut.append(f"{t}:tyst"); continue
        l, s = lagg(m, r); ut.append(f"{t}:{l:.0f}ms({s:.0f})")
    print(k, f"ref börjar {a/R:.2f} s in; mic före ref {db(mic[max(0,a-3200):a]):.0f} dBFS, mic 0-0,3 s {db(mic[a:a+4800]):.0f}, ref 0-0,3 s {db(ref[a:a+4800]):.0f} |", " ".join(ut))
