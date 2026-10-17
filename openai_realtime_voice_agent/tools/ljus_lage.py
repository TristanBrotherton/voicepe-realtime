"""Snapshot every light.* before a test, and put back each one that changed (raawr US-098, B's review of #90).

    python tools/ljus_lage.py spara fore.json
    python tools/ljus_lage.py aterstall fore.json     # turns back on/off each light whose on/off changed

Through the room's comms address (HA_API_URL, COMMS_NYCKEL), like the agent. Only on/off is put back, not the
brightness or colour; a light that was unavailable is left alone.

The put-back goes through comms' MCP door (`intent__HassTurnOn` / `intent__HassTurnOff`, by name and domain light),
because the room's door has no `/services/light/...` (US-098: the first version got 403 and could not switch off what
the test had switched on). A light with no friendly name, or two lights with the same one, is reported and exits 1.
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


def _mcp(namn: str, argument: dict):
    """One HA MCP tool call through comms (same shape as app/idag.py `call_ha_tool`)."""
    kropp = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": namn, "arguments": argument}}
    req = urllib.request.Request(os.environ["HA_API_URL"].rstrip("/") + "/mcp", data=json.dumps(kropp).encode(),
                                 headers={"X-Raawr-Nyckel": os.environ["COMMS_NYCKEL"], "Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=10) as svar:
        ut = json.loads(svar.read())
    if "error" in ut:
        raise RuntimeError(f"{namn}: {ut['error']}")
    return ut


def lagen() -> dict:
    """{light.x: state, ..., "_namn": {light.x: friendly name}}; the names are what the MCP door needs."""
    ljus = [e for e in _anrop("/states") if e["entity_id"].startswith("light.")]
    ut = {e["entity_id"]: e["state"] for e in ljus}
    ut["_namn"] = {e["entity_id"]: (e.get("attributes") or {}).get("friendly_name") for e in ljus}
    return ut


def andringar(fore: dict, efter: dict) -> list[tuple[str, str]]:
    """(entity, service) that puts each light whose on/off changed back the way it was."""
    ut = []
    for eid, var in fore.items():
        if not eid.startswith("light."):
            continue
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
    fore = json.load(open(fil))
    nu = lagen()
    namn_fore, namn_nu = fore.get("_namn", {}), nu.get("_namn", {})
    fel = 0
    for eid, tjanst in andringar(fore, nu):
        namn = namn_fore.get(eid) or namn_nu.get(eid)
        dubbletter = [k for k, v in namn_nu.items() if v == namn and k != eid]
        if not namn or dubbletter:
            print(f"kan inte återställa {eid}: {'inget namn' if not namn else 'samma namn som ' + ', '.join(dubbletter)}")
            fel = 1
            continue
        _mcp("intent__HassTurnOn" if tjanst == "turn_on" else "intent__HassTurnOff", {"name": namn, "domain": ["light"]})
        print(f"återställd: {eid} {tjanst}")
    return fel


if __name__ == "__main__":
    raise SystemExit(main())
