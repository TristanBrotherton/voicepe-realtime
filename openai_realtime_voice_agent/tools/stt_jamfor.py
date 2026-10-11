"""Which ear hears Swedish best? Same recorded phrases against bana 0, Google and xAI (raawr D-97).

Henrik reads the PHRASES below, in order, at the device with the recording on (ENABLE_RECORDING). The recording
is one long file; it is cut at pauses, one clip per phrase, and each clip goes to each engine ONCE (8 calls per
engine, never more than 10). Words right = words of the phrase found in the engine's text, in order (LCS), over
the words in the phrase. Keys are never printed.

    python tools/stt_jamfor.py --wav input_kontoret_....wav --motorer bana0,gemini,xai
    python tools/stt_jamfor.py --wav x.wav --bara-klipp          # only cut and list, no engine is called
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PHRASES = [
    "Vad är klockan?",
    "Tänd lampan i kontoret.",
    "Hur blir vädret i morgon?",
    "Sätt på P3.",
    "Lägg till mjölk på inköpslistan.",
    "Stäng av radion.",
    "Hur länge ska ett ägg koka?",
    "Vad står i kalendern på måndag?",
]
MAX_ANROP = 10


def las(path):
    """Mono 16-bit PCM. Read by hand: a recording still open has 0 in its header sizes."""
    raw = open(path, "rb").read()
    hz = int.from_bytes(raw[24:28], "little")
    return np.frombuffer(raw[44:len(raw) - (len(raw) - 44) % 2], dtype=np.int16), hz


def till(data, fran, till_hz):
    if fran == till_hz:
        return data.astype(np.int16)
    from math import gcd
    g = gcd(fran, till_hz)
    from scipy.signal import resample_poly
    return np.clip(resample_poly(data.astype(np.float32), till_hz // g, fran // g), -32768, 32767).astype(np.int16)


def klipp(data, hz, paus_s=0.7, golv_db=-45.0, min_s=0.5):
    """Cut the stream where it is quiet for paus_s. Returns [(start, end)] in samples."""
    f = int(hz * 0.05)
    n = len(data) // f
    db = np.array([20 * np.log10(max(np.sqrt(np.mean(data[i * f:(i + 1) * f].astype(np.float64) ** 2)), 1.0) / 32768)
                   for i in range(n)])
    tal = db > golv_db
    ut, i = [], 0
    while i < n:
        if not tal[i]:
            i += 1
            continue
        j, tyst = i, 0
        while j < n and tyst * 0.05 < paus_s:
            tyst = 0 if tal[j] else tyst + 1
            j += 1
        slut = j - tyst
        if (slut - i) * 0.05 >= min_s:
            ut.append((max(0, i - 4) * f, min(len(data), slut + 4) * f))
        i = j
    return ut


def ord_(s):
    return re.findall(r"[a-zåäö0-9]+", (s or "").lower())


def ratt(facit, hort):
    a, b = ord_(facit), ord_(hort)
    t = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            t[i + 1][j + 1] = t[i][j] + 1 if a[i] == b[j] else max(t[i][j + 1], t[i + 1][j])
    return t[len(a)][len(b)], len(a)


async def bana0_(pcm16k):
    from app import bana0
    host, port = os.environ.get("BANA0_STT", "127.0.0.1:10300").rsplit(":", 1)
    return await bana0.transkribera(pcm16k, host, int(port), 15.0)


async def gemini_(pcm16k):
    from google import genai
    from google.genai import types
    klient = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)))
    text = ""
    async with klient.aio.live.connect(model=os.environ.get("GEMINI_MODEL", "models/gemini-3.8-live"), config=cfg) as s:
        await s.send_realtime_input(activity_start=types.ActivityStart())
        await s.send_realtime_input(audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000"))
        await s.send_realtime_input(activity_end=types.ActivityEnd())
        try:
            async for m in s.receive():
                sc = m.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    text += sc.input_transcription.text
                if sc and (sc.turn_complete or sc.generation_complete):
                    break
        except asyncio.TimeoutError:
            pass
    return text


async def xai_(pcm24k):
    import websockets
    hdr = {"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"}
    kw = {"additional_headers": hdr}
    async with websockets.connect("wss://api.x.ai/v1/realtime", **kw) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {"audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None,
            "transcription": {"language_hint": "sv"}}}}}))
        for i in range(0, len(pcm24k), 9600):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(pcm24k[i:i + 9600]).decode()}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        try:
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), 10.0))
                if e.get("type", "").endswith("input_audio_transcription.completed"):
                    return e.get("transcript", "")
                if e.get("type") == "error":
                    return "FEL " + str(e)[:120]
        except asyncio.TimeoutError:
            return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--motorer", default="bana0,gemini,xai")
    ap.add_argument("--bara-klipp", action="store_true")
    ap.add_argument("--max", type=int, default=len(PHRASES))
    a = ap.parse_args()
    data, hz = las(a.wav)
    bitar = klipp(data, hz)
    print(f"{len(bitar)} clips from {len(data) / hz:.1f} s, expected {len(PHRASES)}: "
          + ", ".join(f"{(e - s) / hz:.1f}s" for s, e in bitar))
    if a.bara_klipp or len(bitar) != len(PHRASES):
        if len(bitar) != len(PHRASES):
            print("Clip count differs from phrase count: fix the cutting before any engine is called.")
        return
    n = min(a.max, MAX_ANROP)
    tot = {}
    for m in a.motorer.split(","):
        r = t = 0
        for fras, (s, e) in list(zip(PHRASES, bitar))[:n]:
            klipp16 = till(data[s:e], hz, 16000).tobytes()
            if m == "bana0":
                hort = await bana0_(klipp16)
            elif m == "gemini":
                hort = await gemini_(klipp16)
            else:
                hort = await xai_(till(data[s:e], hz, 24000).tobytes())
            x, y = ratt(fras, hort)
            r, t = r + x, t + y
            print(f"{m:7} {x}/{y}  facit: {fras!r}  hörde: {hort!r}", flush=True)
        tot[m] = (r, t)
    print("--")
    for m, (r, t) in tot.items():
        print(f"{m:7} {r}/{t} ord rätt ({100 * r / max(t, 1):.0f} %)")


if __name__ == "__main__":
    asyncio.run(main())
