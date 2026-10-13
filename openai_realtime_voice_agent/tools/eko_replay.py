"""US-060: replay AEC_SPARA recordings through the full-duplex decision chain, offline. No speaker, no cloud engine.

    python tools/eko_replay.py --manifest replay.json            # on core: it uses the local speech-to-text (bana 0)

The manifest is a list of {"fil": path, "person": null | {"t": seconds, "ord": "vänta"}}. Each file is a stereo wav as
AEC_SPARA writes it: left = the raw mic, right = what the agent sent to the speaker, on the agent's own timeline.

What runs is the agent's own code, in the agent's order:
  mic -> MjukAec (AEC3, AGC)  ->  LocalTurns (Silero, the engine's turn detector)
      -> speech that starts while he speaks or just after is a CANDIDATE: >= 0.7 s of it is written out by the local
         speech-to-text and judged by eko_vakt.dom(); too quiet (eko_vakt.niva_dbfs) is ignored
      -> ignored: dropped, the engine never gets it.  A person: his reply is cut (a barge-in) and the turn goes on to
         the engine.
      -> and before any of that: louder than his own echo over a working canceller (MjukAec.person) cuts him at once,
         on the level alone ("person (nivå)").
What is NOT here: the cloud engine, the device, bana 0's own answers, and his live transcript -- his words are taken
from the speech-to-text of the right channel (the whole file), which is a little kinder than the live transcript that
trails his voice. The transcript-lag guard (eko_vakt.utskrift_efter) is therefore left out: it can only ignore more.

Per file: every candidate with its verdict, and every turn that would have reached the engine. The summary counts
  self-interruptions  = a "person" verdict in a file where no person spoke, or before the person spoke
  his words to engine = turns sent on whose text is mostly his own words (eko_vakt.liknar_svar, >= 1 word)
  the person's word   = found or not, and how long after it began the cut would come (audio needed + speech-to-text)
"""
import argparse
import asyncio
import json
import os
import sys
import time
import wave

import numpy as np

os.environ.setdefault("SOFT_AEC", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

STEG = 320                     # 20 ms at 16 kHz, the device's frame
FAS_SVARAR_S = 0.35            # the phase stays "replying" about this long after the last audio (pipecat's bot-stopped)
EKO_MIN_S = float(os.environ.get("REPLAY_EKO_MIN_S", "0.5"))   # ResilientGeminiLiveService.EKO_MIN_S, speech after the start
MIN_EFTER_START = int(os.environ.get("REPLAY_EFTER_START", "1"))  # 0 = as before 2026-10-13: the pre-roll counted too
PREROLL_S = 1.0                # LocalTurnsMixin.PREROLL_S


def las(path):
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 2 and w.getsampwidth() == 2, f"{path}: 16 kHz stereo PCM16"
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)
    return a[:, 0].copy(), a[:, 1].copy()


def topp_dbfs(pcm: bytes) -> float:
    from app.avbrott import dbfs
    steg = [dbfs(pcm[i:i + 8000]) for i in range(0, max(len(pcm) - 8000, 1), 8000)]
    return max(steg) if steg else -99.0


