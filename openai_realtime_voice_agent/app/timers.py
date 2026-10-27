"""Voice timers: backend-owned timer registry with on-device ringing.

The Voice PE firmware keeps its stock timer ring machinery (chime + LED,
silenced by the button or the "stop" word) behind a `timer_ringing` switch
that is exposed to Home Assistant. The backend owns the actual timers: the
model calls set/cancel/list tools, an asyncio task fires at expiry, and the
ring is triggered by flipping that switch via the HA API (TIMER_RING_ENTITY
option). If no entity is configured the assistant simply announces expiry is
unavailable rather than pretending.

Timers survive OpenAI session refreshes (they live here, not in the model) but
NOT add-on restarts — acceptable for kitchen timers; documented in DOCS.
"""
import asyncio
import re
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

MAX_TIMERS = 10
MAX_DURATION_S = 24 * 3600
RING_AUTO_OFF_S = 120  # stop ringing after 2 min if nobody silences it
ANNOUNCE_GRACE_S = 20  # spoken announcement first; ring only if unacknowledged


def _ring_entity(device_id: str, allow_legacy: bool = True) -> str:
    mappings = os.environ.get("TIMER_RING_ENTITIES", "")
    for mapping in mappings.split(","):
        key, separator, entity = mapping.partition("=")
        if separator and key.strip() == device_id:
            return entity.strip()
    return os.environ.get("TIMER_RING_ENTITY", "").strip() if allow_legacy else ""


async def _set_ring(on: bool, device_id: str = "", allow_legacy: bool = True) -> bool:
    entity = _ring_entity(device_id, allow_legacy)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not entity or not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"http://supervisor/core/api/services/switch/turn_{'on' if on else 'off'}",
                headers={"Authorization": f"Bearer {token}"},
                json={"entity_id": entity},
            )
            r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"⚠️ timer ring toggle failed ({entity}): {e!r}")
        return False


