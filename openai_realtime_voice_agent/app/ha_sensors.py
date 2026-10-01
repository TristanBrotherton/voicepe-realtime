"""Publish live Voice PE state to Home Assistant as sensors.

Per-instance sensors (INSTANCE_NAME option, e.g. 'kitchen'):
  sensor.voicepe_<inst>_speaker        james/mary/unknown/none (+score/method)
  sensor.voicepe_<inst>_active_timers  count (+next-expiry attrs)
  binary_sensor.voicepe_<inst>_enrollment_active
  sensor.voicepe_<inst>_wakes_today / _false_wakes_today
  sensor.voicepe_<inst>_motor            which engine is answering + why

States are POSTed to Home Assistant's REST API through raawr-comms (app/ha_api.py) — ad-hoc entities, ideal for
dashboards and automations (e.g. per-person scenes on speaker change).
"""
import logging
import os
import time
from datetime import date

import httpx

from app import ha_api

logger = logging.getLogger(__name__)

_INST = os.environ.get("INSTANCE_NAME", "").strip().lower() or "device"


async def _post(entity: str, state, attrs: dict) -> bool:
    """Write one entity state to the supervisor. Returns whether it landed.

    A sensor write is never worth interrupting a conversation over, so a
    failure is still swallowed at debug level exactly as before -- but it is
    now also REPORTED, because `SensorPublisher.provider` de-duplicates its
    writes and must not remember a state it never actually managed to
    publish. No comms address or key configured means there is nowhere to
    write to: nothing was written, so that is False too.
    """
    if not ha_api.configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                ha_api.url(f"/states/{entity}"),
                headers=ha_api.headers(),
                json={"state": str(state), "attributes": attrs},
            )
            r.raise_for_status()
    except Exception as e:
        logger.debug(f"sensor post failed ({entity}): {e!r}")
        return False
    return True


class SensorPublisher:
    def __init__(self):
        self._day = date.today().isoformat()
        self._wakes = 0
        self._false = 0
        self._last_provider_key = None

    def _roll(self):
        d = date.today().isoformat()
        if d != self._day:
            self._day, self._wakes, self._false = d, 0, 0

    async def speaker(self, label: str, name, score: float, method: str):
        state = name or ("unknown" if label == "unknown" else "none")
        await _post(f"sensor.voicepe_{_INST}_speaker", state, {
            "friendly_name": f"Voice PE {_INST} speaker",
            "label": label, "score": round(float(score), 3), "method": method,
            "at": time.strftime("%H:%M:%S"),
        })

    async def provider(self, status: dict):
        """Publish which voice engine is answering, and why.

        A switch is invisible otherwise: it shows up only in the add-on log,
        and an engine change that quietly bills the other account for three
        days is precisely the fault nobody catches.

        This is called once per device (re)connect, which can be often --
        Voice PE devices drop and reconnect on their own. `retry_primary_in_s`
        ticks down every second while the backup is in cooldown, so comparing
        on that field would defeat any de-dup; instead we key on the fields
        that only change when the engine actually switches (or switches
        back), and skip the HA write when nothing meaningful moved.

        Caveat: because of that de-dup, `retry_primary_in_s` is only ever as
        fresh as the last publish. A device that stays connected through an
        entire cooldown window sees this number freeze at whatever it was on
        connect -- which engine is live and why it switched stay correct,
        only the countdown can go stale. Don't go bug-hunting over that.

        FIX (final review): the key is recorded only AFTER the write actually
        succeeds. It used to be stored first, and `_post` swallows every
        failure -- so an engine switch that happened while the supervisor was
        briefly unreachable (an HA restart, which is a common reason the
        device reconnected in the first place) was lost for good: the write
        failed silently, the key was already stored, and every later connect
        deduped the retry away. The sensor then showed the OLD engine for the
        whole cooldown -- the exact invisibility this entity exists to end.
        Retrying on the next connect costs one HTTP call that would not
        otherwise have happened, and only while the supervisor is down.
        """
        key = (
            status.get("provider"),
            status.get("reason"),
            status.get("primary"),
            status.get("backup"),
            status.get("switched_at"),
        )
        if key == self._last_provider_key:
            return
        published = await _post(f"sensor.voicepe_{_INST}_motor", status.get("provider", ""), {
            "friendly_name": f"Voice PE {_INST} motor",
            "icon": "mdi:swap-horizontal",
            "primary": status.get("primary", ""),
            "backup": status.get("backup") or "",
            "reason": status.get("reason", ""),
            "switched_at": status.get("switched_at", 0.0),
            "retry_primary_in_s": status.get("retry_primary_in_s", 0.0),
            "at": time.strftime("%H:%M:%S"),
        })
        if published:
            self._last_provider_key = key

    async def usage(self, cost: float, detail: dict):
        """Accumulate estimated OpenAI spend and publish it as a sensor."""
        self._roll()
        self._cost_today = getattr(self, "_cost_today", 0.0)
        self._responses_today = getattr(self, "_responses_today", 0)
        if getattr(self, "_cost_day", self._day) != self._day:
            self._cost_today, self._responses_today = 0.0, 0
        self._cost_day = self._day
        self._cost_today += cost
        self._responses_today += 1
        await _post(f"sensor.voicepe_{_INST}_openai_cost_today", round(self._cost_today, 4), {
            "friendly_name": f"Voice PE {_INST} OpenAI cost today",
            "unit_of_measurement": "$", "responses_today": self._responses_today,
            "last_response_cost": round(cost, 5), **detail,
        })

    async def voice_prints(self):
        """Publish enrolled voice prints so users can SEE enrollment worked."""
        import glob, json as _json
        names = []
        for f in sorted(glob.glob("/share/voice-prints/*.json")):
            try:
                d = _json.load(open(f))
                names.append({"name": d.get("name") or os.path.basename(f)[:-5],
                              "chunks": d.get("chunks", 0)})
            except Exception:
                continue
        configured = [n for n in (os.environ.get("SPEAKER_MALE_NAME", ""),
                                  os.environ.get("SPEAKER_FEMALE_NAME", "")) if n.strip()]
        await _post(f"sensor.voicepe_{_INST}_voice_prints", len(names), {
            "friendly_name": f"Voice PE {_INST} enrolled voice prints",
            "enrolled": [n["name"] for n in names],
            "chunks": {n["name"]: n["chunks"] for n in names},
            "configured_names": configured,
            "active": [n["name"] for n in names
                       if n["name"].lower() in {c.strip().lower() for c in configured}],
        })

    async def wake(self):
        self._roll(); self._wakes += 1
        await _post(f"sensor.voicepe_{_INST}_wakes_today", self._wakes,
                    {"friendly_name": f"Voice PE {_INST} wakes today"})

    async def false_wake(self):
        self._roll(); self._false += 1
        await _post(f"sensor.voicepe_{_INST}_false_wakes_today", self._false,
                    {"friendly_name": f"Voice PE {_INST} false wakes today"})

    async def timers(self, registry):
        t = registry.list_timers()["timers"]
        attrs = {"friendly_name": f"Voice PE {_INST} active timers"}
        if t:
            attrs["next_label"] = t[0]["label"]
            attrs["next_seconds_left"] = min(x["seconds_left"] for x in t)
        await _post(f"sensor.voicepe_{_INST}_active_timers", len(t), attrs)

    async def enrollment(self, active: bool):
        await _post(f"binary_sensor.voicepe_{_INST}_enrollment_active",
                    "on" if active else "off",
                    {"friendly_name": f"Voice PE {_INST} enrollment active"})


PUBLISHER = SensorPublisher()
