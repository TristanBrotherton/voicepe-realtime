"""Does the leak at the start of a reply go away if the canceller is fed the silence BETWEEN replies too (live it is
given nothing between replies: for it the last reply's end is followed at once by the next one's start)?"""
import glob, numpy as np
from ns import db, las, ramar, R, S
from ns2 import ny, kor
filer = sorted(glob.glob(f"{S}/prov1700/*.wav") + glob.glob(f"{S}/prov1900/*.wav"), key=lambda p: p.split("/")[-1])
rng = np.random.default_rng(1)
def brus(n): return (rng.standard_normal(n) * 32768 * 10 ** (-52 / 20)).astype(np.int16)     # the room at rest, about -52 dBFS
for namn, mellan_s in (("som live (inget emellan)", 0), ("2 s tystnad emellan", 2), ("10 s tystnad emellan", 10)):
    ap = ny(None); n = tidigt = 0; toppar = []; hoga = []
    for p in filer:
        mic, ref = las(p); nz = np.nonzero(ref)[0]
        if len(nz) < R: continue
        if mellan_s: kor(ap, brus(mellan_s * R), np.zeros(mellan_s * R, np.int16))
        ren = kor(ap, mic, ref); a, b = nz[0] / R, nz[-1] / R
        r2 = ramar(ren, a + 2.0, b)
        if len(r2) < 50 or np.sort(r2)[-13:].mean() > -38: continue
        r1 = ramar(ren, a, a + 2.0); n += 1; h = int((r1 > -40).sum()); tidigt += h > 0; toppar.append(r1.max()); hoga.append(h)
    print(f"{namn:26s}: {n} svar. Läcker över -40 dBFS under första 2 s: {tidigt}. Starkaste ram per svar, median {np.median(toppar):.0f} dBFS. Läckta ramar per svar: {hoga}")
