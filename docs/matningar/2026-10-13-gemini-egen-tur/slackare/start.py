"""Where in a reply is his own voice left after the canceller? One canceller for all recordings in order (what live did:
the saved frames ARE the frames the live canceller was given). Echo-only replies after 15:00 (latest firmware)."""
import glob, numpy as np
from ns import db, las, ramar, R, S
from ns2 import ny, kor
filer = sorted(glob.glob(f"{S}/prov1700/*.wav") + glob.glob(f"{S}/prov1900/*.wav"), key=lambda p: p.split("/")[-1])
for namn, ns in (("ns av", None), ("ns 3", 3)):
    ap = ny(ns); ap0 = ny(None); n = tidigt = sent = 0; rader = []
    for p in filer:
        mic, ref = las(p); nz = np.nonzero(ref)[0]
        if len(nz) < R: continue
        ren = kor(ap, mic, ref); ren0 = kor(ap0, mic, ref) if ns is not None else ren
        a, b = nz[0] / R, nz[-1] / R
        r0 = ramar(ren0, a + 2.0, b)
        if len(r0) < 50 or np.sort(r0)[-13:].mean() > -38: continue     # a voice in it after the first 2 s
        n += 1; r1 = ramar(ren, a, a + 2.0); r2 = ramar(ren, a + 2.0, b)
        h1 = int((r1 > -40).sum()); h2 = int((r2 > -40).sum()); tidigt += h1 > 0; sent += h2 > 0
        rader.append(f"{p.split('_')[-1][:6]}: första 2 s max {r1.max():.0f} ({h1} ramar över -40) | resten max {r2.max():.0f} ({h2})")
    print(f"{namn}: {n} svar med bara eko. Svar med något över -40 dBFS under de första 2 s: {tidigt}. Efter 2 s: {sent}.")
    for x in rader: print("   ", x)
