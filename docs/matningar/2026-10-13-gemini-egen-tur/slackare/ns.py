"""Offline: how much of his own voice is left after the canceller with WebRTC's noise suppressor on (ns_level 0-3),
and what it does to Henrik's voice. Echo-only recordings and recordings with Henrik's normal voice over the reply."""
import wave, numpy as np
from aec_audio_processing import AudioProcessor
S = "/private/tmp/claude-501/-Users-collin-dev-Projects-raawr/5e766051-3e3f-4080-ba20-7a0e6340fe0e/scratchpad"
R = 16000
def db(x): return 20 * np.log10(max(np.sqrt(np.mean(np.square(x.astype(np.float64)))), 1e-3) / 32768)
def las(p):
    w = wave.open(p); x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2); return x[:, 0].copy(), x[:, 1].copy()
def kor(mic, ref, ns):
    ap = AudioProcessor(enable_aec=True, enable_ns=ns is not None, ns_level=ns or 0, enable_agc=False, enable_vad=False)
    ap.set_stream_format(R, 1); ap.set_reverse_stream_format(R, 1); ap.set_stream_delay(0); out = np.zeros_like(mic)
    for i in range(0, len(mic) - 160, 160):
        ap.process_reverse_stream(ref[i:i + 160].tobytes()); out[i:i + 160] = np.frombuffer(ap.process_stream(mic[i:i + 160].tobytes()), dtype=np.int16)
    return out
def ramar(x, a, b): return np.array([db(x[i:i + 320]) for i in range(int(a * R), min(int(b * R), len(x) - 320), 320)])
EKO = [("prov1700", k) for k in ("150122", "150543", "150605", "150626", "151403", "150917", "150839")] + [("prov1900", k) for k in ("165830", "165909", "170712")]
ROST = [("142137", 3.3, 5.0), ("142126", 4.4, 5.7), ("142000", 2.8, 3.8), ("141849", 3.3, 4.2)]   # Henrik over the reply (prov1615)
print("läge      | eko kvar: median, P95, max av 20 ms-ramar efter 1 s (dBFS)  | ramar över -45 / -40  | Henriks röst: topp 0,25 s per klipp")
for namn, ns in (("ns av", None), ("ns 0", 0), ("ns 1", 1), ("ns 2", 2), ("ns 3", 3)):
    alla = []
    for d, k in EKO:
        mic, ref = las(f"{S}/{d}/aec_20261013_{k}.wav"); ren = kor(mic, ref, ns); nz = np.nonzero(ref)[0]
        alla.append(ramar(ren, nz[0] / R + 1.0, nz[-1] / R))
    r = np.concatenate(alla); rost = []
    for k, a, b in ROST:
        mic, ref = las(f"{S}/prov1615/aec_20261013_{k}.wav"); ren = kor(mic, ref, ns)[int(a * R):int(b * R)]
        rost.append(max(db(ren[i:i + 4000]) for i in range(0, len(ren) - 4000, 800)))
    print(f"{namn:9s} | {np.median(r):6.1f} {np.percentile(r, 95):6.1f} {r.max():6.1f}   ({len(r)} ramar)            | {int((r > -45).sum()):4d} / {int((r > -40).sum()):4d}        | " + " ".join(f"{x:6.1f}" for x in rost))
