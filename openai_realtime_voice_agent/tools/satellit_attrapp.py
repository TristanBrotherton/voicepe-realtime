"""Satellite stand-in: talks to the agent's websocket the way a Voice PE does.

One run = one session: connect, start, wake, a spoken question in real time,
then silence. Like the device, the mic closes after the follow-up window once
the agent says idle (a flush, then no more audio; the link stays). Every text frame from the agent, the first and last reply audio
and any slow send is printed as one line with the time since the wake, so a
run can be laid next to the agent's journal (raawr US-032, "Satellitattrappen").

    python tools/satellit_attrapp.py --wav fraga.wav [--url ws://127.0.0.1:8080/]
        [--device attrapp] [--sekunder 25] [--brus] [--bryt-efter S]

--wav none sends no question (a wake nobody answers). --brus fills the time
after the question with noise instead of silence (audio that never ends).
--bryt-efter drops the link S seconds after the wake, without a close frame.

The wav must be 16 kHz mono 16-bit, as the device sends. Runs on core next to
the agent (no auth on 127.0.0.1:8080; comms is the door).
"""
import argparse
import asyncio
import json
import os
import time
import wave

import websockets

FRAME_BYTES = 640  # 20 ms at 16 kHz, 16-bit mono
FRAME_S = 0.02


def ut(t0, what, **kw):
    print(json.dumps({"t": round(time.monotonic() - t0, 3), "vad": what, **kw}, ensure_ascii=False), flush=True)


async def kor(url: str, device: str, pcm: bytes, sekunder: float, brus: bool = False, bryt_efter: float = 0.0) -> int:
    t0 = time.monotonic()
    stangd = {}
    async with websockets.connect(f"{url}?device_id={device}", max_size=None, ping_interval=None) as ws:
        ut(t0, "ansluten")
        await ws.send(json.dumps({"type": "start"}))

        svar = {"bytes": 0, "forsta": None, "sista": None}
        idle_vid = {"t": None}

        async def las():
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        svar["bytes"] += len(msg)
                        nu = round(time.monotonic() - t0, 3)
                        if svar["forsta"] is None:
                            svar["forsta"] = nu
                            ut(t0, "forsta_svarsljud")
                        svar["sista"] = nu
                    else:
                        ut(t0, "text", msg=msg[:200])
                        if '"value":"idle"' in msg and idle_vid["t"] is None:
                            idle_vid["t"] = time.monotonic()
            except websockets.ConnectionClosed as e:
                stangd["kod"], stangd["orsak"] = e.code, e.reason

        lasare = asyncio.create_task(las())
        await asyncio.sleep(1.0)  # the device idles a moment after connecting

        t0 = time.monotonic()
        ut(t0, "wake_skickad")
        await ws.send(json.dumps({"type": "wake"}))

        async def pinga():
            while True:
                await asyncio.sleep(2.0)
                await ws.send(json.dumps({"type": "ping"}))

        pingare = asyncio.create_task(pinga())
        fyll = int(16000 * 2 * sekunder)
        ljud = pcm + (bytes(b & 0x0F for b in os.urandom(fyll)) if brus else bytes(fyll))
        nasta = time.monotonic()
        try:
            for i in range(0, len(ljud), FRAME_BYTES):
                if bryt_efter and time.monotonic() - t0 >= bryt_efter:
                    ut(t0, "lank_bruten")
                    ws.transport.abort()  # no close frame: the link just goes
                    break
                if not brus and idle_vid["t"] and time.monotonic() - idle_vid["t"] >= 6.0:
                    ut(t0, "mic_stangd")
                    await ws.send(json.dumps({"type": "flush"}))
                    await asyncio.sleep(max(0.0, sekunder - (time.monotonic() - t0)))
                    break
                fore = time.monotonic()
                await ws.send(ljud[i:i + FRAME_BYTES])
                if time.monotonic() - fore > 0.1:
                    ut(t0, "langsam_send", ms=round((time.monotonic() - fore) * 1000))
                if i == len(pcm) - len(pcm) % FRAME_BYTES:
                    ut(t0, "fragan_slut")
                nasta += FRAME_S
                await asyncio.sleep(max(0.0, nasta - time.monotonic()))
        except websockets.ConnectionClosed as e:
            stangd["kod"], stangd["orsak"] = e.code, e.reason
        pingare.cancel()
        lasare.cancel()
    ut(t0, "slut", svarsljud_bytes=svar["bytes"], forsta=svar["forsta"], sista=svar["sista"], stangd=stangd or None)
    return 0 if svar["bytes"] and not stangd else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wav", required=True)
    p.add_argument("--url", default="ws://127.0.0.1:8080/")
    p.add_argument("--device", default="attrapp")
    p.add_argument("--sekunder", type=float, default=25.0)
    p.add_argument("--brus", action="store_true")
    p.add_argument("--bryt-efter", type=float, default=0.0)
    a = p.parse_args()
    pcm = b""
    if a.wav != "none":
        with wave.open(a.wav) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), "16 kHz mono 16-bit"
            pcm = w.readframes(w.getnframes())
    return asyncio.run(kor(a.url, a.device, pcm, a.sekunder, a.brus, a.bryt_efter))


if __name__ == "__main__":
    raise SystemExit(main())