class TimerRegistry:
    def __init__(self):
        self._timers: Dict[int, dict] = {}
        self._next_id = 1
        # Wired by main.py. Every callback accepts the timer's device ID so an
        # expiry stays in the room where it was created.
        self.announcer = None
        self.get_owner = None
        self.last_wake = None
        self.allow_legacy_ring = None

    def _prune(self):
        now = time.monotonic()
        for tid in [t for t, v in self._timers.items() if v["ends"] <= now and v["task"].done()]:
            self._timers.pop(tid, None)

    async def _fire(self, tid: int):
        t = self._timers.get(tid)
        if not t:
            return
        try:
            # The speaker verdict often lands a few seconds AFTER set_timer's
            # tool call (probe needs mic audio) — re-capture the owner once.
            wait = t["ends"] - time.monotonic()
            if not t.get("owner") and self.get_owner is not None and wait > 8:
                await asyncio.sleep(6)
                try:
                    t["owner"] = (self.get_owner(t["device_id"]) or "").strip().lower()
                except Exception:
                    pass
                wait = t["ends"] - time.monotonic()
            await asyncio.sleep(max(0.0, wait))
        except asyncio.CancelledError:
            return
        owner = t.get("owner") or ""
        label = t["label"]
        # "timer 3" default labels make clumsy sentences ("your timer 3 timer")
        nice = "" if re.fullmatch(r"timer \d+", label) else f"{label} "
        logger.info(f"⏰ timer {tid} ('{label}', owner={owner or '-'}) expired")
        # 1. One personal spoken announcement (no nagging nudges).
        announced = False
        if self.announcer is not None:
            try:
                who = f"{owner.capitalize()}, y" if owner else "Y"
                announced = await self.announcer(
                    f"{who}our {nice}timer is done.", t["device_id"]
                )
            except Exception as e:
                logger.warning(f"⚠️ timer announcement failed: {e!r}")
        # 2. Grace: a wake from the originating device acknowledges it.
        if announced:
            t0 = time.monotonic()
            await asyncio.sleep(ANNOUNCE_GRACE_S)
            if self.last_wake is not None and self.last_wake(t["device_id"]) > t0:
                logger.info(f"⏰ timer {tid} acknowledged by wake — no ring")
                self._timers.pop(tid, None)
                return
        # 3. The gentle bell (auto-off backstop unchanged).
        logger.info(f"⏰ timer {tid} escalating to ring")
        allow_legacy = (
            self.allow_legacy_ring(t["device_id"])
            if self.allow_legacy_ring is not None else True
        )
        if await _set_ring(True, t["device_id"], allow_legacy):
            await asyncio.sleep(RING_AUTO_OFF_S)
            await _set_ring(False, t["device_id"], allow_legacy)
        self._timers.pop(tid, None)

    def set_timer(self, seconds: int, label: str, owner: str = "", device_id: str = "") -> dict:
        self._prune()
        if sum(t["device_id"] == device_id for t in self._timers.values()) >= MAX_TIMERS:
            return {"error": "too many timers running"}
        seconds = max(5, min(int(seconds), MAX_DURATION_S))
        tid = self._next_id
        self._next_id += 1
        self._timers[tid] = {
            "owner": (owner or "").strip().lower(),
            "device_id": device_id,
            "label": label or f"timer {tid}",
            "ends": time.monotonic() + seconds,
            "wall": time.time() + seconds,
            "task": asyncio.get_running_loop().create_task(self._fire(tid)),
        }
        logger.info(f"⏰ timer {tid} set: {seconds}s ('{label}')")
        try:
            import asyncio as _a
            from .ha_sensors import PUBLISHER
            _a.get_running_loop().create_task(PUBLISHER.timers(self, device_id))
        except Exception:
            pass
        return {"id": tid, "label": self._timers[tid]["label"], "seconds": seconds}

    def cancel(self, tid: Optional[int], device_id: str = "") -> dict:
        self._prune()
        timers = self.list_timers(device_id)["timers"]
        if tid is None:
            if len(timers) == 1:
                tid = timers[0]["id"]
            elif not timers:
                return {"error": "no timers running"}
            else:
                return {"error": "multiple timers running — need the id",
                        "timers": timers}
        t = self._timers.get(int(tid))
        if not t or t["device_id"] != device_id:
            return {"error": f"no timer {tid}"}
        del self._timers[int(tid)]
        t["task"].cancel()
        logger.info(f"⏰ timer {tid} cancelled")
        try:
            import asyncio as _a
            from .ha_sensors import PUBLISHER
            _a.get_running_loop().create_task(PUBLISHER.timers(self, device_id))
        except Exception:
            pass
        return {"cancelled": tid, "label": t["label"]}

    def list_timers(self, device_id: Optional[str] = None) -> dict:
        self._prune()
        now = time.monotonic()
        return {"timers": [
            {"id": tid, "label": t["label"], "seconds_left": int(t["ends"] - now)}
            for tid, t in sorted(self._timers.items())
            if device_id is None or t["device_id"] == device_id
        ]}


def get_timer_tool_definitions() -> list:
    return [
        {"type": "function", "name": "set_timer",
         "description": ("Set a countdown timer. The device will ring when it expires "
                         "(user silences it with the button or by saying 'stop'). "
                         "Use for any 'set a timer', 'remind me in N minutes' request."),
         "parameters": {"type": "object", "properties": {
             "seconds": {"type": "integer", "description": "Duration in seconds"},
             "label": {"type": "string", "description": "Short label, e.g. 'pasta'"}},
             "required": ["seconds"]}},
        {"type": "function", "name": "cancel_timer",
         "description": "Cancel a running timer. Omit id if only one is running.",
         "parameters": {"type": "object", "properties": {
             "id": {"type": "integer", "description": "Timer id (optional)"}}}},
        {"type": "function", "name": "list_timers",
         "description": "List running timers with time remaining.",
         "parameters": {"type": "object", "properties": {}}},
    ]


def register_timer_tools(llm, registry: "TimerRegistry", device_id: str) -> None:
    async def _set(params: "FunctionCallParams") -> None:
        a = params.arguments or {}
        owner = ""
        if registry.get_owner is not None:
            try:
                owner = registry.get_owner(device_id) or ""
            except Exception:
                pass
        await params.result_callback(registry.set_timer(
            a.get("seconds", 0), (a.get("label") or "").strip(), owner, device_id
        ))

    async def _cancel(params: "FunctionCallParams") -> None:
        a = params.arguments or {}
        await params.result_callback(registry.cancel(a.get("id"), device_id))

    async def _list(params: "FunctionCallParams") -> None:
        await params.result_callback(registry.list_timers(device_id))

    llm.register_function("set_timer", _set)
    llm.register_function("cancel_timer", _cancel)
    llm.register_function("list_timers", _list)