async def kor_fil(post, aec, turns, svar, stt, t_bas, utan_aec=False):
    from app import eko_vakt
    mic, ref = las(post["fil"])
    person = post.get("person")
    hans_nu, _ = await stt(ref.tobytes())
    svar.mata(hans_nu or "")
    hans = svar.text()
    ut = {"fil": os.path.basename(post["fil"]), "s": round(len(mic) / 16000, 1), "hans": hans_nu, "kandidater": [], "turer": [],
          "person": person}
    preroll = bytearray()
    kand = None            # {"t0", "pcm", "slut"}
    tur = None             # {"t0", "pcm", "avbrott"}
    ref_sist = -1e9
    niva_sist = -1e9
    turns.reset()
    nz = np.nonzero(ref)[0]
    stampel_vid = (nz[0] / 16000 + post["spelstart_ms"] / 1000 + 0.03) if post.get("spelstart_ms") and len(nz) else None
    for i in range(0, len(mic) - STEG + 1, STEG):
        t = t_bas + i / 16000                       # the agent's clock when this frame begins
        if stampel_vid is not None and i / 16000 >= stampel_vid:
            aec.spelstart(post["spelstart_ms"] / 1000, t)      # the device's play_stamp, as it came live
            stampel_vid = None
        r = ref[i:i + STEG]
        if np.any(r):
            aec.ref(r.tobytes(), 16000, t)
            ref_sist = t + STEG / 16000
        ram = mic[i:i + STEG].tobytes()
        ren = ram if utan_aec else aec.process(ram, t + STEG / 16000)
        if not ren:
            continue
        nu = t + STEG / 16000 - t_bas               # file time
        event = turns.feed(ren)
        if (tur is None and not utan_aec and (t + STEG / 16000 - ref_sist) <= FAS_SVARAR_S and nu - niva_sist > 2.0
                and aec.person(eko_vakt.niva_dbfs(), eko_vakt.ERLE_TILLIT_DB)):
            # Louder than his own echo over a working canceller: cut on the level alone (MjukAec.person), no text.
            niva_sist = nu
            pcm0 = bytes(kand["pcm"]) if kand else bytes(preroll)
            ut["kandidater"].append({"t": round(nu - 0.2, 2), "tal_fran": round(nu - 0.2, 2), "dom_vid": round(nu, 2), "stt_s": 0.0,
                                     "hort": None, "ok": False, "under_tal": True, "niva": round(topp_dbfs(ren * 13), 1),
                                     "dom": "person (nivå)", "erle_fore": None})
            tur = {"t0": nu - len(pcm0) / 32000, "pcm": bytearray(pcm0) + bytearray(ren), "avbrott": True,
                   "niva_slut": nu + turns.silence_s + 0.6}
            kand, preroll = None, bytearray()
            continue
        if tur is not None:                         # a turn on its way to the engine: collect to its end
            tur["pcm"] += ren
            if event == "start" or turns._speaking:
                tur.pop("niva_slut", None)          # the turn detector has the speech: its own end comes
            elif tur.get("niva_slut") and nu >= tur["niva_slut"]:
                event = "end"
            if event == "end":
                text, ok = await stt(bytes(tur["pcm"]))
                ut["turer"].append({"t": round(tur["t0"], 2), "s": round(len(tur["pcm"]) / 32000, 2), "text": text,
                                    "efter_avbrott": tur["avbrott"],
                                    "hans_ord": bool(eko_vakt.liknar_svar(text, hans)) or
                                    (bool(eko_vakt.ord_(text)) and eko_vakt.ar_eko(text, hans))})
                tur = None
            continue
        if kand is None:
            vakt = (t + STEG / 16000 - ref_sist) <= FAS_SVARAR_S + eko_vakt.svans_s()
            if event == "start" and vakt:
                kand = {"t0": nu - len(preroll) / 32000, "pcm": bytearray(preroll) + bytearray(ren), "slut": False,
                        "fore": len(preroll)}
                preroll = bytearray()
            elif event == "start":                  # speech with no reply near it: an ordinary turn
                tur = {"t0": nu - len(preroll) / 32000, "pcm": bytearray(preroll) + bytearray(ren), "avbrott": False}
                preroll = bytearray()
            else:
                preroll = (preroll + ren)[-int(PREROLL_S * 32000):]
            continue
        kand["pcm"] += ren
        if event == "end":
            kand["slut"] = True
        if not (kand["slut"] or len(kand["pcm"]) - kand["fore"] * MIN_EFTER_START >= EKO_MIN_S * 32000):
            continue
        pcm = bytes(kand["pcm"])
        t_stt = time.monotonic()
        hort, ok = await stt(pcm)
        stt_s = time.monotonic() - t_stt
        sedan = (t + STEG / 16000) - ref_sist
        under = eko_vakt.under_tal(sedan, len(pcm) / 32000.0, sedan <= FAS_SVARAR_S)
        erle_t = None if utan_aec else aec.erle(t_bas + kand["t0"] - eko_vakt.ERLE_TILLIT_S, t_bas + kand["t0"], eko_vakt.ERLE_TILLIT_RAMAR, eko_vakt.ERLE_TILLIT_SISTA)
        eko = eko_vakt.dom(hort, ok, hans, under, erle_t if under and os.environ.get("EKO_TILLIT_TEXT", "0") == "1" else None)
        niva = topp_dbfs(pcm)
        skal = "ignorerad" if eko else "person"
        if not eko and niva < eko_vakt.niva_dbfs():
            eko, skal = True, "ignorerad (för tyst)"
        erle = erle_t
        ut["kandidater"].append({"t": round(kand["t0"], 2), "tal_fran": round(kand["t0"] + kand["fore"] / 32000, 2), "dom_vid": round(nu, 2), "stt_s": round(stt_s, 2), "hort": hort,
                                 "ok": ok, "under_tal": under, "niva": round(niva, 1), "dom": skal,
                                 "erle_fore": None if erle is None else round(erle, 1)})
        if eko:
            turns.reset()
        else:
            tur = {"t0": kand["t0"], "pcm": bytearray(pcm), "avbrott": True}
            if kand["slut"]:
                tur["pcm"] += b"\0" * 320
        kand = None
    if tur is not None:                             # the file ended inside a turn: it still goes to the engine
        text, ok = await stt(bytes(tur["pcm"]))
        ut["turer"].append({"t": round(tur["t0"], 2), "s": round(len(tur["pcm"]) / 32000, 2), "text": text,
                            "efter_avbrott": tur["avbrott"], "avklippt": True,
                            "hans_ord": bool(eko_vakt.liknar_svar(text, hans)) or
                            (bool(eko_vakt.ord_(text)) and eko_vakt.ar_eko(text, hans))})
    svar.klar()
    return ut, len(mic) / 16000


