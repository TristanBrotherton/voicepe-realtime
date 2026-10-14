"""Offline: what reaches the engine with the noise suppressor on and the 'restspärr' (silence while the canceller has
not locked on this reply: its first START_S, and START_S after every hole > 30 ms in what is sent to the speaker)."""
import glob, sys, numpy as np
from ns import db, las, R, S
from ns2 import ny, kor
START_S = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
filer = sorted(glob.glob(f"{S}/prov1700/*.wav") + glob.glob(f"{S}/prov1900/*.wav"), key=lambda p: p.split("/")[-1])
def sparr(ref):
    """True per 10 ms frame where the gate is shut."""
    n = len(ref) // 160; nz = np.nonzero(ref)[0]; shut = np.zeros(n, bool); svar_start = nz[0] / R; tyst = 0
    for i in range(n):
        f = ref[i * 160:(i + 1) * 160]
        if not f.any(): tyst += 1
        else:
            if tyst >= 3 and i * 0.01 > nz[0] / R: svar_start = i * 0.01     # a hole > 30 ms just ended
            tyst = 0
        shut[i] = (i + 1) * 0.01 - svar_start < START_S
    return shut
ap = ny(3); ap0 = ny(None); n = 0; kvar = []; rader = []
for p in filer:
    mic, ref = las(p); nz = np.nonzero(ref)[0]
    if len(nz) < R: continue
    ren = kor(ap, mic, ref); ren0 = kor(ap0, mic, ref); a, b = nz[0], nz[-1]; shut = sparr(ref)
    niva = np.array([db(ren[i * 160:(i + 1) * 160]) for i in range(len(ref) // 160)]); niva0 = np.array([db(ren0[i * 160:(i + 1) * 160]) for i in range(len(ref) // 160)])
    i0, i1 = a // 160, b // 160; sent = niva0[i0 + 200:i1]
    rost = len(sent) < 50 or np.sort(sent)[-25:].mean() > -38       # a voice in it after the first 2 s
    ute = np.where(shut, -99.0, niva)[i0:i1]
    rader.append((p.split("_")[-1][:6], "RÖST" if rost else "eko", round(float(ute.max()), 1), int((ute > -45).sum()), int((ute > -40).sum()), round(float(shut[i0:i1].mean()) * 100)))
    if not rost: n += 1; kvar.append(ute)
k = np.concatenate(kvar)
print(f"spärr {START_S} s + ns 3: {n} svar med bara eko, {len(k)/100:.0f} s. Till motorn: max {k.max():.1f} dBFS, 10 ms-ramar över -45: {int((k>-45).sum())}, över -40: {int((k>-40).sum())}")
for r in rader: print("   ", r[0], r[1], f"max ut {r[2]} dBFS, ramar >-45: {r[3]}, >-40: {r[4]}, spärren stängd {r[5]} % av svaret")
