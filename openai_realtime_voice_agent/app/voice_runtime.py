"""Voice runtime selection: OpenAI Realtime (legacy) or GPT-Live.

The two are different protocols, not two model names on one API:

* **realtime** — ``wss://api.openai.com/v1/realtime``: server VAD, explicit
  ``response.create``/``response.cancel``, tools on the voice model.
* **live** — ``wss://api.openai.com/v1/live/sessions``: continuous full-duplex
  audio, the model decides when to speak, tools run through a delegated
  Responses backend, and there is no end-of-response event.

This module only normalizes configuration. ``main.py`` picks the service
class from the result; the device transport, tools and safety gates never
look at it.

Backward compatibility: a missing or empty ``VOICE_RUNTIME`` means
``realtime``. Existing installs therefore keep their behaviour until an
operator explicitly selects ``live``; the add-on's option is optional and has
no default for exactly that reason (Home Assistant merges new option defaults
into existing installs on update).
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

REALTIME = "realtime"
LIVE = "live"
RUNTIMES = (REALTIME, LIVE)

_ALIASES = {
    "": REALTIME,
    "realtime": REALTIME,
    "gpt-realtime": REALTIME,
    "gpt_realtime": REALTIME,
    "legacy": REALTIME,
    "live": LIVE,
    "gpt-live": LIVE,
    "gpt_live": LIVE,
    "gptlive": LIVE,
    "gpt-live-1": LIVE,
}

DEFAULT_LIVE_MODEL = "gpt-live-1"
# Relay-side playout lead the device needs on the Live runtime, in ms.
#
# Measured against the real endpoint on 2026-10-07 (app/live_probe.py): GPT-Live
# delivers reply audio in 100 ms frames at 1.02-1.11x real time during speech,
# with in-speech inter-frame gaps up to ~160 ms and session-wide spikes to
# 341-380 ms. The Realtime API instead bursts a whole reply far faster than
# real time, so the Voice PE accumulates seconds of playout lead by itself and
# rides through jitter invisibly. On Live it starts every reply with no lead and
# gains only ~20-80 ms per second, so its resampler/mixer/I2S chain starves
# repeatedly from the first word — heard as static, rasp or stutter.
#
# 600 ms covers the measured worst-case inter-frame gap with room for network
# jitter, and costs that much latency on the first word (once per reply).
# app/main.py applies it as the default for OUTPUT_LEAD_BUFFER_MS on the Live
# runtime only; an explicit operator value always wins.
LIVE_OUTPUT_LEAD_MS = 600
# The delegated Responses backend. The official delegation guide says to
# start with gpt-6-luna; gpt-6-sol is the heavier option for complex tasks.
DEFAULT_LIVE_BACKEND_MODEL = "gpt-6-luna"
DEFAULT_LIVE_REASONING_EFFORT = "low"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")
SERVICE_TIERS = ("auto", "default", "flex", "priority")

# Voices the GPT-Live documentation lists as supported for `audio.output.voice`
# (the Realtime voices marin/cedar plus the Live-specific set). Anything else
# is NOT guessed: the session falls back to the server default (marin) with a
# loud warning, so an unsupported Realtime-only voice can never fail a canary
# silently or pick a voice nobody configured.
LIVE_VOICES = frozenset({
    "marin", "cedar",
    "quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian",
    "bossa", "tempo", "beacon", "delta", "cinder",
})


def normalize_runtime(raw: Optional[str]) -> str:
    """Map a configured selector onto ``realtime`` or ``live``.

    Unknown values fall back to ``realtime`` (the safe, previously-only
    runtime) and are logged, never raised: a typo in the add-on options must
    not take the voice assistant down.
    """
    value = (raw or "").strip().lower()
    runtime = _ALIASES.get(value)
    if runtime is None:
        logger.warning(f"⚠️ Unknown VOICE_RUNTIME {raw!r}; using {REALTIME}")
        return REALTIME
    return runtime


def resolve_voice_runtime(env: Optional[Mapping[str, str]] = None) -> str:
    """The runtime for this process, from ``VOICE_RUNTIME`` (missing = realtime)."""
    env = os.environ if env is None else env
    return normalize_runtime(env.get("VOICE_RUNTIME"))


def resolve_output_lead_ms(configured: Optional[str], runtime: str,
                           maximum: int = 2000) -> int:
    """How much relay-side playout lead to give the device, in ms.

    Realtime keeps its opt-in default of 0: it bursts a whole reply far faster
    than real time, so the device builds its own cushion. Live defaults to
    :data:`LIVE_OUTPUT_LEAD_MS` because it does not. An explicit operator value
    always wins, including ``0`` to switch the lead off deliberately; a
    non-numeric value falls back to the runtime's default rather than failing
    the add-on's startup.
    """
    default = LIVE_OUTPUT_LEAD_MS if runtime == LIVE else 0
    text = (configured or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except (TypeError, ValueError):
        logger.warning(f"⚠️ OUTPUT_LEAD_BUFFER_MS={configured!r} is not an int; using {default}")
        return default
    return max(0, min(maximum, value))


def live_voice_for(configured_voice: str) -> Optional[str]:
    """The voice to request from GPT-Live for a configured ``openai_voice``.

    Returns the voice when the Live documentation lists it, else ``None``
    (omit the field, server default) after warning. We never substitute a
    different voice on the operator's behalf.
    """
    voice = (configured_voice or "").strip().lower()
    if not voice:
        return None
    if voice in LIVE_VOICES:
        return voice
    logger.warning(
        f"⚠️ openai_voice={voice!r} is not a documented GPT-Live voice; the Live "
        "session will use the server default voice. Set openai_voice to a Live "
        "voice (e.g. marin, cedar or vesper) to choose explicitly."
    )
    return None


@dataclass
class LiveConfig:
    """Everything the GPT-Live runtime needs beyond the shared options."""

    model: str = DEFAULT_LIVE_MODEL
    backend_model: str = DEFAULT_LIVE_BACKEND_MODEL
    reasoning_effort: str = DEFAULT_LIVE_REASONING_EFFORT
    service_tier: Optional[str] = None
    backend_instructions: str = ""
    # Startup refuses to run the Live runtime while parity gaps exist unless
    # the operator acknowledges them explicitly (see live_parity_gaps()).
    acknowledge_gaps: bool = False
    max_output_tokens: Optional[int] = None
    voice: Optional[str] = None
    # Diagnostics: milliseconds of raw model-output PCM to hold in memory for a
    # "that reply was static" report. 0 = off (the default). The format metrics
    # and the bounded hash a static report needs are always collected; this is
    # only for when the waveform itself has to be inspected, and it holds the
    # assistant's voice, so it is opt-in and capped.
    audio_capture_ms: int = 0
    # Options the Live protocol has no equivalent for; reported, not applied.
    ignored: list = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None,
                 configured_voice: str = "", max_output_tokens: Optional[int] = None) -> "LiveConfig":
        env = os.environ if env is None else env

        def _get(name: str, default: str = "") -> str:
            return (env.get(name) or "").strip() or default

        effort = _get("LIVE_REASONING_EFFORT", DEFAULT_LIVE_REASONING_EFFORT).lower()
        if effort not in REASONING_EFFORTS:
            logger.warning(f"⚠️ Unknown LIVE_REASONING_EFFORT {effort!r}; using {DEFAULT_LIVE_REASONING_EFFORT}")
            effort = DEFAULT_LIVE_REASONING_EFFORT
        tier = _get("LIVE_SERVICE_TIER").lower() or None
        if tier is not None and tier not in SERVICE_TIERS:
            logger.warning(f"⚠️ Unknown LIVE_SERVICE_TIER {tier!r}; leaving the project default")
            tier = None
        ack = _get("LIVE_ACKNOWLEDGE_GAPS").lower() in ("true", "1", "yes", "on")
        # The Responses backend requires at least 16 when the cap is set.
        cap = None
        if max_output_tokens:
            cap = max(16, int(max_output_tokens))

        try:
            capture_ms = int(_get("LIVE_AUDIO_CAPTURE_MS", "0"))
        except ValueError:
            capture_ms = 0
        capture_ms = max(0, min(60000, capture_ms))

        ignored = []
        for name, label in (
            ("TURN_DETECTION_TYPE", "turn_detection_type"),
            ("VAD_EAGERNESS", "vad_eagerness"),
            ("VAD_THRESHOLD", "vad_threshold"),
            ("VAD_PREFIX_PADDING_MS", "vad_prefix_padding_ms"),
            ("VAD_SILENCE_DURATION_MS", "vad_silence_duration_ms"),
            ("NOISE_REDUCTION", "noise_reduction"),
            ("TRANSCRIPTION_MODEL", "transcription_model"),
            ("TRANSCRIPTION_LANGUAGE", "transcription_language"),
        ):
            if _get(name):
                ignored.append(label)
        try:
            speed = float(_get("OPENAI_SPEED", "1.0"))
        except ValueError:
            speed = 1.0
        if abs(speed - 1.0) > 1e-6:
            ignored.append("openai_speed")

        return cls(
            model=_get("LIVE_MODEL", DEFAULT_LIVE_MODEL),
            backend_model=_get("LIVE_BACKEND_MODEL", DEFAULT_LIVE_BACKEND_MODEL),
            reasoning_effort=effort,
            service_tier=tier,
            backend_instructions=_get("LIVE_BACKEND_INSTRUCTIONS"),
            acknowledge_gaps=ack,
            max_output_tokens=cap,
            voice=live_voice_for(configured_voice),
            audio_capture_ms=capture_ms,
            ignored=ignored,
        )


# ---------------------------------------------------------------------------
# Parity report
#
# Every capability the Realtime runtime offers, with its GPT-Live status.
# "unsupported" items block startup of the Live runtime until the operator
# sets live_acknowledge_gaps, so a canary never silently loses a feature.
# ---------------------------------------------------------------------------
SUPPORTED = "supported"
DEGRADED = "degraded"
UNSUPPORTED = "unsupported"

PARITY = (
    ("Home Assistant tools (MCP)", SUPPORTED, "function tools on the delegated Responses backend"),
    ("web search", SUPPORTED, "same web_search function tool, executed locally"),
    ("timers / memory / enrollment / OpenClaw", SUPPORTED, "function tools"),
    ("confirmations (action gate) + speaker gate", SUPPORTED, "enforced below the model, shared code"),
    ("slow-tool acknowledgement / spoken errors", SUPPORTED, "shared prompt playback"),
    ("transcripts", SUPPORTED, "native session transcript deltas (transcription_* options unused)"),
    ("device phases", SUPPORTED, "derived from transcript turns and gated output audio"),
    ("session reuse / reconnect", SUPPORTED, "new session seeded with recent text history"),
    ("speaker verdict injection", SUPPORTED, "session.thinking.append"),
    ("interruption (device stop)", DEGRADED,
     "no response.cancel on Live: stop instruction + local output gating; backend work finishes"),
    ("input buffer clear on stop/flush", DEGRADED,
     "Live has no input buffer; the device mic gate is authoritative"),
    ("VAD / noise-reduction / speed tuning", DEGRADED, "the Live model owns turn-taking; options ignored"),
    ("per-response cost sensor", UNSUPPORTED,
     "Live bills per second and the backend per token; no priced estimate is published"),
)


def live_parity_gaps(config: "LiveConfig") -> list:
    """Capabilities GPT-Live cannot provide here. Empty when fully covered."""
    return [name for name, status, _ in PARITY if status == UNSUPPORTED]


def parity_report_lines() -> list:
    return [f"{status:<11} {name}: {note}" for name, status, note in PARITY]


class LiveRuntimeBlocked(RuntimeError):
    """Raised at startup when Live parity gaps are not acknowledged."""


def check_live_deployable(config: "LiveConfig") -> None:
    gaps = live_parity_gaps(config)
    if gaps and not config.acknowledge_gaps:
        raise LiveRuntimeBlocked(
            "voice_runtime=live is blocked: unsupported parity items "
            f"{gaps}. Review the report in the log and set live_acknowledge_gaps: true "
            "to run the GPT-Live canary anyway, or set voice_runtime back to realtime."
        )
