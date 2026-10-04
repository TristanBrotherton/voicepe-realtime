"""Wake events: device- and turn-scoped false-wake labels with a bounded archive.

Every wake the device reports becomes a ``WakeEvent`` keyed by device id and
turn id. Feedback ("that was a false alarm", a fast button press, a double
press) labels exactly one event: the newest wake *on the device that sent the
feedback* within ``flag_window_s`` — or the exact turn id the firmware names.
The previous implementation renamed the globally newest ``probe_*.wav``,
which could hit another room's capture or an earlier genuine wake, and a
double press had no time limit at all.

What is stored is an explicit, documented choice (``WAKE_CAPTURE``):

* ``off``      — nothing is written, not even metadata (guest mode).
* ``metadata`` — counters and a bounded JSONL log of wake metadata (time,
                 device, turn, model, cutoff, window, outcome, label). No audio.
* ``audio``    — metadata plus the post-wake capture and, when the firmware
                 opts in, the pre-wake trigger snippet. Raw household audio:
                 stays on the Home Assistant host; never uploaded by the add-on.
* ``auto``     — the backwards-compatible default: ``audio`` when the legacy
                 ``enable_recording`` option is on, otherwise ``metadata``.

A guest-mode entity (``GUEST_MODE_ENTITY``, e.g. an ``input_boolean``) turns
all storage off while it is ``on``.

Retention never sacrifices labels for unlabeled clips: unlabeled captures are
pruned first (count cap + TTL), labeled false wakes have their own, longer
TTL and cap. Metadata sidecars live in a ``meta/`` subdirectory so the
offline trainer's ``falsewake_*`` harvest glob still matches audio only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

PROBE_DIR = "/share/voice-probes"
META_SUBDIR = "meta"
SAMPLE_RATE = 16000
UNLABELED_PREFIXES = ("probe_", "trigger_", "candidate_")
LABELED_PREFIX = "falsewake_"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_token(raw: object, limit: int = 40) -> str:
    """Reduce an untrusted id to a filename/log-safe token."""
    return _SAFE.sub("", str(raw or ""))[:limit]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class CaptureConfig:
    mode: str = "metadata"
    probe_dir: str = PROBE_DIR
    unlabeled_ttl_days: int = 30
    labeled_ttl_days: int = 180
    max_unlabeled: int = 500
    max_labeled: int = 1000
    flag_window_s: float = 30.0
    queued_flag_window_s: float = 600.0
    max_events_logged: int = 5000

    @property
    def stores_metadata(self) -> bool:
        return self.mode in ("metadata", "audio")

    @property
    def stores_audio(self) -> bool:
        return self.mode == "audio"

    @classmethod
    def from_env(cls) -> "CaptureConfig":
        mode = os.environ.get("WAKE_CAPTURE", "auto").strip().lower() or "auto"
        if mode not in ("off", "metadata", "audio", "auto"):
            logger.warning(f"⚠️ unknown WAKE_CAPTURE={mode!r}; using metadata-only")
            mode = "metadata"
        if mode == "auto":
            legacy_audio = os.environ.get("ENABLE_RECORDING", "false").strip().lower() == "true"
            mode = "audio" if legacy_audio else "metadata"
        return cls(
            mode=mode,
            probe_dir=os.environ.get("WAKE_CAPTURE_DIR", PROBE_DIR),
            unlabeled_ttl_days=_env_int("WAKE_CAPTURE_TTL_DAYS", 30),
            labeled_ttl_days=_env_int("WAKE_LABEL_TTL_DAYS", 180),
            max_unlabeled=_env_int("WAKE_CAPTURE_MAX_FILES", 500),
            flag_window_s=float(_env_int("FALSE_WAKE_FLAG_WINDOW_S", 30)),
        )


@dataclass
class WakeEvent:
    device_id: str
    turn_id: str
    t_mono: float
    wall: float
    source: str = "wake_word"
    model: str = ""
    model_sha: str = ""
    cutoff: Optional[float] = None
    window: Optional[int] = None
    tier: str = ""
    outcome: str = ""
    label: str = ""
    label_method: str = ""
    labeled_at: Optional[float] = None
    reply_audio_before_flag: Optional[bool] = None
    probe_path: str = ""
    trigger_path: str = ""
    stem: str = ""

    def public_dict(self) -> dict:
        """Metadata safe to log/persist: no audio, no transcript, no paths."""
        d = asdict(self)
        d.pop("t_mono", None)
        d.pop("probe_path", None)
        d.pop("trigger_path", None)
        d["has_probe"] = bool(self.probe_path)
        d["has_trigger"] = bool(self.trigger_path)
        return d


class ProbeArchive:
    """Writes captures and enforces label-preserving retention."""

    def __init__(self, config: CaptureConfig, now: Callable[[], float] = time.time):
        self.config = config
        self._now = now

    @property
    def meta_dir(self) -> str:
        return os.path.join(self.config.probe_dir, META_SUBDIR)

    def write_wav(self, name: str, pcm: bytes, rate: int = SAMPLE_RATE) -> str:
        os.makedirs(self.config.probe_dir, exist_ok=True)
        path = os.path.join(self.config.probe_dir, name)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        return path

    def write_sidecar(self, stem: str, payload: dict) -> None:
        os.makedirs(self.meta_dir, exist_ok=True)
        tmp = os.path.join(self.meta_dir, f".{stem}.json.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, sort_keys=True)
        os.replace(tmp, os.path.join(self.meta_dir, f"{stem}.json"))

    def rename_prefix(self, path: str, new_prefix: str) -> str:
        if not path or not os.path.exists(path):
            return ""
        directory, name = os.path.split(path)
        for prefix in UNLABELED_PREFIXES + (LABELED_PREFIX,):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        target = os.path.join(directory, new_prefix + name)
        os.rename(path, target)
        return target

    def _entries(self) -> List[tuple]:
        try:
            names = os.listdir(self.config.probe_dir)
        except FileNotFoundError:
            return []
        out = []
        for name in names:
            if not name.endswith(".wav"):
                continue
            path = os.path.join(self.config.probe_dir, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            out.append((mtime, name, path))
        return out

    @staticmethod
    def event_stem(name: str) -> str:
        """``falsewake_<stem>_trigger.wav`` -> ``<stem>`` (the event's stem)."""
        stem = name[:-4] if name.endswith(".wav") else name
        for prefix in UNLABELED_PREFIXES + (LABELED_PREFIX,):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
                break
        return stem[:-len("_trigger")] if stem.endswith("_trigger") else stem

    def prune(self) -> Dict[str, int]:
        """Apply retention. Labeled clips are never removed to make room for unlabeled ones.

        Unlabeled captures (``probe_``/``trigger_``/``candidate_``) and labeled
        false wakes (``falsewake_``) are separate pools, each with its own TTL
        and count cap, oldest first. Label sidecars are removed only together
        with the last labeled clip of their event.
        """
        now = self._now()
        removed = {"unlabeled": 0, "labeled": 0}
        entries = self._entries()
        pools = {
            "unlabeled": (
                sorted(e for e in entries if not e[1].startswith(LABELED_PREFIX)),
                self.config.unlabeled_ttl_days * 86400,
                self.config.max_unlabeled,
            ),
            "labeled": (
                sorted(e for e in entries if e[1].startswith(LABELED_PREFIX)),
                self.config.labeled_ttl_days * 86400,
                self.config.max_labeled,
            ),
        }
        removed_labeled_stems = set()
        for bucket, (pool, ttl_s, cap) in pools.items():
            survivors = []
            doomed = []
            for entry in pool:
                (doomed if ttl_s and now - entry[0] > ttl_s else survivors).append(entry)
            excess = len(survivors) - cap
            if excess > 0:
                doomed.extend(survivors[:excess])
                survivors = survivors[excess:]
            for _, name, path in doomed:
                try:
                    os.remove(path)
                    removed[bucket] += 1
                except OSError:
                    continue
                if bucket == "labeled":
                    removed_labeled_stems.add(self.event_stem(name))
            if bucket == "labeled":
                still_labeled = {self.event_stem(name) for _, name, _ in survivors}
                for stem in removed_labeled_stems - still_labeled:
                    try:
                        os.remove(os.path.join(self.meta_dir, f"{stem}.json"))
                    except OSError:
                        pass
        if removed["unlabeled"] or removed["labeled"]:
            logger.info(f"🧹 wake capture retention removed {removed}")
        return removed

    def purge(self, include_labeled: bool = False) -> int:
        """Delete captures (and their metadata). Used by the purge command."""
        count = 0
        for _, name, path in self._entries():
            if name.startswith(LABELED_PREFIX) and not include_labeled:
                continue
            try:
                os.remove(path)
                count += 1
            except OSError:
                pass
        if include_labeled:
            try:
                for name in os.listdir(self.meta_dir):
                    os.remove(os.path.join(self.meta_dir, name))
            except FileNotFoundError:
                pass
        return count


class WakeEventStore:
    """Per-device wake history, labeling and (optional) capture persistence."""

    RING_PER_DEVICE = 32

    def __init__(
        self,
        config: Optional[CaptureConfig] = None,
        instance: str = "",
        guest_mode: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.config = config or CaptureConfig.from_env()
        self.instance = safe_token(instance or os.environ.get("INSTANCE_NAME", "") or "device", 24)
        self.archive = ProbeArchive(self.config, now=wall)
        self._guest_mode = guest_mode
        self._clock = clock
        self._wall = wall
        self._events: Dict[str, Deque[WakeEvent]] = {}
        self.false_wake_hooks: List[Callable[[WakeEvent], None]] = []
        # Label/path transitions happen on the event loop (flags) and on the
        # capture writer thread (saves); this lock keeps them consistent so a
        # flag that lands mid-save still ends up as a falsewake_ file.
        self._lock = threading.Lock()

    # -- storage policy -----------------------------------------------------
    def storing_metadata(self) -> bool:
        return self.config.stores_metadata and not self._guest_mode()

    def storing_audio(self) -> bool:
        return self.config.stores_audio and not self._guest_mode()

    # -- events -------------------------------------------------------------
    def record_wake(self, device_id: str, turn_id: Optional[str] = None, **meta) -> WakeEvent:
        device_id = safe_token(device_id, 64) or "unknown"
        turn_id = safe_token(turn_id, 40) or uuid.uuid4().hex[:12]
        now_wall = self._wall()
        ms = int((now_wall % 1) * 1000)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now_wall)) + f"_{ms:03d}"
        event = WakeEvent(
            device_id=device_id,
            turn_id=turn_id,
            t_mono=self._clock(),
            wall=now_wall,
            stem=f"{stamp}_{device_id}_{turn_id}",
            **{k: v for k, v in meta.items() if k in WakeEvent.__dataclass_fields__},
        )
        ring = self._events.setdefault(device_id, deque(maxlen=self.RING_PER_DEVICE))
        ring.append(event)
        self._log(event, "wake")
        return event

    def latest(self, device_id: str) -> Optional[WakeEvent]:
        ring = self._events.get(safe_token(device_id, 64))
        return ring[-1] if ring else None

    def find(self, device_id: str, turn_id: str) -> Optional[WakeEvent]:
        ring = self._events.get(safe_token(device_id, 64)) or ()
        turn_id = safe_token(turn_id, 40)
        for event in reversed(ring):
            if event.turn_id == turn_id:
                return event
        return None

    def set_outcome(self, device_id: str, turn_id: str, outcome: str) -> None:
        event = self.find(device_id, turn_id)
        if event is not None and not event.label:
            event.outcome = outcome
            self._log(event, "outcome")

    # -- captures -----------------------------------------------------------
    def _save_clip(self, event: WakeEvent, attr: str, prefix: str, suffix: str,
                   pcm: bytes, rate: int) -> str:
        if not pcm or not self.storing_audio():
            return ""
        try:
            with self._lock:
                labeled = event.label == "false_wake"
            name = f"{LABELED_PREFIX if labeled else prefix}{event.stem}{suffix}.wav"
            path = self.archive.write_wav(name, pcm, rate)
            with self._lock:
                setattr(event, attr, path)
                # A flag may have arrived while the file was being written.
                if event.label == "false_wake" and not labeled:
                    path = self.archive.rename_prefix(path, LABELED_PREFIX)
                    setattr(event, attr, path)
                    labeled = True
            if labeled:
                self._write_label_sidecar(event)
            self.archive.prune()
            return path
        except Exception as e:
            logger.warning(f"⚠️ wake capture write failed: {e!r}")
            return ""

    def save_probe(self, event: WakeEvent, pcm: bytes) -> str:
        """Persist the post-wake capture for one event (audio mode only)."""
        return self._save_clip(event, "probe_path", "probe_", "", pcm, SAMPLE_RATE)

    def save_trigger(self, event: WakeEvent, pcm: bytes, rate: int = SAMPLE_RATE) -> str:
        """Persist the device's pre-wake trigger snippet (audio mode only)."""
        return self._save_clip(event, "trigger_path", "trigger_", "_trigger", pcm, rate)

    # -- labels -------------------------------------------------------------
    def flag_false_wake(
        self,
        device_id: str,
        method: str,
        turn_id: Optional[str] = None,
        age_s: float = 0.0,
        reply_audio_before_flag: Optional[bool] = None,
    ) -> Optional[WakeEvent]:
        """Label one wake on ``device_id`` as a false trigger.

        With ``turn_id`` the exact event is labeled (queued firmware flags may
        be up to ``queued_flag_window_s`` old). Without it, only the newest
        wake on this device within ``flag_window_s`` qualifies. Returns the
        labeled event, or None when nothing on this device qualifies.
        """
        now = self._clock()
        if turn_id:
            event = self.find(device_id, turn_id)
            window = self.config.queued_flag_window_s
            if event is not None and now - age_s - event.t_mono > window:
                event = None
        else:
            event = self.latest(device_id)
            if event is not None and now - event.t_mono > self.config.flag_window_s:
                event = None
        if event is None:
            logger.info(
                f"🏷️ false-wake flag ({method}) on {safe_token(device_id, 64)} matched no recent wake — ignored"
            )
            return None
        with self._lock:
            if event.label == "false_wake":
                return event
            event.label = "false_wake"
            event.label_method = safe_token(method, 24)
            event.labeled_at = self._wall()
            event.reply_audio_before_flag = reply_audio_before_flag
            event.outcome = "false_wake"
            try:
                if event.probe_path:
                    event.probe_path = self.archive.rename_prefix(event.probe_path, LABELED_PREFIX)
                if event.trigger_path:
                    event.trigger_path = self.archive.rename_prefix(event.trigger_path, LABELED_PREFIX)
            except OSError as e:
                logger.warning(f"⚠️ could not relabel capture: {e!r}")
        if self.storing_metadata() and (event.probe_path or event.trigger_path):
            self._write_label_sidecar(event)
        self._log(event, "label")
        logger.info(
            f"🏷️ false wake labeled: device={event.device_id} turn={event.turn_id} "
            f"method={event.label_method} probe={bool(event.probe_path)} "
            f"trigger={bool(event.trigger_path)}"
        )
        for hook in self.false_wake_hooks:
            try:
                hook(event)
            except Exception as e:
                logger.debug(f"false-wake hook failed: {e!r}")
        return event

    def mark_candidate(self, device_id: str, turn_id: str, reason: str) -> None:
        """Record an unconfirmed false-wake candidate (e.g. admission silence).

        Candidates are NEVER training negatives: only a human flag promotes a
        capture to ``falsewake_*``.
        """
        event = self.find(device_id, turn_id)
        if event is None or event.label:
            return
        with self._lock:
            event.outcome = safe_token(reason, 32)
            if event.probe_path and os.path.basename(event.probe_path).startswith("probe_"):
                try:
                    event.probe_path = self.archive.rename_prefix(event.probe_path, "candidate_")
                except OSError:
                    pass
        self._log(event, "candidate")

    # -- metadata log -------------------------------------------------------
    def _write_label_sidecar(self, event: WakeEvent) -> None:
        try:
            self.archive.write_sidecar(event.stem, event.public_dict())
        except Exception as e:
            logger.debug(f"label sidecar failed: {e!r}")

    def _log(self, event: WakeEvent, kind: str) -> None:
        if not self.storing_metadata():
            return
        try:
            os.makedirs(self.archive.meta_dir, exist_ok=True)
            path = os.path.join(self.archive.meta_dir, f"events-{self.instance}.jsonl")
            record = {"kind": kind, "at": round(self._wall(), 3), **event.public_dict()}
            with open(path, "a") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
            self._bound_log(path)
        except Exception as e:
            logger.debug(f"wake event log failed: {e!r}")

    def _bound_log(self, path: str) -> None:
        """Keep the metadata log bounded (count and TTL)."""
        try:
            if os.path.getsize(path) < 2_000_000:
                return
            with open(path) as f:
                lines = f.readlines()
            cutoff = self._wall() - self.config.unlabeled_ttl_days * 86400
            kept = []
            for line in lines[-self.config.max_events_logged:]:
                try:
                    if json.loads(line).get("at", 0) >= cutoff:
                        kept.append(line)
                except ValueError:
                    continue
            tmp = f"{path}.tmp"
            with open(tmp, "w") as f:
                f.writelines(kept)
            os.replace(tmp, path)
        except OSError:
            pass


