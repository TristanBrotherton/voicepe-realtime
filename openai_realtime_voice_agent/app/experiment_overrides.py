"""Experiment overrides for a developer test rig (A/B without a rebuild).

Honoured ONLY when the hidden ``experiment_mode`` option is true (default
off). With it off, both sources are ignored and one warning is logged if
either is set. Nothing here can
touch a cost bound: the session lifetime, follow-up window, daily caps and
spend gate are not overridable, and every number is clamped.

Two sources, one shape ``{"run_id": str, "arm": str, "overrides": {...}}``:

* ``experiment_overrides`` option (JSON string): COLD keys, applied once at
  start-up before the configuration is read (the lab rewrites the option and
  restarts the add-on between A/B blocks).
* ``/data/experiment.json``: HOT keys, re-read at every Live session open, so
  the lab can interleave arms trial by trial with no restart.

Every session open logs ``🧪 experiment {"run_id", "arm", "keys"}`` so the lab
can verify the arm it intended is the arm that ran (a mismatch is a harness
failure, never a measurement).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)

EXPERIMENT_FILE = "/data/experiment.json"

# name -> (module, attribute, low, high)
HOT_KEYS = {
    "user_turn_gap_s": ("live_service", "USER_TURN_GAP_S", 0.3, 2.0),
    # Floored: with no lag the gate has no time to see the transcript of a
    # guess before its audio plays.
    "ack_lag_s": ("live_service", "ACK_LAG_S", 0.15, 1.5),
    "fallback_ack_after_s": ("live_service", "FALLBACK_ACK_AFTER_S", 0.5, 5.0),
    "progress_first_s": ("live_service", "PROGRESS_FIRST_S", 2.0, 30.0),
}
_ENUMS = {
    "live_service_tier": ("LIVE_SERVICE_TIER", ("auto", "default", "flex", "priority")),
    "live_reasoning_effort": ("LIVE_REASONING_EFFORT", ("none", "minimal", "low", "medium", "high")),
}
_MODEL = re.compile(r"^gpt-[a-z0-9.\-]{1,40}$")
COLD_KEYS = {"live_backend_model", "live_service_tier", "live_reasoning_effort", "live_router",
             "voice_prompt_variant", "backend_prompt_variant", "wait_core_extra", "wait_tails_extra"}
MAX_VARIANT_CHARS = 600
MAX_GRAMMAR_ITEMS = 20
# A grammar addition may never make an outcome audible before the verified
# result: a bare state word ("unlocked") or anything that claims success or an
# action, alone or composed with the existing grammar, is refused (rig review).
STATE_WORDS = frozenset({"on", "off", "locked", "unlocked", "open", "closed", "done", "set", "yes"})

_defaults: Dict[str, object] = {}
_warned = False


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("EXPERIMENT_MODE") or "").strip().lower() in ("true", "1", "yes", "on")


def _ignored_warning(env: Mapping[str, str]) -> None:
    global _warned
    if not _warned and (env.get("EXPERIMENT_OVERRIDES") or os.path.exists(EXPERIMENT_FILE)):
        logger.warning("⚠️ experiment overrides ignored: experiment_mode is off")
        _warned = True


def _parse(raw: str, where: str) -> Optional[dict]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(f"⚠️ experiment: {where} is not JSON; ignored")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("overrides", {}), dict):
        logger.warning(f"⚠️ experiment: {where} must be an object with an 'overrides' object; ignored")
        return None
    data.setdefault("overrides", {})
    data["arm"] = str(data.get("arm", ""))[:40]
    data["run_id"] = str(data.get("run_id", ""))[:60]
    return data


def validate(overrides: Mapping[str, object], allowed: set) -> Dict[str, object]:
    """Keep only known keys with sane values; log and drop everything else."""
    out: Dict[str, object] = {}
    for key, value in overrides.items():
        if key not in allowed:
            logger.warning(f"⚠️ experiment: {key!r} is not overridable here; ignored")
            continue
        if key in HOT_KEYS:
            _mod, _attr, low, high = HOT_KEYS[key]
            try:
                out[key] = max(low, min(high, float(value)))
            except (TypeError, ValueError):
                logger.warning(f"⚠️ experiment: {key}={value!r} is not a number; ignored")
        elif key in _ENUMS:
            if str(value).lower() in _ENUMS[key][1]:
                out[key] = str(value).lower()
        elif key == "live_backend_model":
            if _MODEL.match(str(value)):
                out[key] = str(value)
        elif key == "live_router":
            out[key] = bool(value)
        elif key in ("voice_prompt_variant", "backend_prompt_variant"):
            out[key] = str(value)[:MAX_VARIANT_CHARS]
        elif key in ("wait_core_extra", "wait_tails_extra"):
            if isinstance(value, list):
                items = [" ".join(re.sub(r"[^a-z ]", "", str(v).lower()).split())
                         for v in value[:MAX_GRAMMAR_ITEMS]]
                out[key] = [v for v in items if v]
    if "wait_core_extra" in out or "wait_tails_extra" in out:
        core, tails = _safe_grammar(out.get("wait_core_extra", []), out.get("wait_tails_extra", []))
        for key, kept in (("wait_core_extra", core), ("wait_tails_extra", tails)):
            if key in out:
                out[key] = kept
    return out


def _claims(phrase: str) -> bool:
    from app.live_router import claims_action, claims_success
    return claims_success(phrase) or claims_action(phrase)


def _safe_grammar(core, tails):
    """Drop every addition that is a bare state word or that claims an
    outcome by itself or in any phrase it would form (core + tail, with and
    without a lead-in)."""
    from app import live_router as lr
    lead_ins = [""] + [" ".join(unit) for unit in lr._LEAD_INS]

    def bad(item: str, cores, tails_) -> bool:
        if not item or set(item.split()) <= STATE_WORDS or _claims(item):
            return True
        for c in cores:
            for t in tails_:
                for lead in lead_ins:
                    if _claims(" ".join(p for p in (lead, c, t) if p)):
                        return True
        return False

    alone_ok = [t for t in tails if not bad(t, [], [])]
    kept_core = [c for c in core if not bad(c, [c], list(lr._WAIT_TAILS) + alone_ok)]
    kept_tails = [t for t in alone_ok if not bad(t, list(lr._WAIT_CORE) + kept_core, [t])]
    for item in set(core) - set(kept_core) | set(tails) - set(kept_tails):
        logger.warning(f"⚠️ experiment: grammar addition {item!r} refused (a state word or an outcome claim)")
    return kept_core, kept_tails


def apply_startup(env: MutableMapping[str, str]) -> Optional[dict]:
    """Apply COLD overrides from the experiment_overrides option to the
    environment the configuration is read from. Call before Application()."""
    if not enabled(env):
        _ignored_warning(env)
        return None
    data = _parse(env.get("EXPERIMENT_OVERRIDES") or "{}", "experiment_overrides")
    if data is None:
        return None
    ov = validate(data["overrides"], COLD_KEYS)
    if "live_backend_model" in ov:
        env["LIVE_BACKEND_MODEL"] = ov["live_backend_model"]
    for key, (var, _choices) in _ENUMS.items():
        if key in ov:
            env[var] = ov[key]
    if "live_router" in ov:
        env["LIVE_ROUTER"] = "true" if ov["live_router"] else "false"
    if ov.get("voice_prompt_variant"):
        env["INSTRUCTIONS"] = (env.get("INSTRUCTIONS", "") + " " + ov["voice_prompt_variant"]).strip()
    if ov.get("backend_prompt_variant"):
        env["LIVE_BACKEND_INSTRUCTIONS"] = (env.get("LIVE_BACKEND_INSTRUCTIONS", "") + " "
                                            + ov["backend_prompt_variant"]).strip()
    if ov.get("wait_core_extra") or ov.get("wait_tails_extra"):
        extend_wait_grammar(ov.get("wait_core_extra") or [], ov.get("wait_tails_extra") or [])
    data["overrides"] = ov
    env["EXPERIMENT_ACTIVE_COLD"] = json.dumps(data, sort_keys=True)
    logger.info("🧪 experiment cold overrides " + json.dumps({"run_id": data["run_id"], "arm": data["arm"],
                                                             "keys": sorted(ov)}))
    return data


def extend_wait_grammar(core, tails) -> None:
    from app import live_router as lr
    lr._WAIT_CORE = tuple(lr._WAIT_CORE) + tuple(c for c in core if c not in lr._WAIT_CORE)
    lr._WAIT_TAILS = tuple(lr._WAIT_TAILS) + tuple(t for t in tails if t not in lr._WAIT_TAILS)
    lr._WAITS = tuple(sorted({tuple((c + " " + t).split()) for c in lr._WAIT_CORE for t in lr._WAIT_TAILS},
                             key=len, reverse=True))


def apply_session_safely() -> Optional[dict]:
    """The session-open hook: an experiment never blocks or fails a session."""
    try:
        return apply_session()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"⚠️ experiment overrides not applied: {e!r}")
        return None


def apply_session(path: str = EXPERIMENT_FILE, env: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """Apply HOT overrides at a Live session open (defaults when the file is
    absent), and log which arm this session runs."""
    env = os.environ if env is None else env
    if not enabled(env):
        _ignored_warning(env)
        return None
    import importlib
    data = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = _parse(f.read(), path)
        except OSError as e:
            logger.warning(f"⚠️ experiment: cannot read {path}: {e!r}")
    ov = validate((data or {}).get("overrides", {}), set(HOT_KEYS) | (COLD_KEYS & set((data or {}).get("overrides", {}))))
    cold_in_file = sorted(set(ov) & COLD_KEYS)
    if cold_in_file:
        logger.warning(f"⚠️ experiment: {cold_in_file} need a restart (experiment_overrides option); ignored here")
    for key, (mod_name, attr, _lo, _hi) in HOT_KEYS.items():
        mod = importlib.import_module(f"app.{mod_name}")
        _defaults.setdefault(key, getattr(mod, attr))
        setattr(mod, attr, ov.get(key, _defaults[key]))
    cold = json.loads(env.get("EXPERIMENT_ACTIVE_COLD") or "null")
    arm = (data or {}).get("arm") or (cold or {}).get("arm") or ""
    run_id = (data or {}).get("run_id") or (cold or {}).get("run_id") or ""
    hot = {k: v for k, v in ov.items() if k in HOT_KEYS}
    logger.info("🧪 experiment " + json.dumps({"run_id": run_id, "arm": arm,
                                              "keys": sorted(hot) + sorted((cold or {}).get("overrides", {}))},
                                             separators=(",", ":")))
    return {"run_id": run_id, "arm": arm, "hot": hot}
