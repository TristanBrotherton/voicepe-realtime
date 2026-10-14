"""Builds the clips and the plan for the silent Gemini probe (sond.py). Everything is 16 kHz mono int16."""
import sys, wave, io, json, base64, contextlib, subprocess, numpy as np
S = sys.argv[1]; sys.path.insert(0, f"{S}/rost"); sys.argv = sys.argv[:1]
with contextlib.redirect_stdout(io.StringIO()):
    from tat_ref import aec, db
R = 16000
def topp(x): return max(db(x[i:i + 4000]) for i in range(0, max(1, len(x) - 4000), 800))
def eko(fil, varm=True):
    # varm: the canceller has heard this reply once already (live it is one canceller for the whole evening);
    # kall: it starts from nothing, as on the very first reply after a restart
    w = wave.open(fil); x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2); mic, ref = x[:, 0].copy(), x[:, 1].copy()
    if not varm: return aec(mic, ref)
    return aec(np.concatenate([mic, mic]), np.concatenate([ref, ref]))[len(mic):]
def rost(namn): return np.frombuffer(base64.b64decode(open(f"{S}/kedja/rost_{namn}.b64").read()), dtype=np.int16)
def say(text, namn):
    subprocess.run(["say", "-v", "Alva", "-o", f"syn/{namn}.aiff", text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"syn/{namn}.aiff", f"syn/{namn}.wav"], check=True)
    w = wave.open(f"syn/{namn}.wav"); return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
def till(x, mal):                      # scale so the loudest 0.25 s is `mal` dBFS
    return np.clip(x.astype(np.float64) * 10 ** ((mal - topp(x)) / 20), -32768, 32767).astype(np.int16)
