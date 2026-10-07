"""Is a session's state chain unbroken in the journal? (raawr US-032 AC-9)

Reads the agent's "🧭 <device>: A -> B (event: reason)" lines for one device
since a time and checks that each starts where the last one ended, that the
first starts in IDLE and the last ends in IDLE, and that nothing was rejected.
Exit 0 when it holds, 1 otherwise. Run on core.

    python tools/kedjekoll.py attrapp "2026-10-08 08:00:00" ["2026-10-08 08:03:00"]
"""
import re
import subprocess
import sys

RAD = re.compile(r"🧭 (\S+): (?:(\w+) -> (\w+) \((\w+): (.*)\)|rejected (\w+) in (\w+) \((.*)\))")


def kolla(rader: list[str], enhet: str) -> tuple[bool, list[str]]:
    """(ok, one line per step) for the given journal lines."""
    steg, nu, fel = [], "IDLE", []
    for rad in rader:
        m = RAD.search(rad)
        if not m or m.group(1) != enhet:
            continue
        if m.group(6):
            fel.append(f"avvisad: {m.group(6)} i {m.group(7)} ({m.group(8)})")
            continue
        fran, till, handelse, orsak = m.group(2), m.group(3), m.group(4), m.group(5)
        if fran != nu:
            fel.append(f"bruten kedja: {nu} -> (nästa rad börjar i {fran})")
        steg.append(f"{fran} -> {till} ({handelse}: {orsak})")
        nu = till
    if not steg:
        fel.append("inga tillståndsrader")
    elif nu != "IDLE":
        fel.append(f"slutar i {nu}, inte IDLE")
    return not fel, steg + fel


def main() -> int:
    enhet, sedan = sys.argv[1], sys.argv[2]
    till = ["--until", sys.argv[3]] if len(sys.argv) > 3 else []
    ut = subprocess.run(["journalctl", "-u", "raawr-rostagent", "--no-pager", "--since", sedan, *till],
                        capture_output=True, text=True, check=True).stdout
    ok, rader = kolla(ut.splitlines(), enhet)
    print("\n".join(rader))
    print("KEDJAN HÅLLER" if ok else "KEDJAN BRUTEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