class WakeAudioCapture:
    """Collects the first seconds of post-wake mic audio for one device.

    Independent of the speaker-identification settings (the old probe dump
    only ran when speaker names were configured). Saves happen on a worker
    thread so file I/O and retention scans never stall the audio path.
    """

    def __init__(self, store: WakeEventStore, capture_seconds: float = 5.0,
                 min_seconds: float = 1.0, rate: int = SAMPLE_RATE):
        self.store = store
        self._cap = int(capture_seconds * rate * 2)
        self._min = int(min_seconds * rate * 2)
        self._capture_seconds = capture_seconds
        self._event: Optional[WakeEvent] = None
        self._buf = bytearray()
        self._active = False
        self._generation = 0
        self.pending: List[asyncio.Future] = []

    def start(self, event: WakeEvent) -> None:
        self.finalize()
        self._generation += 1
        self._event = event
        self._buf = bytearray()
        self._active = self.store.storing_audio()
        if not self._active:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        generation = self._generation
        loop.call_later(self._capture_seconds + 1.0,
                        lambda: generation == self._generation and self.finalize())

    def feed(self, pcm: bytes) -> None:
        if not self._active:
            return
        self._buf += pcm
        if len(self._buf) >= self._cap:
            self._flush(bytes(self._buf[:self._cap]))

    def finalize(self) -> None:
        """Save a partial capture (short false wakes matter most) or drop it."""
        if not self._active:
            return
        if len(self._buf) >= self._min:
            self._flush(bytes(self._buf))
        else:
            self._active = False
            self._buf = bytearray()

    def _flush(self, pcm: bytes) -> None:
        event = self._event
        self._active = False
        self._buf = bytearray()
        if event is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.store.save_probe(event, pcm)
            return
        future = loop.run_in_executor(None, self.store.save_probe, event, pcm)
        self.pending = [f for f in self.pending if not f.done()] + [future]


