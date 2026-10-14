"""Same as ns.py but ONE canceller for the whole evening (as live), all echo-only replies in the order they were
recorded, and where the loud leftovers are."""
import sys, glob, wave, numpy as np
from aec_audio_processing import AudioProcessor
from ns import db, las, ramar, R, S
filer = sorted(glob.glob(f"{S}/prov1700/*.wav") + glob.glob(f"{S}/prov1900/*.wav"), key=lambda p: p.split("/")[-1])
def ny(ns):
    ap = AudioProcessor(enable_aec=True, enable_ns=ns is not None, ns_level=ns or 0, enable_agc=False, enable_vad=False)
    ap.set_stream_format(R, 1); ap.set_reverse_stream_format(R, 1); ap.set_stream_delay(0); return ap
def kor(ap, mic, ref):
    out = np.zeros_like(mic)
    for i in range(0, len(mic) - 160, 160):
        ap.process_reverse_stream(ref[i:i + 160].tobytes()); out[i:i + 160] = np.frombuffer(ap.process_stream(mic[i:i + 160].tobytes()), dtype=np.int16)
    return out
print(len(filer), "inspelningar i tidsordning")
for namn, ns in (("ns av", None), ("ns 3", 3)):
    ap = ny(ns); ap0 = ny(None); alla = []; hoga = []; n_eko = 0
    for p in filer:
        mic, ref = las(p); nz = np.nonzero(ref)[0]
        if len(nz) < R: continue
        ren = kor(ap, mic, ref); ren0 = kor(ap0, mic, ref) if ns is not None else ren
        a, b = nz[0] / R, nz[-1] / R
        r0 = ramar(ren0, a + 1.0, b)
        if len(r0) < 50 or np.sort(r0)[-13:].mean() > -38: continue      # a voice (or a slip) in it: not an echo-only reply
        n_eko += 1; r = ramar(ren, a + 0.0, b); alla.append(r)
        for i in np.nonzero(r > -45)[0]: hoga.append((p.split("_")[-1][:6], round(i * 0.02, 2), round(float(r[i]), 1)))
    r = np.concatenate(alla)
    print(f"{namn}: {n_eko} svar med bara eko, {len(r)*0.02:.0f} s. Kvar: median {np.median(r):.1f}, P95 {np.percentile(r,95):.1f}, P99 {np.percentile(r,99):.1f}, max {r.max():.1f} dBFS. Ramar över -45: {int((r>-45).sum())}, över -40: {int((r>-40).sum())}")
    print("   höga ramar (fil, sekund in i svaret, nivå):", hoga[:30])
