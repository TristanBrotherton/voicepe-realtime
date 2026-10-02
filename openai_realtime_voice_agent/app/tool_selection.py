"""Which Home Assistant tools the realtime model is shown. The one place.

Every declaration is resent with every session, and Gemini reads them all
before its first function call: live 2026-10-02 15:19:44 "vad blir det för
väder i helgen" on 57 tools took 3.7 s from the released audio to the call.
So the model sees what the house asks for -- lights and scenes, media and
music, timers, calendar, lists and notes, weather, time, GetLiveContext,
delegation and web search -- and not every intent HA happens to expose.

Matched on the base name: comms hands tools out as `<domain>__<name>`, HA
itself bare. Hidden is not disabled: pipecat still registers a handler for
every MCP tool, the model just is not offered it.

ponytail: a static list per house. When the tools come from one registry in
comms (raawr US-021) this list moves there and this module goes.
"""
import os

# What is hidden, and why. Usage is the journal 2026-10-01..02 (none of
# these was called once); the rest is what the house never asks the model.
DEFAULT_DENY = frozenset({
    # Replaced by our own tools (play_media searches per kind; the house's
    # timers are set_timer/cancel_timer).
    "HassMediaSearchAndPlay",
    "HassCancelAllTimers",
    # Not something this house does by voice through the model.
    "HassBroadcast",               # announcements go through the announce endpoint
    "HassClimateSetTemperature",   # no climate control asked for
    "HassSetPosition",             # covers; HassTurnOn/Off still open and close them
    "HassStopMoving",
    "RaawrHubVisa",                # hub screen intents
    "RaawrHubAterstall",
    # Old numbered scripts: robot vacuum and HomeKit. HassTurnOn still runs
    # any script by its name ("städa köket"), so nothing is lost but the
    # declarations.
    "1557266572879",  # Homekit start
    "1559850838286",  # Skicka hem Hugo
    "1559849518594",  # Städa Elsas rum
    "1560157469881",  # Städa kontor
    "1559763419170",  # Städa köket
    "1560081785838",  # Städa Moas rum
    "1560157376429",  # Städa sovrummet
    "1559848799861",  # Städa hallen
    "1560157870173",  # Städa lekrummet
    "1559849102426",  # Städa matplatsen
    "1559738227957",  # Städa vardagsrum
})


def _names(env: str):
    return {n.strip().rsplit("__", 1)[-1] for n in os.environ.get(env, "").split(",") if n.strip()}


def deny() -> set:
    """TOOL_DENY (comma-separated) replaces the default; unset = DEFAULT_DENY, "-" = hide nothing."""
    if os.environ.get("TOOL_DENY", "").strip() == "-":
        return set()
    return _names("TOOL_DENY") or set(DEFAULT_DENY)


def shown(name: str, allow=()) -> bool:
    """Whether an HA tool is offered to the model.

    A non-empty `allow` (MCP_TOOL_ALLOWLIST) keeps only those; the deny list
    applies either way.
    """
    base = name.rsplit("__", 1)[-1]
    if allow and name not in allow and base not in allow:
        return False
    return base not in deny()
