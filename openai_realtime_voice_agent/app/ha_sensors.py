"""Publish live Voice PE state to Home Assistant as sensors.

Per-instance sensors (INSTANCE_NAME option, e.g. 'kitchen'):
  sensor.voicepe_<inst>_speaker        recognized name/unknown/none (+score/method)
  sensor.voicepe_<inst>_active_timers  count (+next-expiry attrs)
  binary_sensor.voicepe_<inst>_enrollment_active
  sensor.voicepe_<inst>_wakes_today / _false_wakes_today
  sensor.voicepe_<inst>_latency        last turn speech-end -> first reply audio
                                       sent (ms), with p50/p90 attributes
  sensor.voicepe_<inst>_wake_word      active wake model, cutoff and window as
                                       reported by the device

States are POSTed via the supervisor core API — ad-hoc entities, ideal for
dashboards and automations.

Publishing never blocks the caller. Every post goes through one shared HTTP
client and one background worker that keeps only the newest state per
entity, so a slow or unreachable Home Assistant can delay sensor updates but
can never stall the device's audio path (the wake handler used to await an
HTTP POST with an 8 s timeout inside the WebSocket receive loop).
"""
import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_INST = os.environ.get("INSTANCE_NAME", "").strip().lower() or "device"
_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_BASE_URL = "http://supervisor/core/api"
# Daily counters survive add-on restarts. /data is the add-on's persistent
# volume; outside the add-on (tests, local runs) persistence is skipped.
_COUNTER_PATH = os.environ.get("VOICEPE_COUNTER_PATH", "/data/voicepe_counters.json")


