"""Where does the time go from the end of a question to the first sound? (raawr US-032)

Runs the satellite stand-in with each wav a few times against the agent and splits the
wait with the agent's own journal lines:

  tystnad   the question ends -> `phase -> thinking` (the local turn end waits for silence)
  lokal     thinking -> comms' answer to the local turn (bana 0: STT + HA)
  motor     comms' answer -> the first sound (the cloud engine, tools included)
  verktyg   tool time finished before the first sound ("⏱ tool X N ok")
  uppspelning  the server's `phase -> replying` -> the stand-in hears it
  filler    the first sound was the early-ack line, not the answer
  svar      the question ends -> the answer's own first sound (`phase -> replying`)

    python tools/forstaljud.py ren.wav hem.wav core.wav --reps 3 [--json out.json]

Run on core with /root/PROVPLATS set. The stand-in's minutes count in its own ledger.
"""
import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys

PY = "/opt/raawr-rostagent/venv/bin/python"


def tid(rad: str) -> dt.datetime:
    return dt.datetime.fromisoformat(rad.split()[0][:26]).replace(tzinfo=None)


def dela(handelser: dict, rader: list[str]) -> dict:
    """Stage times (s) of one turn from the stand-in's events {vad: t} and journal lines."""
    vak = next(tid(r) for r in rader if "device wake received" in r)
    slut = vak + dt.timedelta(seconds=handelser["fragan_slut"])
    ljud = vak + dt.timedelta(seconds=handelser["forsta_svarsljud"])
    efter = [r for r in rader if tid(r) >= slut]

    def forst(mon, fran=slut):
        return next((tid(r) for r in efter if mon in r and tid(r) >= fran), None)

    tank = forst("phase -> thinking")
    lokal = forst("conversation/process", tank or slut)
    svar = forst("phase -> replying")
    ack = forst("early ack:")
    filler = ack is not None and abs((ack - ljud).total_seconds()) < 0.3
    verktyg = sum(int(m.group(1)) for r in efter if tid(r) <= ljud and (m := re.search(r"⏱ tool \S+ (\d+) ok", r)))
    s = lambda a, b: round((b - a).total_seconds(), 2) if a and b else None
    return {
        "totalt": s(slut, ljud),
        "svar": s(slut, svar),  # the real answer's first sound (the filler, if any, came before)
        "tystnad": s(slut, tank),
        "lokal": s(tank, lokal),
        "motor": s(lokal or tank or slut, ljud),
        "verktyg": round(verktyg / 1000, 2),
        "uppspelning": None if filler else s(svar, ljud),
        "filler": filler,
    }


def kor(wav: str, sek: int) -> dict:
    t0 = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    ut = subprocess.run([PY, "satellit_attrapp.py", "--wav", wav, "--device", "attrapp", "--sekunder", str(sek)],
                        capture_output=True, text=True, cwd="/root").stdout
    h = {}
    for rad in ut.splitlines():
        if rad.startswith("{"):
            j = json.loads(rad)
            h.setdefault(j["vad"], j["t"])
    jr = subprocess.run(["journalctl", "-u", "raawr-rostagent", "--no-pager", "-o", "short-iso-precise",
                         "--since", t0.strftime("%Y-%m-%d %H:%M:%S")], capture_output=True, text=True).stdout
    if "forsta_svarsljud" not in h:
        return {"fel": "inget svarsljud"}
    return dela(h, jr.splitlines())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("wavs", nargs="+")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--sekunder", type=int, default=25)
    p.add_argument("--json")
    a = p.parse_args()
    alla = {}
    for wav in a.wavs:
        alla[wav] = [kor(wav, a.sekunder) for _ in range(a.reps)]
        ok = [r for r in alla[wav] if "fel" not in r]
        med = {k: round(statistics.median(x[k] for x in ok if x[k] is not None), 2)
               for k in ("totalt", "svar", "tystnad", "lokal", "motor", "verktyg") if any(x[k] is not None for x in ok)} if ok else {}
        print(f"{wav}: median {med} filler {sum(r.get('filler', False) for r in ok)}/{len(alla[wav])}", flush=True)
    if a.json:
        json.dump(alla, open(a.json, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
