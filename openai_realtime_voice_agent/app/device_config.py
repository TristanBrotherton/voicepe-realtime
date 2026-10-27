"""Per-device settings for one add-on instance serving several Voice PEs.

The ``devices`` option names each device and, optionally, overrides the
instance-wide settings for it. A device is matched by the ``device_id`` in
its connection URL (``ws://<host>:<port>/?device_id=<name>``) or by the port
it connects to, so devices that were each pointed at their own add-on copy
can move onto one instance by keeping their ports, with no reflash.

With no ``devices`` configured (the default), nothing changes: one port,
instance-wide settings, devices identified by their URL or IP as before.

Example option (YAML):

    devices:
      - name: hallway
        port: 8082
        announce_port: 8091
        follow_up_listen_seconds: 4
        timer_ring_entity: switch.hallway_voice_timer_ringing
        live_max_usd_per_day: 1
        live_max_session_minutes_per_day: 20
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional

from app.device_registry import sanitize_device_id

logger = logging.getLogger(__name__)

# Overrides a device may set; None means "use the instance value".
_INT_FIELDS = ("port", "announce_port", "follow_up_listen_seconds", "follow_up_open_delay_ms",
               "wake_open_delay_ms", "playback_prebuffer_ms", "output_lead_buffer_ms")
_FLOAT_FIELDS = ("live_max_usd_per_day", "live_max_session_minutes_per_day")
_STR_FIELDS = ("timer_ring_entity", "room", "voice")


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    port: Optional[int] = None
    announce_port: Optional[int] = None
    follow_up_listen_seconds: Optional[int] = None
    follow_up_open_delay_ms: Optional[int] = None
    wake_open_delay_ms: Optional[int] = None
    playback_prebuffer_ms: Optional[int] = None
    output_lead_buffer_ms: Optional[int] = None
    live_max_usd_per_day: Optional[float] = None
    live_max_session_minutes_per_day: Optional[float] = None
    timer_ring_entity: Optional[str] = None
    room: Optional[str] = None
    voice: Optional[str] = None

    @property
    def has_own_caps(self) -> bool:
        return self.live_max_usd_per_day is not None or self.live_max_session_minutes_per_day is not None


def _profile(raw: Any) -> Optional[DeviceProfile]:
    if not isinstance(raw, dict):
        return None
    name = sanitize_device_id(str(raw.get("name") or ""))
    if not name:
        return None
    values: Dict[str, Any] = {"name": name}
    for key in _INT_FIELDS:
        value = raw.get(key)
        if value not in (None, ""):
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                logger.warning(f"⚠️ device {name}: ignoring {key}={value!r} (not a whole number)")
    for key in _FLOAT_FIELDS:
        value = raw.get(key)
        if value not in (None, ""):
            try:
                values[key] = float(value)
            except (TypeError, ValueError):
                logger.warning(f"⚠️ device {name}: ignoring {key}={value!r} (not a number)")
    for key in _STR_FIELDS:
        value = str(raw.get(key) or "").strip()
        if value:
            values[key] = value
    for key in ("port", "announce_port"):
        if key in values and not 1024 <= values[key] <= 65535:
            logger.warning(f"⚠️ device {name}: ignoring {key}={values[key]} (outside 1024-65535)")
            values.pop(key)
    return DeviceProfile(**values)


@dataclass
class DeviceDirectory:
    """The configured devices, looked up by name or listening port."""

    profiles: List[DeviceProfile] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str) -> "DeviceDirectory":
        try:
            raw = json.loads(text) if (text or "").strip() else []
        except ValueError:
            logger.error("❌ the devices option is not valid JSON; no per-device settings")
            return cls()
        if not isinstance(raw, list):
            return cls()
        profiles: List[DeviceProfile] = []
        names, ports = set(), set()
        for entry in raw:
            profile = _profile(entry)
            if profile is None:
                logger.warning(f"⚠️ ignoring a devices entry without a usable name: {entry!r}")
                continue
            if profile.name in names:
                logger.warning(f"⚠️ device {profile.name} is listed twice; using the first entry")
                continue
            if profile.port is not None and profile.port in ports:
                logger.warning(f"⚠️ device {profile.name}: port {profile.port} is taken; not matched by port")
                profile = DeviceProfile(**{f.name: getattr(profile, f.name) for f in fields(profile)
                                           if f.name != "port"})
            names.add(profile.name)
            if profile.port is not None:
                ports.add(profile.port)
            profiles.append(profile)
        return cls(profiles)

    @classmethod
    def from_env(cls) -> "DeviceDirectory":
        return cls.from_json(os.environ.get("DEVICES_JSON", ""))

    def __bool__(self) -> bool:
        return bool(self.profiles)

    def get(self, name: str) -> Optional[DeviceProfile]:
        return next((p for p in self.profiles if p.name == name), None)

    def by_port(self, port: Optional[int]) -> Optional[DeviceProfile]:
        if port is None:
            return None
        return next((p for p in self.profiles if p.port == port), None)

    def by_announce_port(self, port: int) -> Optional[DeviceProfile]:
        return next((p for p in self.profiles if p.announce_port == port), None)

    def extra_ports(self, main_port: int) -> List[int]:
        """Device ports to listen on besides the instance's own."""
        return sorted({p.port for p in self.profiles if p.port is not None and p.port != main_port})

    def announce_ports(self) -> List[int]:
        return sorted({p.announce_port for p in self.profiles if p.announce_port is not None})

    def timer_ring_mappings(self) -> str:
        """``name=entity`` pairs in the TIMER_RING_ENTITIES format."""
        return ",".join(f"{p.name}={p.timer_ring_entity}" for p in self.profiles if p.timer_ring_entity)

    def describe(self) -> Iterable[str]:
        for p in self.profiles:
            own = {f.name: getattr(p, f.name) for f in fields(p)
                   if f.name not in ("name", "timer_ring_entity") and getattr(p, f.name) is not None}
            yield f"{p.name}: {own or 'instance defaults'}"
