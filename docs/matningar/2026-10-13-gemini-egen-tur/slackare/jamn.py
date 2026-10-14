"""If the echo's delay were the SAME in every reply (the reference moved so each reply's echo lands at one fixed lag),
does the leak at the start of the replies go away? One canceller for all replies in order, as live."""
import glob, numpy as np
from numpy.fft import rfft, irfft
from ns import db, las, ramar, R, S
from ns2 import ny, kor
filer = sorted(glob.glob(f"{S}/prov1700/*.wav") + glob.glob(f"{S}/prov1900/*.wav"), key=lambda p: p.split("/")[-1])
def lagg(mic, ref):
    nz = np.nonzero(ref)[0]; a = nz[0]; n = 65536
    m, r = mic[a:a + 3 * R].astype(np.float64), ref[a:a + 3 * R].astype(np.float64)
    X = rfft(m, n) * np.conj(rfft(r, n)); c = irfft(X / (np.abs(X) + 1e-9), n); return int(np.argmax(np.abs(c[:8000])))
MAL = int(0.140 * R)     # every reply's echo is put 140 ms after its reference
for namn, jamna in (("som inspelat (olika fördröjning)", False), ("samma fördröjning i varje svar", True)):
    ap = ny(None); n = tidigt = 0; hoga = []; laggar = []
    for p in filer:
        mic, ref = las(p); nz = np.nonzero(ref)[0]
        if len(nz) < R: continue
        l = lagg(mic, ref); laggar.append(round(l / 16))
        if jamna and 0 < l < MAL: ref = np.concatenate([np.zeros(MAL - l, np.int16), ref])[:len(mic)]     # the reference later = the lag grows to MAL... 
        if jamna and l > MAL: ref = np.concatenate([ref[l - MAL:], np.zeros(l - MAL, np.int16)])
        ren = kor(ap, mic, ref); nz = np.nonzero(ref)[0]; a, b = nz[0] / R, nz[-1] / R
        r2 = ramar(ren, a + 2.0, b)
        if len(r2) < 50 or np.sort(r2)[-13:].mean() > -38: continue
        r1 = ramar(ren, a, a + 2.0); n += 1; h = int((r1 > -40).sum()); tidigt += h > 0; hoga.append(h)
    print(f"{namn:34s}: {n} svar med bara eko. Läcker över -40 dBFS under första 2 s: {tidigt}. Läckta 20 ms-ramar per svar: {hoga} (summa {sum(hoga)})")
    if not jamna: print("   fördröjning per inspelning, ms:", laggar)
