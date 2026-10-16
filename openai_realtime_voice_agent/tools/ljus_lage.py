"""Snapshot every light.* before a test, and put back each one that changed (raawr US-098, B's review of #90).

    python tools/ljus_lage.py spara fore.json
    python tools/ljus_lage.py aterstall fore.json     # turns back on/off each light whose on/off changed

Through the room's comms address (HA_API_URL, COMMS_NYCKEL), like the agent. Only on/off is put back, not the
brightness or colour; a light that was unavailable is left alone.
"""
import json
import os
import sys
import urllib.request


def _anrop(sokvag: str, kropp=None):
    url = os.environ["HA_API_URL"].rstrip("/") + sokvag
    req = urllib.request.Request(url, data=None if kropp is None else json.dumps(kropp).encode(),
                                 headers={"X-Raawr-Nyckel": os.environ["COMMS_NYCKEL"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as svar:
        return json.loads(svar.read() or b"null")


def lagen() -> dict:
    return {e["entity_id"]: e["state"] for e in _anrop("/states") if e["entity_id"].startswith("light.")}


def andringar(fore: dict, efter: dict) -> list[tuple[str, str]]:
    """(entity, service) that puts each light whose on/off changed back the way it was."""
    ut = []
    for eid, var in fore.items():
        nu = efter.get(eid)
        if var in ("on", "off") and nu in ("on", "off") and nu != var:
            ut.append((eid, "turn_on" if var == "on" else "turn_off"))
    return ut


def main() -> int:
    vad, fil = sys.argv[1], sys.argv[2]
    if vad == "spara":
        json.dump(lagen(), open(fil, "w"))
        print(f"sparade {fil}")
        return 0
    for eid, tjanst in andringar(json.load(open(fil)), lagen()):
        _anrop(f"/services/light/{tjanst}", {"entity_id": eid})
        print(f"återställd: {eid} {tjanst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
