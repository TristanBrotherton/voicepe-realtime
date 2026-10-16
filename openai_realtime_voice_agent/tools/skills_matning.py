"""raawr US-098: the same questions through today's tools (a), the two meta tools (b), and (c), measured alike.

Runs the satellite stand-in once per question and rep against one agent instance and reads that instance's
journal: time to first sound (tools/forstaljud.py's split), the tools the model called (for `anvand_verktyg`,
the comms tool it named), what Björn said, and whether that was right by the question's key
(`fragor.jsonl`: {"id", "wav", "vantat": [tool names, "-" = no tool is right too], "ord": [words, one must be said]}).

    python tools/skills_matning.py fragor.jsonl --vag b --url ws://127.0.0.1:18080/ --unit prov-skills-b \
        --reps 2 --ut b.jsonl

Run on core with /root/PROVPLATS set, next to tools/forstaljud.py and tools/satellit_attrapp.py. The stand-in's
cloud minutes count in the test ledger (MOLN_MAX_MINUTER_PROV_PER_DAG). Never a question that touches a lock, the
alarm, the garage, the TV or the living room; a light that is turned on is turned off by the next question.
"""
import argparse
import ast
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forstaljud import PY, dela  # noqa: E402

ANROP = re.compile(r"Calling function \[([^:\]]+)[^\]]*\] with arguments (\{.*\})")
TID = re.compile(r"⏱ tool (\S+) (\d+) (\S+)")
SA = re.compile(r"(?:🤖 assistant|💬 Björn sa): (.*)")


def anropade(rader: list[str]) -> list[str]:
    """Tool names in call order; `anvand_verktyg` is shown as the comms tool it ran (`anvand_verktyg:styr`)."""
    ut = []
    for r in rader:
        m = ANROP.search(r)
        if not m:
            continue
        namn = m.group(1).rsplit("__", 1)[-1]
        if namn == "anvand_verktyg":
            try:
                namn += ":" + str(ast.literal_eval(m.group(2)).get("namn", "?")).rsplit("__", 1)[-1]
            except (ValueError, SyntaxError):
                namn += ":?"
        ut.append(namn)
    return ut


def verktygstider(rader: list[str], anrop: list[str]) -> list[list]:
    """[name, ms, outcome] per finished tool, `anvand_verktyg` named by the comms tool it ran (in call order)."""
    inre = iter(a for a in anrop if a.startswith("anvand_verktyg"))
    ut = []
    for r in rader:
        if m := TID.search(r):
            namn = m.group(1).rsplit("__", 1)[-1]
            ut.append([next(inre, namn) if namn == "anvand_verktyg" else namn, int(m.group(2)), m.group(3)])
    return ut


def ratt(fraga: dict, verktyg: list[str], sagt: str) -> bool:
    """Right = a wanted tool was called (or none, when "-" is allowed), and one key word was said if any are set."""
    kallade = {v.split(":", 1)[-1] for v in verktyg if not v.startswith("las_skill")}
    vantat = set(fraga.get("vantat", []))
    verktyg_ok = bool(kallade & vantat) or ("-" in vantat and not kallade)
    ord = [o.lower() for o in fraga.get("ord", [])]
    return verktyg_ok and (not ord or any(o in sagt.lower() for o in ord))


def kor(fraga: dict, a) -> dict:
    t0 = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    attrapp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "satellit_attrapp.py")
    ut = subprocess.run([PY, attrapp, "--wav", fraga["wav"], "--url", a.url, "--device", "attrapp",
                         "--sekunder", str(a.sekunder)], capture_output=True, text=True).stdout
    h = {}
    for rad in ut.splitlines():
        if rad.startswith("{"):
            j = json.loads(rad)
            h.setdefault(j["vad"], j["t"])
    rader = subprocess.run(["journalctl", "-u", a.unit, "--no-pager", "-o", "short-iso-precise",
                            "--since", t0.strftime("%Y-%m-%d %H:%M:%S")], capture_output=True, text=True).stdout.splitlines()
    verktyg = anropade(rader)
    sagt = " ".join(m.group(1).strip() for r in rader if (m := SA.search(r)))
    rad = {"vag": a.vag, "id": fraga["id"], "verktyg": verktyg, "tider": verktygstider(rader, verktyg),
           "sagt": sagt[:400]}
    if "forsta_svarsljud" not in h:
        return {**rad, "fel": "inget svarsljud", "ratt": False}
    return {**rad, **dela(h, rader), "ratt": ratt(fraga, verktyg, sagt)}


def p95(v: list[float]) -> float:
    return sorted(v)[max(0, round(0.95 * len(v)) - 1)]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("fragor")
    p.add_argument("--vag", required=True)
    p.add_argument("--url", default="ws://127.0.0.1:8080/")
    p.add_argument("--unit", default="raawr-rostagent")
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--sekunder", type=int, default=18)
    p.add_argument("--ut", required=True)
    a = p.parse_args()
    fragor = [json.loads(r) for r in open(a.fragor) if r.strip()]
    rader = []
    with open(a.ut, "a") as ut:
        for _ in range(a.reps):
            for f in fragor:
                r = kor(f, a)
                rader.append(r)
                ut.write(json.dumps(r, ensure_ascii=False) + "\n")
                ut.flush()
                print(json.dumps(r, ensure_ascii=False)[:300], flush=True)
    tider = [r["totalt"] for r in rader if r.get("totalt") is not None]
    per_verktyg: dict = {}
    for r in rader:
        for namn, ms, _ in r.get("tider", []):
            per_verktyg.setdefault(namn, []).append(ms)
    print(json.dumps({"vag": a.vag, "turer": len(rader), "ratt": sum(r["ratt"] for r in rader),
                      "forsta_ljud_median": statistics.median(tider) if tider else None,
                      "forsta_ljud_p95": p95(tider) if tider else None,
                      "verktyg_p95_ms": {n: p95(v) for n, v in sorted(per_verktyg.items())}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
