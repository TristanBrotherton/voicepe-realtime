"""The "Idag:" block: date, time, weather and the next events, in the prompt.

A weather question took 6 s on Gemini, 3.7 s of it before the tool call
(2026-10-02). With today's facts already in the system instruction the model
answers "vad är klockan", "vad blir det för väder" and "vad händer i morgon"
without a tool round. The calendar scripts' own descriptions already tell the
model to compute dates "ur raden Idag: i systemprompten" -- a line that did
not exist until now.

Weather and calendar come from the same HA scripts the model calls
(script__vaderprognos, script__kalender_sok), through comms' MCP door. A
failed fetch keeps the last good part; the time is rendered fresh every time
the block is.
"""
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app import ha_api

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Stockholm")
WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
CONDITIONS = {
    "clear-night": "klart", "sunny": "sol", "partlycloudy": "halvklart",
    "cloudy": "mulet", "fog": "dimma", "rainy": "regn", "pouring": "skyfall",
    "lightning": "åska", "lightning-rainy": "åska och regn", "snowy": "snö",
    "snowy-rainy": "snöblandat regn", "hail": "hagel", "windy": "blåsigt",
    "windy-variant": "blåsigt", "exceptional": "ovanligt väder",
}
WEATHER_DAYS = 3  # idag, i morgon, i övermorgon: a Friday covers the weekend
EVENTS = 3


async def call_ha_tool(name: str, arguments: Optional[dict] = None, timeout: float = 8.0) -> Any:
    """One HA MCP tool call through comms; returns the script's `result`."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            ha_api.url("/mcp"),
            headers={**ha_api.headers(), "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments or {}}},
        )
        r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{name}: {body['error']}")
    payload = json.loads(body["result"]["content"][0]["text"])
    if not payload.get("success", True):
        raise RuntimeError(f"{name}: {payload}")
    return payload.get("result", payload)


def _local(value: str) -> Optional[datetime]:
    """An ISO time from HA (Z, offset or naive local) in Stockholm time; None for a date."""
    if len(value) == 10:
        return None
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t.astimezone(TZ) if t.tzinfo else t.replace(tzinfo=TZ)


def _day_name(day: date, today: date) -> str:
    if day == today:
        return "idag"
    if day == today + timedelta(days=1):
        return "i morgon"
    return f"{WEEKDAYS[day.weekday()]} {day.day}/{day.month}"


def parse_weather(result: Dict[str, Any]) -> List[Tuple[date, str]]:
    """SMHI's daily forecast -> [(day, "halvklart 13–16°, 0.3 mm, vind 3 m/s"), ...]."""
    forecast = next(
        (v["forecast"] for v in result.values() if isinstance(v, dict) and "forecast" in v), []
    )
    days = []
    for f in forecast:
        text = CONDITIONS.get(f.get("condition"), f.get("condition") or "")
        if f.get("templow") is not None:
            text += f" {round(f['templow'])}–{round(f['temperature'])}°"
        elif f.get("temperature") is not None:
            text += f" {round(f['temperature'])}°"
        if f.get("precipitation"):
            text += f", {f['precipitation']} mm"
        if f.get("wind_speed") is not None:
            text += f", vind {round(f['wind_speed'] / 3.6)} m/s"
        days.append((_local(f["datetime"]).date(), text))
    return days


def parse_events(result: Dict[str, Any]) -> List[Tuple[datetime, bool, str]]:
    """kalender_sok's hits -> [(start, has_clock, title)], soonest first."""
    out = []
    for e in result.get("handelser", []):
        start = e.get("borjar", "")
        when = _local(start)
        timed = when is not None
        if not timed:
            when = datetime.combine(date.fromisoformat(start), datetime.min.time(), TZ)
        out.append((when, timed, " ".join(str(e.get("vad", "")).split())[:60]))
    return sorted(out, key=lambda e: e[0])


class Idag:
    """The cached facts, and the block rendered from them."""

    def __init__(self, call=call_ha_tool):
        self._call = call
        self.weather: Optional[List[Tuple[date, str]]] = None
        self.events: Optional[List[Tuple[datetime, bool, str]]] = None
        self.fetched_at: Optional[datetime] = None

    async def refresh(self, now: Optional[datetime] = None) -> None:
        """Fetch both parts; a part that fails keeps its last good value."""
        now = now or datetime.now(TZ)
        try:
            self.weather = parse_weather(await self._call("script__vaderprognos"))
        except Exception as e:
            logger.warning(f"⚠️ Idag: weather not refreshed, keeping the last ({e!r})")
        try:
            self.events = parse_events(await self._call("script__kalender_sok"))
        except Exception as e:
            logger.warning(f"⚠️ Idag: calendar not refreshed, keeping the last ({e!r})")
        self.fetched_at = now

    def block(self, now: Optional[datetime] = None) -> str:
        """The lines that go at the end of the system instruction."""
        now = now or datetime.now(TZ)
        lines = [
            f"Idag: {WEEKDAYS[now.weekday()]} {now:%Y-%m-%d}, klockan {now:%H:%M} "
            f"(svensk tid, gäller när samtalet öppnades)."
        ]
        today = now.date()
        # Day names are worked out now, not at fetch time: "i morgon" fetched
        # at 23:55 is "idag" at 00:05.
        weather = [f"{_day_name(d, today)} {t}" for d, t in self.weather or [] if d >= today]
        if weather:
            lines.append("Väder (SMHI): " + "; ".join(weather[:WEATHER_DAYS]) + ".")
        events = [
            f"{_day_name(w.date(), today)}{f' {w:%H:%M}' if timed else ''} {title}"
            for w, timed, title in self.events or []
            if (w >= now if timed else w.date() >= today)
        ]
        if events:
            lines.append("Kalendern härnäst: " + "; ".join(events[:EVENTS]) + ".")
        lines.append(
            "Klockan, vädret och kalendern ovan svarar du på direkt, utan verktyg. "
            "Annat väder, andra dagar eller detaljer: använd verktygen."
        )
        return "\n\n" + "\n".join(lines)
