"""Does the cloud ledger match the agent's own journal? (raawr US-032 AC-4)

Sums the journal's connect/close pairs ("☁️ connected ... [moln abc123]",
"🧾 cloud session closed ... [moln abc123]") and compares the sum with the
ledger's growth over the same span. A session the process died with (kill -9)
ends at the next systemd line about the unit; one already open at the start
counts from the start. Sessions still open now count in the journal but are
booked only every 20 s, so run it with nothing open. Exit 1 when they differ
by more than a minute.

    python tools/minutkoll.py                                  # today, from midnight UTC
    python tools/minutkoll.py --fran "2026-10-07 16:00:00" --start-varde 1563.8

Run on core, where the unit's journal and the ledger live. Only sessions from
0.27.8 on carry the [moln] tag.
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys

LEDGERS = {"moln": "/var/lib/raawr-rostagent/data/moln_minuter.json",
           "prov": "/var/lib/raawr-rostagent/data/moln_minuter_prov.json"}
UNIT = "raawr-rostagent"
TAGG = re.compile(r"\[(moln|prov) (\w+)\]")


def summera(rader, slut: dt.datetime, fran: dt.datetime | None = None, typ: str = "moln") -> float:
    """Connected seconds of one ledger kind ("moln" real speakers, "prov" the
    stand-in) in (timestamp, text) lines, in time order."""
    oppna, total = {}, 0.0
    for ts, text in rader:
        m = TAGG.search(text)
        if m and m.group(1) != typ:
            continue
        if m and "connected to the cloud engine" in text:
            oppna[m.group(2)] = ts
        elif m and "cloud session closed" in text:
            start = oppna.pop(m.group(2), None)
            if start is None and fran is not None:
                # Open before the span began: count only its part inside it.
                efter = re.search(r"after (\d+)s", text)
                start = max(fran, ts - dt.timedelta(seconds=int(efter.group(1)))) if efter else ts
            if start is not None:
                total += (ts - start).total_seconds()
        elif "systemd[" in text and f"{UNIT}.service" in text:
            # The process ended (stop, crash, kill -9): whatever was open ends here.
            total += sum((ts - s).total_seconds() for s in oppna.values())
            oppna.clear()
    return total + sum((slut - s).total_seconds() for s in oppna.values())


def ledger_varde(path: str, dag: str) -> float:
    varden = []
    for p in (path, path + ".kopia"):
        try:
            with open(p) as f:
                varden.append(float(json.load(f).get(dag, 0.0)))
        except (OSError, ValueError, AttributeError):
            pass
    return max(varden, default=0.0)


def journal(fran: dt.datetime, till: dt.datetime):
    out = subprocess.run(
        ["journalctl", "-u", UNIT, "--no-pager", "-o", "short-iso-precise",
         "--since", fran.strftime("%Y-%m-%d %H:%M:%S"), "--until", till.strftime("%Y-%m-%d %H:%M:%S")],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        try:
            ts = dt.datetime.fromisoformat(line.split()[0][:26])
        except (ValueError, IndexError):
            continue
        yield ts.replace(tzinfo=None), line


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fran", help="start, UTC 'YYYY-MM-DD HH:MM:SS' (default: midnight today)")
    p.add_argument("--start-varde", type=float, default=0.0, help="the ledger's seconds at --fran")
    p.add_argument("--typ", choices=("moln", "prov"), default="moln", help="real speakers or the stand-in")
    p.add_argument("--ledger", help="default: the typ's ledger")
    a = p.parse_args()
    nu = dt.datetime.utcnow()
    fran = dt.datetime.fromisoformat(a.fran) if a.fran else nu.replace(hour=0, minute=0, second=0, microsecond=0)
    journalen = summera(journal(fran, nu), nu, fran, a.typ)
    ledgern = ledger_varde(a.ledger or LEDGERS[a.typ], fran.date().isoformat()) - a.start_varde
    skillnad = ledgern - journalen
    print(f"journalen {journalen / 60:.2f} min, minutfilen {ledgern / 60:.2f} min, skillnad {skillnad / 60:+.2f} min")
    return 0 if abs(skillnad) <= 60 else 1


if __name__ == "__main__":
    sys.exit(main())