def weekly_report(log_paths: List[str], now: Optional[float] = None, days: int = 7) -> dict:
    """Aggregate metadata-only wake statistics (no audio, no transcripts)."""
    now = now or time.time()
    since = now - days * 86400
    wakes: Dict[str, set] = {}
    labels: Dict[str, set] = {}
    candidates: Dict[str, set] = {}
    outcomes: Dict[str, int] = {}
    models: Dict[str, int] = {}
    methods: Dict[str, int] = {}
    for path in log_paths:
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("at", 0) < since:
                        continue
                    key = (rec.get("device_id"), rec.get("turn_id"))
                    device = rec.get("device_id") or "unknown"
                    if rec.get("kind") == "wake":
                        wakes.setdefault(device, set()).add(key)
                        model = f"{rec.get('model') or 'unknown'}@{rec.get('cutoff')}/{rec.get('window')}"
                        models[model] = models.get(model, 0) + 1
                    elif rec.get("kind") == "label":
                        labels.setdefault(device, set()).add(key)
                        m = rec.get("label_method") or "unknown"
                        methods[m] = methods.get(m, 0) + 1
                    elif rec.get("kind") == "candidate":
                        candidates.setdefault(device, set()).add(key)
                    if rec.get("kind") in ("outcome", "label", "candidate") and rec.get("outcome"):
                        outcomes[rec["outcome"]] = outcomes.get(rec["outcome"], 0) + 1
        except FileNotFoundError:
            continue
    devices = sorted(set(wakes) | set(labels) | set(candidates))
    per_device = {}
    for device in devices:
        n_wakes = len(wakes.get(device, ()))
        n_flags = len(labels.get(device, ()))
        per_device[device] = {
            "wakes": n_wakes,
            "false_wake_flags": n_flags,
            "unconfirmed_candidates": len(candidates.get(device, set()) - labels.get(device, set())),
            "flags_per_day": round(n_flags / days, 3),
            "flag_rate": round(n_flags / n_wakes, 4) if n_wakes else None,
        }
    total_wakes = sum(v["wakes"] for v in per_device.values())
    total_flags = sum(v["false_wake_flags"] for v in per_device.values())
    return {
        "period_days": days,
        "generated_at": round(now, 3),
        "devices": per_device,
        "totals": {
            "wakes": total_wakes,
            "false_wake_flags": total_flags,
            "flag_rate": round(total_flags / total_wakes, 4) if total_wakes else None,
        },
        "operating_points": models,
        "label_methods": methods,
        "outcomes": outcomes,
    }


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Voice PE wake capture administration")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_purge = sub.add_parser("purge", help="delete wake captures")
    p_purge.add_argument("--include-labeled", action="store_true")
    sub.add_parser("prune", help="apply retention now")
    p_report = sub.add_parser("report", help="weekly metadata-only aggregate")
    p_report.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    config = CaptureConfig.from_env()
    archive = ProbeArchive(config)
    if args.cmd == "purge":
        print(json.dumps({"removed": archive.purge(include_labeled=args.include_labeled)}))
    elif args.cmd == "prune":
        print(json.dumps(archive.prune()))
    else:
        meta = archive.meta_dir
        try:
            logs = [os.path.join(meta, n) for n in os.listdir(meta) if n.startswith("events-")]
        except FileNotFoundError:
            logs = []
        print(json.dumps(weekly_report(logs, days=args.days), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
