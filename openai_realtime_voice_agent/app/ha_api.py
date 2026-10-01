"""Where Home Assistant is, and the key to it -- both handed out by comms.

The add-on holds no Home Assistant key (raawr US-011, decision 56). HA has no
scoped tokens, so any HA key the add-on held could do anything HA can --
unlock the front door included. Instead every HA call goes to raawr-comms,
which holds HA's token and forwards only the shapes it has handed out:

    HA_API_URL   comms' address for this room, ending in /api, e.g.
                 http://10.10.0.118:3500/kanal/rost/kontoret/api
    COMMS_NYCKEL the key comms gave this add-on (sent as X-Raawr-Nyckel)

Both are options, not code, so moving the agent (US-014) changes a value and
nothing else. Read at call time, not import time, so a restart is never needed
to notice them and tests can set them per test.

SUPERVISOR_TOKEN and LONGLIVED_TOKEN are deliberately never read here.
"""
import os
from typing import Dict


def base() -> str:
    return os.environ.get("HA_API_URL", "").strip().rstrip("/")


def url(path: str) -> str:
    """`path` is what follows /api, e.g. "/states"."""
    return f"{base()}{path}"


def headers() -> Dict[str, str]:
    return {"X-Raawr-Nyckel": os.environ.get("COMMS_NYCKEL", "").strip()}


def configured() -> bool:
    return bool(base() and headers()["X-Raawr-Nyckel"])