def summera(resultat):
    sjalv, hans_till_motor, person_rader = 0, 0, []
    for r in resultat:
        p = r["person"]
        avbrott = [k for k in r["kandidater"] if k["dom"].startswith("person")]
        if p is None:
            sjalv += len(avbrott)
            hans_till_motor += sum(1 for t in r["turer"] if t["hans_ord"])
            continue
        fore = [k for k in avbrott if k["dom_vid"] < p["t"]]
        sjalv += len(fore)
        traff = next((k for k in avbrott if k["dom_vid"] >= p["t"] and k["t"] <= p["t"] + 1.5), None)
        person_rader.append({"fil": r["fil"], "ord": p["ord"], "hittad": traff is not None,
                             "hort": traff["hort"] if traff else None,
                             "s_till_klipp": round(traff["dom_vid"] + traff["stt_s"] - p["t"], 2) if traff else None})
        hans_till_motor += sum(1 for t in r["turer"] if t["hans_ord"] and t["t"] + t["s"] < p["t"])
    return {"filer": len(resultat), "sjalvavbrott": sjalv, "turer_med_hans_ord_till_motorn": hans_till_motor,
            "person": person_rader}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--ut", help="write the full result here as json")
    ap.add_argument("--utan-aec", action="store_true", help="the raw mic straight to the turn detector (SOFT_AEC=0)")
    a = ap.parse_args()
    from app import bana0, eko_vakt
    from app.mjuk_aec import MjukAec
    from app.providers.local_turns import LocalTurns, PRE_END_MS
    host, port = os.environ.get("BANA0_STT", "127.0.0.1:10300").rsplit(":", 1)
    timeout = float(os.environ.get("REPLAY_STT_TIMEOUT", "4.0"))

    async def stt(pcm):
        return await bana0.transkribera_tydligt(pcm, host, int(port), timeout)

    aec = MjukAec.skapa()
    assert a.utan_aec or (aec is not None and aec.aec_pa), "the echo canceller did not start (aec-audio-processing missing?)"
    turns = LocalTurns.create(int(os.environ.get("GEMINI_TURN_SILENCE_MS", "800")), pre_ms=PRE_END_MS)
    assert turns is not None, "no local turn detection"
    svar = eko_vakt.Svar()
    resultat, t_bas = [], 1000.0
    for post in json.load(open(a.manifest)):
        r, s = await kor_fil(post, aec, turns, svar, stt, t_bas, a.utan_aec)
        t_bas += s + 10.0                           # the recordings are apart in time: the canceller sees the gap
        resultat.append(r)
        print(f"== {r['fil']} ({r['s']} s) han: {r['hans']!r}" + (f" | person: {r['person']}" if r["person"] else ""))
        for k in r["kandidater"]:
            print(f"   kandidat, tal från {k['tal_fran']:5.2f} s, dom vid {k['dom_vid']:5.2f} s (+{k['stt_s']:.2f} s stt), {k['niva']:6.1f} dBFS, "
                  f"ERLE före {k['erle_fore']} dB, {'medan han talar' if k['under_tal'] else 'strax efter'}: {k['hort']!r} -> {k['dom']}")
        for t in r["turer"]:
            print(f"   TILL MOTORN {t['t']:5.2f} s ({t['s']} s){' efter avbrott' if t['efter_avbrott'] else ''}: {t['text']!r}"
                  f"{'  <- HANS ORD' if t['hans_ord'] else ''}")
    s = summera(resultat)
    print("SUMMA", json.dumps(s, ensure_ascii=False))
    if a.ut:
        json.dump({"summa": s, "filer": resultat}, open(a.ut, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
