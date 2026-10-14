"""What the home has, read from Home Assistant itself (0.18.30).

The router's backend used to guess device names blind: its prompt said "use
the device's ordinary name" and listed nothing, so "turn on boost heat
downstairs" became GetLiveContext(name='boost heat', area='downstairs') and
two exact-match misses. Home Assistant already publishes what a model needs:
the MCP server's ``Assist`` prompt (every exposed entity with its names,
domain and areas) and the area and floor registries. This module turns them
into one stable block for the backend's cached prefix, and finds the room of
the satellite that is listening (its MAC from the ARP table, matched to the
device registry), so "the lights" means the lights of the room you are in.

Nothing here is hand-kept: rename, expose or move a device in Home Assistant
and the next refresh picks it up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MCP_URL = "http://supervisor/core/api/mcp"
ARP_PATH = "/proc/net/arp"
REFRESH_S = 300.0

# Domains a spoken command can act on. Sensors and the like are read through
# GetLiveContext and only bloat the prefix.
CONTROL_DOMAINS = frozenset({
    "light", "switch", "fan", "climate", "cover", "lock", "media_player", "script",
    "scene", "input_boolean", "todo", "vacuum", "valve", "water_heater", "humidifier",
})

# Domains kept exposed to Assist automatically, so a device or helper added
# in Home Assistant is controllable by voice without anyone exposing it.
# Locks are left to the owner: exposure is their choice.
AUTO_EXPOSE_DOMAINS = CONTROL_DOMAINS - {"lock"}
# Helpers and scripts are automation plumbing as often as controls (the
# helpers that back other automations). One is a
# control when a person can tap it: it is on a dashboard.
DASHBOARD_ONLY_DOMAINS = frozenset({"input_boolean", "script"})
_DASHBOARD_ENTITY = re.compile(r"\b(?:input_boolean|script)\.[a-z0-9_]+\b")


def dashboard_entities(configs: List[Any]) -> set:
    """Helper and script entity ids mentioned anywhere in dashboard configs."""
    found = set()
    for config in configs:
        found.update(_DASHBOARD_ENTITY.findall(json.dumps(config)))
    return found


def entities_to_expose(registry: List[dict], exposed: Dict[str, Dict[str, bool]],
                       on_dashboard: Optional[set] = None) -> List[str]:
    """Registry entities a person could name (enabled, visible, not a
    config/diagnostic entity) in AUTO_EXPOSE_DOMAINS that Assist cannot see;
    helpers and scripts only when they are on a dashboard. An entity the
    owner explicitly un-exposed stays un-exposed."""
    out = []
    on_dashboard = on_dashboard or set()
    for e in registry:
        entity_id = str(e.get("entity_id") or "")
        domain = entity_id.split(".", 1)[0]
        if domain not in AUTO_EXPOSE_DOMAINS:
            continue
        if domain in DASHBOARD_ONLY_DOMAINS and entity_id not in on_dashboard:
            continue
        if e.get("disabled_by") or e.get("hidden_by") or e.get("entity_category"):
            continue
        state = exposed.get(entity_id) or {}
        if "conversation" in state:  # decided already (True, or a deliberate False)
            continue
        out.append(entity_id)
    return sorted(out)


_ENTRY = re.compile(r"^- names: (?P<names>.*)$")
_FIELD = re.compile(r"^  (?P<key>domain|areas): (?P<value>.*)$")


def fold(text: str) -> str:
    """Lower-case ASCII words, for comparing names with what was said."""
    text = unicodedata.normalize("NFKD", (text or "").replace("’", "'").lower())
    text = text.encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def mentions(text: str, phrase: str) -> bool:
    """Whole-word containment after folding."""
    phrase = fold(phrase)
    return bool(phrase) and f" {phrase} " in f" {fold(text)} "


def parse_overview(prompt: str) -> List[Dict[str, Any]]:
    """The entity entries of Home Assistant's Assist prompt ("Static
    Context"): [{"names": str, "domain": str, "areas": str}]."""
    entries: List[Dict[str, Any]] = []
    for line in (prompt or "").splitlines():
        m = _ENTRY.match(line)
        if m:
            entries.append({"names": m.group("names").strip(), "domain": "", "areas": ""})
            continue
        f = _FIELD.match(line)
        if f and entries:
            entries[-1][f.group("key")] = f.group("value").strip()
    return entries


