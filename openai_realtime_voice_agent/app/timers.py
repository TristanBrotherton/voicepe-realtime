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


async def _set_ring(on: bool) -> bool:
    entity = os.environ.get("TIMER_RING_ENTITY", "").strip()
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

    def _prune(self):
        now = time.monotonic()
        for tid in [t for t, v in self._timers.items() if v["ends"] <= now and v["task"].done()]:
            self._timers.pop(tid, None)

    async def _fire(self, tid: int):
        t = self._timers.get(tid)
        if not t:
            return
        try:
            await asyncio.sleep(max(0.0, t["ends"] - time.monotonic()))
        except asyncio.CancelledError:
            return
        logger.info(f"⏰ timer {tid} ('{t['label']}') expired — ringing")
        if await _set_ring(True):
            # Auto-silence backstop; the button/"stop" path turns the switch
            # off on-device well before this in normal use.
            await asyncio.sleep(RING_AUTO_OFF_S)
            await _set_ring(False)
        self._timers.pop(tid, None)

    def set_timer(self, seconds: int, label: str) -> dict:
        self._prune()
        if len(self._timers) >= MAX_TIMERS:
            return {"error": "too many timers running"}
        seconds = max(5, min(int(seconds), MAX_DURATION_S))
        tid = self._next_id
        self._next_id += 1
        self._timers[tid] = {
            "label": label or f"timer {tid}",
            "ends": time.monotonic() + seconds,
            "wall": time.time() + seconds,
            "task": asyncio.get_running_loop().create_task(self._fire(tid)),
        }
        logger.info(f"⏰ timer {tid} set: {seconds}s ('{label}')")
        try:
            import asyncio as _a
            from .ha_sensors import PUBLISHER
            _a.get_running_loop().create_task(PUBLISHER.timers(self))
        except Exception:
            pass
        return {"id": tid, "label": self._timers[tid]["label"], "seconds": seconds}

    def cancel(self, tid: Optional[int]) -> dict:
        self._prune()
        if tid is None:
            if len(self._timers) == 1:
                tid = next(iter(self._timers))
            elif not self._timers:
                return {"error": "no timers running"}
            else:
                return {"error": "multiple timers running — need the id",
                        "timers": self.list_timers()["timers"]}
        t = self._timers.pop(int(tid), None)
        if not t:
            return {"error": f"no timer {tid}"}
        t["task"].cancel()
        logger.info(f"⏰ timer {tid} cancelled")
        try:
            import asyncio as _a
            from .ha_sensors import PUBLISHER
            _a.get_running_loop().create_task(PUBLISHER.timers(self))
        except Exception:
            pass
        return {"cancelled": tid, "label": t["label"]}

    def list_timers(self) -> dict:
        self._prune()
        now = time.monotonic()
        return {"timers": [
            {"id": tid, "label": t["label"], "seconds_left": int(t["ends"] - now)}
            for tid, t in sorted(self._timers.items())
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


def register_timer_tools(llm, registry: "TimerRegistry") -> None:
    async def _set(params: "FunctionCallParams") -> None:
        a = params.arguments or {}
        await params.result_callback(registry.set_timer(a.get("seconds", 0), (a.get("label") or "").strip()))

    async def _cancel(params: "FunctionCallParams") -> None:
        a = params.arguments or {}
        await params.result_callback(registry.cancel(a.get("id")))

    async def _list(params: "FunctionCallParams") -> None:
        await params.result_callback(registry.list_timers())

    llm.register_function("set_timer", _set)
    llm.register_function("cancel_timer", _cancel)
    llm.register_function("list_timers", _list)