class StatePoster:
    """Coalescing, non-blocking poster for Home Assistant entity states.

    ``post_nowait`` records the newest (state, attributes) for an entity and
    wakes a single worker task; the worker posts each pending entity once
    with a shared ``httpx.AsyncClient``. Bursts collapse to the latest value,
    concurrency is bounded to one request at a time, and callers never await
    the network.
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        token_getter: Callable[[], str] = lambda: _TOKEN,
        timeout_s: float = 3.0,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_getter = token_getter
        self._timeout_s = timeout_s
        self._client_factory = client_factory
        self._client = None
        self._pending: Dict[str, Tuple[str, dict]] = {}
        self._wakeup: Optional[asyncio.Event] = None
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.posted = 0
        self.failed = 0

    def _ensure_worker(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._loop is not loop or self._worker is None or self._worker.done():
            self._loop = loop
            self._wakeup = asyncio.Event()
            self._worker = loop.create_task(self._run())
        return True

    def post_nowait(self, entity: str, state: Any, attrs: Optional[dict] = None) -> None:
        """Queue a state update; returns immediately."""
        if not self._token_getter():
            return
        self._pending[entity] = (str(state), dict(attrs or {}))
        if self._ensure_worker():
            self._wakeup.set()

    async def flush(self, timeout_s: float = 5.0) -> None:
        """Wait until queued updates have been attempted (tests, shutdown)."""
        deadline = time.monotonic() + timeout_s
        while self._pending and time.monotonic() < deadline:
            if self._wakeup is not None:
                self._wakeup.set()
            await asyncio.sleep(0.01)

    def _get_client(self):
        if self._client is None:
            factory = self._client_factory or (lambda: httpx.AsyncClient(timeout=self._timeout_s))
            self._client = factory()
        return self._client

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while self._pending:
                entity, (state, attrs) = self._pending.popitem()
                await self._post(entity, state, attrs)

    async def _post(self, entity: str, state: str, attrs: dict) -> None:
        try:
            client = self._get_client()
            response = await client.post(
                f"{self._base_url}/states/{entity}",
                headers={"Authorization": f"Bearer {self._token_getter()}"},
                json={"state": state, "attributes": attrs},
            )
            response.raise_for_status()
            self.posted += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.failed += 1
            logger.debug(f"sensor post failed ({entity}): {e!r}")

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


class DailyCounters:
    """Per-day wake / false-wake counts that survive restarts and roll at midnight."""

    def __init__(self, path: Optional[str] = _COUNTER_PATH, today: Callable[[], str] = None):
        self._path = path
        self._today = today or (lambda: date.today().isoformat())
        self.day = self._today()
        self.counts: Dict[str, int] = {}
        self.by_device: Dict[str, Dict[str, int]] = {}
        # One writer thread keeps persisted snapshots in submission order.
        self._writer: Optional[ThreadPoolExecutor] = None
        self._load()

    def _load(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("day") == self.day:
            self.counts = {k: int(v) for k, v in (data.get("counts") or {}).items()}
            self.by_device = {
                d: {k: int(v) for k, v in c.items()}
                for d, c in (data.get("by_device") or {}).items()
            }

    def _write(self, snapshot: dict) -> None:
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.debug(f"counter persist failed: {e!r}")

    def _save(self) -> None:
        """Persist off the event loop: a slow SD card must not stall audio."""
        if not self._path or not os.path.isdir(os.path.dirname(self._path) or "."):
            return
        snapshot = {
            "day": self.day,
            "counts": dict(self.counts),
            "by_device": {d: dict(c) for d, c in self.by_device.items()},
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write(snapshot)
            return
        if self._writer is None:
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voicepe-counters")
        loop.run_in_executor(self._writer, self._write, snapshot)

    def roll(self) -> bool:
        """Reset at a day boundary. Returns True when a rollover happened."""
        today = self._today()
        if today == self.day:
            return False
        self.day, self.counts, self.by_device = today, {}, {}
        self._save()
        return True

    def increment(self, name: str, device_id: Optional[str] = None) -> int:
        self.roll()
        self.counts[name] = self.counts.get(name, 0) + 1
        if device_id:
            per = self.by_device.setdefault(device_id, {})
            per[name] = per.get(name, 0) + 1
        self._save()
        return self.counts[name]

    def get(self, name: str) -> int:
        self.roll()
        return self.counts.get(name, 0)

    def device_counts(self, name: str) -> Dict[str, int]:
        return {d: c.get(name, 0) for d, c in self.by_device.items() if c.get(name)}


class SensorPublisher:
    def __init__(self, poster: Optional[StatePoster] = None, counters: Optional[DailyCounters] = None):
        self.poster = poster or StatePoster()
        self.counters = counters or DailyCounters()
        self._cost_today = 0.0
        self._responses_today = 0
        self._cost_day = self.counters.day
        self._rollover_task: Optional[asyncio.Task] = None

    def _post(self, entity: str, state, attrs: dict) -> None:
        self._ensure_rollover_task()
        self.poster.post_nowait(entity, state, attrs)

    def _ensure_rollover_task(self) -> None:
        """Republish zeroed day counters at midnight even without new events."""
        if self._rollover_task is not None and not self._rollover_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._rollover_task = loop.create_task(self._rollover_loop())

    async def _rollover_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self.counters.roll():
                self._publish_counter("wakes", "wakes today")
                self._publish_counter("false_wakes", "false wakes today")

    def _publish_counter(self, name: str, label: str) -> None:
        self.poster.post_nowait(
            f"sensor.voicepe_{_INST}_{name}_today",
            self.counters.get(name),
            {
                "friendly_name": f"Voice PE {_INST} {label}",
                "by_device": self.counters.device_counts(name),
                "day": self.counters.day,
            },
        )

    async def speaker(self, label: str, name, score: float, method: str):
        state = name or ("unknown" if label == "unknown" else "none")
        self._post(f"sensor.voicepe_{_INST}_speaker", state, {
            "friendly_name": f"Voice PE {_INST} speaker",
            "label": label, "score": round(float(score), 3), "method": method,
            "at": time.strftime("%H:%M:%S"),
        })

    async def usage(self, cost: float, detail: dict):
        """Accumulate estimated OpenAI spend and publish it as a sensor."""
        self.counters.roll()
        if self._cost_day != self.counters.day:
            self._cost_today, self._responses_today = 0.0, 0
            self._cost_day = self.counters.day
        self._cost_today += cost
        self._responses_today += 1
        self._post(f"sensor.voicepe_{_INST}_openai_cost_today", round(self._cost_today, 4), {
            "friendly_name": f"Voice PE {_INST} OpenAI cost today",
            "unit_of_measurement": "$", "responses_today": self._responses_today,
            "last_response_cost": round(cost, 5), **detail,
        })

    async def voice_prints(self):
        """Publish enrolled voice prints so users can SEE enrollment worked."""
        import glob
        names = []
        for f in sorted(glob.glob("/share/voice-prints/*.json")):
            try:
                with open(f) as fh:
                    d = json.load(fh)
                names.append({"name": d.get("name") or os.path.basename(f)[:-5],
                              "chunks": d.get("chunks", 0)})
            except Exception:
                continue
        configured = [n for n in (os.environ.get("SPEAKER_MALE_NAME", ""),
                                  os.environ.get("SPEAKER_FEMALE_NAME", "")) if n.strip()]
        self._post(f"sensor.voicepe_{_INST}_voice_prints", len(names), {
            "friendly_name": f"Voice PE {_INST} enrolled voice prints",
            "enrolled": [n["name"] for n in names],
            "chunks": {n["name"]: n["chunks"] for n in names},
            "configured_names": configured,
            "active": [n["name"] for n in names
                       if n["name"].lower() in {c.strip().lower() for c in configured}],
        })

    async def wake(self, device_id: Optional[str] = None):
        self.counters.increment("wakes", device_id)
        self._publish_counter("wakes", "wakes today")

    async def false_wake(self, device_id: Optional[str] = None):
        self.counters.increment("false_wakes", device_id)
        self._publish_counter("false_wakes", "false wakes today")

    async def timers(self, registry):
        t = registry.list_timers()["timers"]
        attrs = {"friendly_name": f"Voice PE {_INST} active timers"}
        if t:
            attrs["next_label"] = t[0]["label"]
            attrs["next_seconds_left"] = min(x["seconds_left"] for x in t)
        self._post(f"sensor.voicepe_{_INST}_active_timers", len(t), attrs)

    async def enrollment(self, active: bool):
        self._post(f"binary_sensor.voicepe_{_INST}_enrollment_active",
                   "on" if active else "off",
                   {"friendly_name": f"Voice PE {_INST} enrollment active"})

    def latency(self, summary: dict, stats: dict) -> None:
        """Publish the newest turn timeline and rolling percentiles (numbers only)."""
        state = summary.get("speech_end_to_first_audio_sent_ms")
        self._post(f"sensor.voicepe_{_INST}_latency", state if state is not None else "unknown", {
            "friendly_name": f"Voice PE {_INST} reply latency",
            "unit_of_measurement": "ms",
            "device_id": summary.get("device_id"),
            "turn_id": summary.get("turn_id"),
            "outcome": summary.get("outcome"),
            "last_turn": summary.get("intervals", {}),
            "device": summary.get("device", {}),
            "tools": summary.get("tools", []),
            "stats": stats,
        })

    def wake_model(self, device_id: str, meta: dict) -> None:
        """Publish the wake model/operating point the device reports using."""
        self._post(f"sensor.voicepe_{_INST}_wake_word", meta.get("model") or "unknown", {
            "friendly_name": f"Voice PE {_INST} wake word",
            "device_id": device_id,
            **{k: meta[k] for k in ("model", "model_sha", "cutoff", "cutoff_uint8", "window",
                                    "tier", "fw") if k in meta},
        })


PUBLISHER = SensorPublisher()