def overview_block(entries: List[Dict[str, Any]], areas: List[dict], floors: List[dict]) -> str:
    """One compact, deterministic block: floors with their rooms, then every
    controllable device as "name (domain, room)"."""
    lines: List[str] = []
    if floors or areas:
        lines.append("Floors and rooms of this home (a floor alias such as 'downstairs' means every room on it):")
        by_floor: Dict[Optional[str], List[str]] = {}
        for a in sorted(areas, key=lambda a: a.get("name") or ""):
            label = a.get("name") or ""
            if a.get("aliases"):
                label += f" (also: {', '.join(sorted(a['aliases']))})"
            by_floor.setdefault(a.get("floor_id"), []).append(label)
        for fl in sorted(floors, key=lambda f: (f.get("level") is None, f.get("level") or 0, f.get("name") or "")):
            name = fl.get("name") or fl.get("floor_id")
            aliases = f" (also: {', '.join(sorted(fl.get('aliases') or []))})" if fl.get("aliases") else ""
            rooms = ", ".join(by_floor.get(fl.get("floor_id"), [])) or "no rooms"
            lines.append(f"- {name}{aliases}: {rooms}")
        if by_floor.get(None):
            lines.append(f"- no floor: {', '.join(by_floor[None])}")
    seen = set()
    devices = []
    for e in entries:
        if e.get("domain") not in CONTROL_DOMAINS:
            continue
        line = f"- {e['names']} ({e['domain']}" + (f", {e['areas']}" if e.get("areas") else "") + ")"
        if line not in seen:
            seen.add(line)
            devices.append(line)
    if devices:
        lines.append("Devices you can control, exactly as Home Assistant names them "
                     "(pass these names to the tools; match loose wording to them):")
        lines.extend(sorted(devices, key=str.lower))
    return "\n".join(lines)


def satellite_mac(ip: str, arp_path: str = ARP_PATH) -> Optional[str]:
    """The MAC of ``ip`` from the kernel ARP table (the add-on runs on the
    host network), or None."""
    try:
        with open(arp_path) as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
                    return parts[3].lower()
    except OSError:
        return None
    return None


async def fetch_assist_prompt(token: str, url: str = MCP_URL, timeout_s: float = 15.0) -> str:  # pragma: no cover - real HTTP
    """Home Assistant's MCP ``Assist`` prompt text."""
    import httpx
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        init = await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "voicepe-household", "version": "1"}}})
        init.raise_for_status()
        sid = init.headers.get("Mcp-Session-Id")
        if sid:
            headers["Mcp-Session-Id"] = sid
        await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply = await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "Assist", "arguments": {}}})
        reply.raise_for_status()
        body = reply.text
        if "text/event-stream" in (reply.headers.get("Content-Type") or ""):
            body = next((l[5:].strip() for l in body.splitlines() if l.startswith("data:")), "{}")
        data = json.loads(body)
    messages = ((data.get("result") or {}).get("messages")) or []
    return "\n".join(str((m.get("content") or {}).get("text") or "") for m in messages)


CommandFn = Callable[[dict], Awaitable[dict]]


@dataclass
class Room:
    area_id: str
    name: str
    aliases: List[str] = field(default_factory=list)
    floor_id: Optional[str] = None


