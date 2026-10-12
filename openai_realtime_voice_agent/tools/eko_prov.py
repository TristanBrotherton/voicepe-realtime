"""US-060 AC-1 (layer 1 alone) and the echo half of AC-5: what does the mic hear while the speaker talks?

Run on core, ONLY when told (it makes the speaker talk) and with the agent started with EKO_PROV=1 (ENABLE_RECORDING is not needed):

    say -v Alva -o /tmp/eko.aiff --data-format=LEI16@24000 -f text.txt     # on the Mac; any 20 s of Swedish
    python tools/eko_prov.py --wav eko.wav --text-fil text.txt --rum kontoret --gange 3

The agent plays the clip with the device's mic streaming (POST /eko-prov on 127.0.0.1, loopback), captures every mic
frame before any guard, and this script reports per run: the loudest 0.25 s while the clip played (AC-1 wants <= -45 dBFS,
25 dB under speech at -20), what bana 0 and Gemini write out of the mic audio, how many of the text's 5+ letter words
Gemini heard (AC-1 wants 0), and whether the echo net (AC-5) would call it his own words. One Gemini call per run.
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.request
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def las_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "24 kHz mono 16-bit wav"
        return w.readframes(w.getnframes())


def post(rum, pcm, extra_s, port):
    body = json.dumps({"rum": rum, "pcm_b64": base64.b64encode(pcm).decode(), "extra_s": extra_s}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/eko-prov", body, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=len(pcm) / 48000 + 60) as r:
        return json.loads(r.read())


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--text-fil", required=True)
    ap.add_argument("--rum", default="kontoret")
    ap.add_argument("--gange", type=int, default=3)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--utan-moln", action="store_true")
    ap.add_argument("--spara", help="folder to keep each capture as a wav")
    a = ap.parse_args()
    from app import bana0, eko_vakt
    from tools.stt_jamfor import gemini_
    pcm, text = las_wav(a.wav), open(a.text_fil).read()
    tung = {w for w in re.findall(r"[a-zåäö]{5,}", text.lower())}
    host, port = os.environ.get("BANA0_STT", "127.0.0.1:10300").rsplit(":", 1)
    ok = 0
    for i in range(a.gange):
        r = post(a.rum, pcm, 2.0, a.port)
        if "error" in r:
            print(i + 1, "error", r["error"])
            continue
        heard = base64.b64decode(r.pop("pcm_b64"))
        lv = r["levels_dbfs_per_250ms"]
        topp = max(lv) if lv else None
        b = await bana0.transkribera(heard[int(r["mark_s"] * 32000):], host, int(port), 15.0)
        g = None if a.utan_moln else await gemini_(heard[int(r["mark_s"] * 32000):])
        hort = {w for w in re.findall(r"[a-zåäö]{5,}", (g or "").lower())}
        traffar = sorted(tung & hort)
        eko = eko_vakt.ar_eko(b, text)
        godkand = topp is not None and topp <= -45 and not traffar
        ok += godkand
        print(f"{i + 1}: clip {r['clip_s']} s, heard {r['heard_s']} s, loudest {topp} dBFS, "
              f"bana0 {b!r}, gemini {g!r}, his words heard by gemini {traffar}, net would ignore: {eko} -> {'OK' if godkand else 'NOT OK'}")
        if a.spara:
            os.makedirs(a.spara, exist_ok=True)
            with wave.open(os.path.join(a.spara, f"eko_{i + 1}.wav"), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                w.writeframes(heard)
    print(f"AC-1: {ok} of {a.gange} runs under -45 dBFS with none of his words heard")


if __name__ == "__main__":
    asyncio.run(main())