def tyst(x, ms=20):                    # trim leading/trailing digital silence
    nz = np.nonzero(np.abs(x) > 60)[0]; return x[max(0, nz[0] - R * ms // 1000):nz[-1] + R * ms // 1000]
def spara(namn, x): open(f"klipp/{namn}.pcm", "wb").write(x.astype(np.int16).tobytes()); return namn

FILER = (("prov1700", "150122"), ("prov1700", "150543"), ("prov1700", "150605"), ("prov1700", "150626"), ("prov1700", "151403"), ("prov1900", "165830"), ("prov1900", "165909"), ("prov1700", "150917"), ("prov1700", "150839"), ("prov1900", "170712"))
ekon = {k: eko(f"{S}/{d}/aec_20261013_{k}.wav") for d, k in FILER}
kalla_ekon = {k: eko(f"{S}/{d}/aec_20261013_{k}.wav", False) for d, k in FILER}
for k, e in ekon.items(): print("eko", k, f"{len(e)/R:.1f} s, nivå {db(e):.1f}, topp {topp(e[2*R:]):.1f}")
langa = [k for k, e in ekon.items() if len(e) >= 6.4 * R]
henrik = {n: rost(n) for n in ("vanlig1", "vanlig2", "vanlig3", "bestamd", "mening")}
for n, x in henrik.items(): print("henrik", n, f"{len(x)/R:.2f} s, topp {topp(x):.1f}")
NIVA = float(np.median([topp(henrik[n]) for n in ("vanlig1", "vanlig2", "vanlig3")]))   # a normal voice at the mic
print(f"syntetisk röst skalas till {NIVA:.1f} dBFS (Henriks vanliga röst)")
plan = []
# A: his own echo only, while the model speaks
for k, e in ekon.items(): plan.append(dict(id=f"A-varm-{k}", typ="over", pcm=spara(f"A-varm-{k}", e), rost_t=None, kalla="eko"))
for k, e in kalla_ekon.items():
    print("kallt eko", k, f"första 2 s {db(e[:2*R]):.1f}, topp {topp(e[:2*R]):.1f}")
    plan.append(dict(id=f"A-kall-{k}", typ="over", pcm=spara(f"A-kall-{k}", e), rost_t=None, kalla="eko"))
# B: a voice over the echo, 3 s into the clip
syn = {n: till(tyst(say(t, n)), NIVA) for n, t in (("hjalp", "Hjälp!"), ("tand", "Tänd!"), ("nej", "Nej."), ("klockan", "Vad är klockan?"), ("fel", "Nej, det var inte det jag menade."))}
B = [("vanlig1", "henrik"), ("vanlig2", "henrik"), ("vanlig3", "henrik"), ("bestamd", "henrik"), ("mening", "henrik"), ("hjalp", "syntetisk"), ("tand", "syntetisk"), ("nej", "syntetisk"), ("klockan", "syntetisk"), ("fel", "syntetisk")]
def over(e, r, t=3.0):
    y = np.concatenate([e, np.zeros(max(0, int(t * R) + len(r) - len(e)), np.int16)]).astype(np.int32); a = int(t * R); y[a:a + len(r)] += r
    return np.clip(y, -32768, 32767).astype(np.int16)
for i, (n, kalla) in enumerate(B):
    r = henrik[n] if kalla == "henrik" else syn[n]; k = langa[i % len(langa)]
    plan.append(dict(id=f"B-{n}", typ="over", pcm=spara(f"B-{n}", over(ekon[k], r)), rost_t=3.0, rost_s=len(r) / R, kalla=kalla, eko=k))
# D: short agreement over the echo
med = {n: till(tyst(say(t, f"med_{n}")), NIVA) for n, t in (("mm", "Mm."), ("ja", "Ja."), ("okej", "Okej."), ("precis", "Precis."), ("aha", "Aha."))}
for i, n in enumerate(list(med) * 2):
    k = langa[(i + 3) % len(langa)]
    plan.append(dict(id=f"D-{n}-{i//5}", typ="over", pcm=spara(f"D-{n}-{i//5}", over(ekon[k], med[n])), rost_t=3.0, rost_s=len(med[n]) / R, kalla="syntetisk", eko=k))
# C: one sentence with a 1.2 s pause in it, nobody else speaking
def paus_i(x):                         # (start, end) of the longest quiet stretch inside the clip
    r = np.array([db(x[i:i + 160]) for i in range(0, len(x) - 160, 160)]); q = r < -50; bast = (0, 0); a = None
    for i, v in enumerate(list(q) + [False]):
        if v and a is None: a = i
        if not v and a is not None:
            if i - a > bast[1] - bast[0] and a > 10 and i < len(r) - 10: bast = (a, i)
            a = None
    return bast[0] / 100, bast[1] / 100
m = henrik["mening"]; r = np.array([db(m[i:i + 480]) for i in range(0, len(m) - 480, 160)]); n = len(r)
snitt = (int(n * 0.3) + int(np.argmin(r[int(n * 0.3):int(n * 0.7)]))) * 160 + 240
hp = np.concatenate([m[:snitt], np.zeros(int(1.2 * R), np.int16), m[snitt:]])
print(f"Henriks mening delad vid {snitt/R:.2f} s av {len(m)/R:.2f} s")
C = [("henrik1", hp, "henrik"), ("henrik2", hp, "henrik")]
for i, t in enumerate(("Vad blir det för väder [[slnc 1200]] i morgon i Stockholm?", "Kan du berätta [[slnc 1200]] vad klockan är just nu?", "Jag undrar hur långt det är [[slnc 1200]] mellan jorden och månen.",
                       "Vilken är [[slnc 1200]] den största staden i Norge?", "Hur många dagar [[slnc 1200]] är det kvar till jul?", "Kan du förklara [[slnc 1200]] varför himlen är blå?",
                       "Vad heter [[slnc 1200]] huvudstaden i Finland?", "Berätta något roligt [[slnc 1200]] om björnar.")):
    C.append((f"syn{i+1}", till(tyst(say(t, f"paus{i+1}")), NIVA), "syntetisk"))
for namn, x, kalla in C:
    a, b = paus_i(x)
    print(f"C-{namn}: {len(x)/R:.2f} s, paus {a:.2f}-{b:.2f} ({b-a:.2f} s)")
    for aad in ("forval", "idag", "talig"):
        plan.append(dict(id=f"C-{namn}-{aad}", typ="fraga", pcm=spara(f"C-{namn}", x), paus=[a, b], slut=len(x) / R, kalla=kalla, aad=aad))
json.dump(plan, open("plan.json", "w"), indent=1, ensure_ascii=False)
print(len(plan), "försök;", sum(1 for p in plan if p["typ"] == "over"), "över svar,", sum(1 for p in plan if p["typ"] == "fraga"), "frågor")