class HouseholdDirectory:
    """Home Assistant's own view of the home, refreshed in the background.

    ``command`` sends one Core websocket command and returns its reply (the
    intent recogniser's socket). ``fetch_prompt`` returns the Assist prompt.
    """

    def __init__(self, command: Optional[CommandFn], fetch_prompt: Optional[Callable[[], Awaitable[str]]],
                 refresh_s: float = REFRESH_S, arp_path: str = ARP_PATH, auto_expose: bool = False):
        self._command = command
        self._fetch_prompt = fetch_prompt
        self.auto_expose = auto_expose
        self.refresh_s = refresh_s
        self.arp_path = arp_path
        self._block = ""
        self._rooms: Dict[str, Room] = {}
        self._devices: List[dict] = []
        self._refreshed = 0.0
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._command is not None or self._fetch_prompt is not None

    def block(self) -> str:
        """The overview for the backend's stable prefix ("" until fetched)."""
        return self._block

    def rooms(self) -> List[Room]:
        return list(self._rooms.values())

    def ensure_fresh(self) -> None:
        """Start a background refresh when stale; never waits."""
        if not self.enabled or (self._task is not None and not self._task.done()):
            return
        if self._refreshed and time.monotonic() - self._refreshed < self.refresh_s:
            return
        self._task = asyncio.get_running_loop().create_task(self.refresh())

    async def refresh(self) -> None:
        areas: List[dict] = []
        floors: List[dict] = []
        entries: List[Dict[str, Any]] = []

        async def step(name, run):
            # One failing step (an older Core without floors, a dashboard
            # read refused) does not cost the others.
            try:
                return await run()
            except Exception as e:
                logger.info(f"ℹ️ household directory: {name} failed: {e!r}")
                return None
        if self._command is not None:
            areas = await step("areas", lambda: self._result({"type": "config/area_registry/list"})) or []
            floors = await step("floors", lambda: self._result({"type": "config/floor_registry/list"})) or []
            self._devices = await step("devices", lambda: self._result(
                {"type": "config/device_registry/list"})) or self._devices
            if self.auto_expose:
                await step("exposure", self._sync_exposure)
        if self._fetch_prompt is not None:
            prompt = await step("Assist prompt", self._fetch_prompt)
            entries = parse_overview(prompt or "")
        if areas:
            self._rooms = {a["area_id"]: Room(a["area_id"], a.get("name") or a["area_id"],
                                              list(a.get("aliases") or []), a.get("floor_id"))
                           for a in areas if a.get("area_id")}
        block = overview_block(entries, areas, floors)
        if block:
            if block != self._block:
                logger.info(f"🏠 household directory: {len(self._rooms)} rooms, "
                            f"{block.count(chr(10) + '- ')} lines")
            self._block = block
        self._refreshed = time.monotonic()

    async def _sync_exposure(self) -> None:
        """Expose to Assist every nameable entity in AUTO_EXPOSE_DOMAINS that
        is not exposed yet (and was never deliberately un-exposed)."""
        # The display list: ~0.5 MB instead of several, disabled entities
        # already left out; "hb" = hidden, "ec" = config/diagnostic category.
        compact = await self._command({"type": "config/entity_registry/list_for_display"})
        if not compact.get("success"):
            raise RuntimeError(f"entity registry: {(compact.get('error') or {}).get('message')}")
        registry = [{"entity_id": e.get("ei"), "hidden_by": "hidden" if e.get("hb") else None,
                     # "ec" is an index and 0 ("config") is falsy: test presence.
                     "entity_category": "category" if e.get("ec") is not None else None}
                    for e in (compact.get("result") or {}).get("entities") or []]
        exposed = (await self._command({"type": "homeassistant/expose_entity/list"})
                   ).get("result", {}).get("exposed_entities") or {}
        missing = entities_to_expose(registry, exposed, await self._dashboard_entities())
        if not missing:
            return
        await self._result({"type": "homeassistant/expose_entity", "assistants": ["conversation"],
                            "entity_ids": missing, "should_expose": True})
        logger.info(f"🏠 exposed {len(missing)} new entities to Assist: {', '.join(missing[:12])}"
                    + (f" (+{len(missing) - 12})" if len(missing) > 12 else ""))

    async def _dashboard_entities(self) -> set:
        """Helpers and scripts on any storage-mode dashboard."""
        configs = []
        boards = await self._result({"type": "lovelace/dashboards/list"})
        for url_path in [None] + [b.get("url_path") for b in boards if b.get("mode", "storage") == "storage"]:
            payload = {"type": "lovelace/config"}
            if url_path:
                payload["url_path"] = url_path
            reply = await self._command(payload)
            if reply.get("success"):
                configs.append(reply.get("result"))
        return dashboard_entities(configs)

    async def _result(self, payload: dict) -> list:
        reply = await self._command(dict(payload))
        if not reply.get("success"):
            raise RuntimeError(f"{payload['type']}: {(reply.get('error') or {}).get('message')}")
        return reply.get("result") or []

    def room_of(self, satellite_ip: Optional[str]) -> Optional[Room]:
        """The room of the satellite at ``satellite_ip``: its MAC from the ARP
        table, matched to a Home Assistant device that has an area."""
        if not satellite_ip:
            return None
        mac = satellite_mac(satellite_ip, self.arp_path)
        if not mac:
            return None
        for device in self._devices:
            if not device.get("area_id") or device.get("disabled_by"):
                continue
            if any(str(c[1]).lower() == mac for c in device.get("connections") or [] if len(c) == 2):
                return self._rooms.get(device["area_id"]) or Room(device["area_id"], device["area_id"])
        return None

    def device_id_of(self, satellite_ip: Optional[str]) -> Optional[str]:
        """The Home Assistant device id of the satellite, for room context."""
        mac = satellite_mac(satellite_ip, self.arp_path) if satellite_ip else None
        if not mac:
            return None
        for device in self._devices:
            if device.get("area_id") and not device.get("disabled_by") and any(
                    str(c[1]).lower() == mac for c in device.get("connections") or [] if len(c) == 2):
                return device.get("id")
        return None

    def spoken_room(self, text: str) -> Optional[Room]:
        """A room (or alias) named in ``text``, longest name first."""
        best = None
        for room in self._rooms.values():
            for name in [room.name] + room.aliases:
                if mentions(text, name) and (best is None or len(name) > len(best[1])):
                    best = (room, name)
        return best[0] if best else None
