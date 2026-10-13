"""US-060 with a FIXED line, no cloud engine: echo and an interruption from outside, in one run.

The agent plays a long fixed clip (POST /eko-prov: the device's mic streams, every frame is captured before any guard)
and another voice (the Mac, `afplay`) interrupts at a set second. What this measures, per run: the loudest echo before
the interruption (layer 1), the interruption's own level in the mic, and layer 2's verdict on it (bana 0 writes it out,
`eko_vakt.dom` says person or his own words). What it does NOT measure: that the device stops talking (needs an engine
that talks) and the net's false alarms over a real answer. Needs EKO_PROV=1; talks in the room, so run it only when told.

On the Mac (the Mac must be near the device; set its volume so the clip sounds like a person at one metre):

    python tools/eko_avbrott_prov.py --ssh root@10.10.0.118 --klipp vanta.aiff --vid 12 --gange 5

The core half runs by itself: `--core-side` (the Mac half calls it over ssh, reads its START line, and plays on time).
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.eko_prov import las_wav, post  # noqa: E402

PRE_S = 0.6  # the agent opens the mic this long before the clip starts (_eko_prov)


def niva(pcm: bytes, a_s: float, b_s: float) -> float:
    from app.avbrott import dbfs
    fonster = [dbfs(pcm[i:i + 8000]) for i in range(int(a_s * 32000), int(b_s * 32000) - 8000, 8000)]
    return round(max(fonster), 1) if fonster else -96.0


async def core_side(a) -> None:
    from app import bana0, eko_vakt
    pcm, text = las_wav(a.wav), open(a.text_fil).read()
    host, port = os.environ.get("BANA0_STT", "127.0.0.1:10300").rsplit(":", 1)
    for i in range(a.gange):
        print(f"START {time.time():.3f}", flush=True)
        r = post(a.rum, pcm, 2.0, a.port, a.pace)
        if "error" in r:
            print(json.dumps({"i": i + 1, "error": r["error"]}), flush=True)
            time.sleep(5)
            continue
        heard = base64.b64decode(r["pcm_b64"])
        mark = r["mark_s"]
        t0 = mark + a.vid  # the interruption starts about here
        hort = await bana0.transkribera(heard[int((t0 - 0.2) * 32000):int((t0 + 2.5) * 32000)], host, int(port), 15.0)
        ok = hort is not None
        gate = eko_vakt.niva_dbfs()
        avbrott_db = niva(heard, t0 - 0.2, t0 + 1.5)
        # False alarms: the net over 2 s of pure echo, as the agent would judge it (below the level gate = noise)
        falsklarm, fonster = 0, 0
        for a_s in range(int(mark + 1.0), int(t0 - 2.5), 2):
            fonster += 1
            if niva(heard, a_s, a_s + 2) < gate:
                continue
            h = await bana0.transkribera(heard[a_s * 32000:(a_s + 2) * 32000], host, int(port), 15.0)
            falsklarm += not eko_vakt.dom(h, h is not None, text)
        ignorerad = avbrott_db < gate or eko_vakt.dom(hort, ok, text)
        print(json.dumps({"i": i + 1, "eko_dbfs": niva(heard, mark + 1.0, t0 - 0.3), "avbrott_dbfs": avbrott_db,
                          "hort": hort, "avbrott_hors_som_person": not ignorerad,
                          "falsklarm_fonster": f"{falsklarm}/{fonster}"}, ensure_ascii=False), flush=True)
        if a.spara:
            os.makedirs(a.spara, exist_ok=True)
            with wave.open(os.path.join(a.spara, f"avbrott_{i + 1}.wav"), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                w.writeframes(heard)
        time.sleep(4)


def mac_side(a) -> None:
    cmd = ["ssh", a.ssh, f"cd {a.dir} && set -a && . /etc/raawr-rostagent.env && set +a && "
           f"PYTHONPATH={a.dir} {a.python} {a.skript} --core-side --wav {a.wav} --text-fil {a.text_fil} "
           f"--vid {a.vid} --gange {a.gange} --rum {a.rum} --port {a.port}" + (" --pace" if a.pace else "")
           + (f" --spara {a.spara}" if a.spara else "")]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for rad in p.stdout:
        if rad.startswith("START "):
            tid = float(rad.split()[1]) + PRE_S + a.vid
            time.sleep(max(0.0, tid - time.time()))
            subprocess.run(["afplay", a.klipp])
        else:
            print(rad.rstrip(), flush=True)
    p.wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-side", action="store_true")
    ap.add_argument("--wav", default="/root/q/eko2/eko.wav")
    ap.add_argument("--text-fil", default="/root/q/eko2/text.txt")
    ap.add_argument("--vid", type=float, default=12.0, help="seconds into the clip when the interruption starts")
    ap.add_argument("--gange", type=int, default=5)
    ap.add_argument("--rum", default="kontoret")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--pace", action="store_true", help="send the clip in real time, as a reply is")
    ap.add_argument("--spara")
    ap.add_argument("--klipp", help="Mac side: the sound file to play (afplay)")
    ap.add_argument("--ssh", default="root@10.10.0.118")
    ap.add_argument("--dir", default="/opt/raawr-rostagent/app")
    ap.add_argument("--skript", default="tools/eko_avbrott_prov.py", help="path of this file on the core")
    ap.add_argument("--python", default="/opt/raawr-rostagent/venv/bin/python")
    a = ap.parse_args()
    asyncio.run(core_side(a)) if a.core_side else mac_side(a)
