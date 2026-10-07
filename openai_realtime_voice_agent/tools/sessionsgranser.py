"""Judge a satellite stand-in run against the agent's journal (raawr US-032 AC-3).

Reads the run log written around tools/satellit_attrapp.py (lines
"=== FALL <name> start HH:MM:SS.mmm", the stand-in's JSON lines, "=== slut",
"=== efter 45 s tyst") and the agent's journal for the same span, then checks
per case that the cloud engine went down for the right reason within the
limit, and that nothing connected it again before the next case.

    python tools/sessionsgranser.py /root/ac3.log [--datum 2026-10-07]

Exit 0 when every case holds, 1 otherwise. Run on core.
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys

# case prefix -> (reason in the "💤 disconnected" line, seconds allowed after the reference point)
REGLER = {
    "A": ("wake without speech", 10.0),   # wake + 5 s limit + 5 s
    "B": ("quiet for", 35.0),             # last audio + 30 s + 5 s
    "D": (None, 35.0),                    # link drop + 30 s + 5 s, any reason
    "C": ("maximum session length", 605.0),  # connect + 600 s + 5 s
}


def klocka(datum: str, hms: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{datum}T{hms}")


def las_korning(path: str, datum: str) -> list[dict]:
    fall, cur = [], None
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"=== FALL (\S+) start (\S+)", line)
        if m:
            cur = {"namn": m.group(1), "start": klocka(datum, m.group(2)), "h": {}}
            fall.append(cur)
            continue
        m = re.match(r"=== efter 45 s tyst (\S+)", line)
        if m and cur:
            cur["stopp"] = klocka(datum, m.group(1))
            continue
        if cur and line.startswith("{"):
            try:
                rad = json.loads(line)
            except ValueError:
                continue
            # t is seconds since the wake; the stand-in waits 1 s after connecting.
            cur["h"].setdefault(rad["vad"], rad["t"])
    return fall


def las_journal(start: dt.datetime, stopp: dt.datetime) -> list[tuple[dt.datetime, str]]:
    out = subprocess.run(
        ["journalctl", "-u", "raawr-rostagent", "--no-pager", "-o", "short-iso-precise",
         "--since", start.strftime("%Y-%m-%d %H:%M:%S"), "--until", stopp.strftime("%Y-%m-%d %H:%M:%S")],
        capture_output=True, text=True, check=True,
    ).stdout
    rader = []
    for line in out.splitlines():
        if "cloud engine" not in line and "Disconnecting from Gemini service" not in line:
            continue
        ts = dt.datetime.fromisoformat(line.split()[0][:26])
        rader.append((ts.replace(tzinfo=None), line))
    return rader


def domare(f: dict, journal) -> tuple[bool, str]:
    regel = REGLER[f["namn"][0]]
    vakning = f["start"] + dt.timedelta(seconds=1.0)
    h = f["h"]
    upp = [t for t, l in journal if "connected to the cloud engine on wake" in l and vakning - dt.timedelta(seconds=1) <= t <= f["stopp"]]
    # A dropped link tears the session down without sova(): the engine's own
    # disconnect line is the proof there (case D).
    ner_tecken = ("💤 disconnected from the cloud engine", "Disconnecting from Gemini service") if f["namn"][0] == "D" else ("💤 disconnected from the cloud engine",)
    ner = [(t, l) for t, l in journal if any(s in l for s in ner_tecken) and t >= vakning]
    if f["namn"][0] == "D":
        bruten = vakning + dt.timedelta(seconds=h.get("lank_bruten", 0.0))
        ner = [(t, l) for t, l in ner if t >= bruten]
    if not ner:
        return False, "ingen nedkoppling i fallets fönster"
    t_ner, rad = ner[0]
    if regel[0] and regel[0] not in rad:
        return False, f"fel orsak: {rad.split(' - ')[-1][:90]}"
    if f["namn"][0] == "A":
        ref = vakning
    elif f["namn"][0] == "B":
        ref = vakning + dt.timedelta(seconds=h.get("mic_stangd", 0.0))
    elif f["namn"][0] == "D":
        ref = vakning + dt.timedelta(seconds=h.get("lank_bruten", 0.0))
    else:
        if not upp:
            return False, "ingen uppkoppling att mäta maxtiden från"
        ref = upp[0]
    gick = (t_ner - ref).total_seconds()
    if gick > regel[1]:
        return False, f"nere {gick:.1f} s efter gränsens start, tillåtet {regel[1]:.0f}"
    senare = [t for t, l in journal if "connected to the cloud engine" in l and t_ner < t <= f["stopp"]]
    if senare:
        return False, f"uppkopplad igen {senare[0].time()} utan ny väckning"
    return True, f"nere {gick:.1f} s efter gränsens start ({rad.split('(')[-1].rstrip(')')[:40]})"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("korlogg")
    p.add_argument("--datum", default=dt.date.today().isoformat())
    a = p.parse_args()
    fall = las_korning(a.korlogg, a.datum)
    if not fall:
        print("inga fall i körloggen")
        return 1
    journal = las_journal(fall[0]["start"], max(f.get("stopp", f["start"]) for f in fall))
    alla = True
    for f in fall:
        if "stopp" not in f:
            print(f"FAIL {f['namn']}: fallet blev aldrig klart")
            alla = False
            continue
        ok, varfor = domare(f, journal)
        alla &= ok
        print(f"{'PASS' if ok else 'FAIL'} {f['namn']}: {varfor}")
    return 0 if alla else 1


if __name__ == "__main__":
    sys.exit(main())
