"""Read what a tool actually answered, and stop a failing action repeating.

The second canary (2026-10-07 09:48 PDT) showed a complete, healthy tool
lifecycle around a household action that did not happen::

    🔧 live function calls: {'observed': 4, 'dispatched': 4, 'unregistered': 0,
                             'submitted': 4, 'continued': 4, 'abandoned': 0}

Nothing was lost, so the lifecycle counters said everything was fine, while the
light was still off and the assistant was telling the user that control had
failed. The two action results were::

    Error calling tool: Received invalid slot info for HassLightSet
    Error calling tool: Received invalid slot info for HassTurnOn

— and pipecat logged both as ``completed successfully``, because to pipecat a
tool that returns a string has succeeded. The *outcome* of a call was simply
never inspected anywhere.

This module adds that missing step, in three parts:

1. :func:`classify_result` — turn whatever a tool returned (a dict, a JSON
   string, or Home Assistant's bare ``Error calling tool: ...``) into an
   :class:`ToolOutcome`: did it work, what kind of failure was it, and is that
   verdict definitive.
2. :func:`explain_for_model` — replace an opaque error with one that says what
   to do instead. ``Received invalid slot info`` tells the model nothing; it
   needs to hear "omit parameters you have no value for" to recover, or "ask
   which device is meant" when a name matched nothing.
3. :class:`ActionLedger` — a per-utterance record of what has already been
   attempted, so a failing action cannot be retried without end. The canary's
   model answered one failure by immediately firing a second, different action
   at the same entity; after two definitive failures on one target the third
   attempt is answered from the ledger instead of reaching the house.

Privacy: the model-facing text keeps the detail, because the entity name in it
came from the model and it needs that name to clarify. The *log* text does not:
:func:`redact` removes quoted values, so a diagnostic line carries
``No exposed entities matched name '…'`` and never a household entity name.
This matches the existing rule that function-call logging records argument keys
and never argument values.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Failure kinds, most specific first. Matched case-insensitively against the
# text of whatever the tool returned.
_KINDS: Tuple[Tuple[str, str], ...] = (
    ("invalid-arguments", r"invalid slot info|invalid slot|not a valid value|string value is empty"),
    ("no-matching-entity", r"no exposed entities matched|no entities matched|matched no entities"),
    ("no-handler", r"not available in this session|could not be started"),
    ("unreachable", r"not reachable|is unavailable|connection refused"),
    ("uncertain", r"did not confirm|may or may not"),
    ("speaker-gate", r"reserved for .*and the current speaker"),
)
_QUOTED = re.compile(r"('[^']*'|\"[^\"]*\")")
_DETAIL_LIMIT = 160

# Read-only Home Assistant tools: asking twice is free and must never be
# refused, so the ledger ignores them entirely.
READ_ONLY_INTENTS = frozenset({
    "GetLiveContext", "GetDateTime", "get_items", "HassGetState", "HassGetCurrentDate",
    "HassGetCurrentTime", "HassGetWeather", "HassGetTemperature",
})
# Definitive failures allowed per target per utterance before the ledger stops
# answering with the house. Two lets the model recover from one bad call (which
# is exactly what a clearer error is for) without letting it loop.
MAX_FAILED_ATTEMPTS = 2


def redact(text: object, limit: int = _DETAIL_LIMIT) -> str:
    """A bounded, quote-stripped version of an error, safe to log."""
    flat = " ".join(str(text or "").split())
    flat = _QUOTED.sub("'…'", flat)
    return flat[:limit]


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool call actually achieved."""

    ok: bool
    kind: str = "ok"
    detail: str = ""
    # True when the answer settles the question: the action either happened or
    # definitely did not. False for "it may or may not have completed", where
    # neither a retry nor a confident report is safe.
    definitive: bool = True
    # The action gate held the call pending a spoken confirmation. Nothing ran,
    # and this must not count as an attempt against the ledger.
    pending_confirmation: bool = False

    @property
    def log_detail(self) -> str:
        return redact(self.detail)

    def describe(self) -> str:
        if self.ok:
            return "ok"
        if self.pending_confirmation:
            return "awaiting confirmation"
        return f"{self.kind}" + (f" ({self.log_detail})" if self.detail else "")


def _as_mapping(result: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(result, Mapping):
        return result
    if isinstance(result, str):
        text = result.strip()
        if text[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return None
            if isinstance(parsed, Mapping):
                return parsed
    return None


def _kind_of(text: str) -> str:
    for kind, pattern in _KINDS:
        if re.search(pattern, text, re.I):
            return kind
    return "error"


def classify_result(result: Any) -> ToolOutcome:
    """Decide whether a tool result reports success, and what kind of failure.

    Accepts every shape the tools in this add-on return: a dict from our own
    handlers, Home Assistant's ``{"success": bool, ...}`` JSON, and the bare
    ``Error calling tool: ...`` / ``Error: ...`` strings pipecat passes through
    from MCP without inspecting them.
    """
    if result is None:
        return ToolOutcome(ok=True, kind="ok")
    mapping = _as_mapping(result)
    if mapping is not None:
        if (mapping.get("requires_confirmation") or
                mapping.get("confirmation_required") or
                mapping.get("status") == "confirmation_required"):
            return ToolOutcome(ok=False, kind="confirmation-pending",
                               pending_confirmation=True)
        error = mapping.get("error")
        success = mapping.get("success")
        if error:
            detail = str(error)
            return ToolOutcome(ok=False, kind=_kind_of(detail), detail=detail,
                               definitive=_kind_of(detail) != "uncertain")
        if success is False:
            detail = str(mapping.get("message") or mapping.get("result") or "")
            kind = _kind_of(detail) if detail else "error"
            return ToolOutcome(ok=False, kind=kind, detail=detail,
                               definitive=kind != "uncertain")
        return ToolOutcome(ok=True, kind="ok")
    text = str(result).strip()
    if re.match(r"^error(\s+calling\s+(mcp\s+)?tool)?\b|^error:", text, re.I):
        kind = _kind_of(text)
        return ToolOutcome(ok=False, kind=kind, detail=text,
                           definitive=kind != "uncertain")
    return ToolOutcome(ok=True, kind="ok")


_ADVICE = {
    "invalid-arguments": (
        "Home Assistant rejected the arguments, so nothing changed. It does not "
        "accept empty strings, empty lists or placeholder zeros for parameters "
        "you have no value for — leave those parameters out entirely. Try once "
        "more with only the parameters you actually know (usually just name, or "
        "name plus area)."
    ),
    "no-matching-entity": (
        "Home Assistant has no exposed entity with that name, so nothing "
        "changed. Call GetLiveContext for the area to see what is exposed, or "
        "ask the user which device they mean. Do not say the action succeeded."
    ),
    "no-handler": (
        "That tool is not available in this session, so nothing changed. Say so "
        "briefly and do not try it again."
    ),
    "unreachable": (
        "Home Assistant could not be reached, so nothing changed. Say so "
        "briefly."
    ),
    "uncertain": (
        "Home Assistant did not confirm this; it may or may not have completed. "
        "Say exactly that and do not retry automatically."
    ),
}


def explain_for_model(function_name: str, result: Any,
                      outcome: Optional[ToolOutcome] = None) -> Any:
    """The result to hand the model: unchanged on success, clearer on failure.

    The original text is kept alongside the advice — it names the entity the
    model asked for, which is what lets it ask a useful clarifying question.
    """
    outcome = outcome or classify_result(result)
    if outcome.ok or outcome.pending_confirmation:
        return result
    advice = _ADVICE.get(outcome.kind)
    if advice is None:
        return result
    detail = " ".join(str(outcome.detail or "").split())[:_DETAIL_LIMIT]
    return {
        "error": f"{function_name} failed: {detail}" if detail
                 else f"{function_name} failed.",
        "what_to_do": advice,
        "state_changed": "unknown" if not outcome.definitive else False,
    }


# ---------------------------------------------------------------------------
# Per-utterance action ledger
# ---------------------------------------------------------------------------

def _target_key(function_name: str, arguments: Optional[Mapping[str, Any]]) -> str:
    """A stable identity for "the thing this call acts on".

    Only the targeting parameters, normalised, so ``HassTurnOn`` and
    ``HassLightSet`` aimed at the same light share a key while two different
    lights never do.
    """
    args = dict(arguments or {})

    def one(value: Any) -> str:
        if isinstance(value, (list, tuple, set)):
            return ",".join(sorted(str(v).strip().lower() for v in value))
        return str(value or "").strip().lower()

    return "|".join(one(args.get(field))
                    for field in ("name", "area", "floor", "domain", "device_class"))


def _exact_key(function_name: str, arguments: Optional[Mapping[str, Any]]) -> str:
    try:
        rendered = json.dumps(arguments or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        rendered = repr(sorted((arguments or {}).items()))
    return f"{function_name}\n{rendered}"


@dataclass
class _Attempt:
    function_name: str
    outcome: ToolOutcome


@dataclass
class ActionLedger:
    """What has already been attempted during the current user utterance.

    Reset on every new utterance (``_open_turn("user")``), so a follow-up always
    gets a clean slate. Two rules, both narrow enough that no legitimate
    compound command ("turn it on, then set it to fifty percent") is affected:

    * an **exact repeat** — same tool, same arguments — after any definitive
      result is answered from here. That is never a second instruction.
    * after :data:`MAX_FAILED_ATTEMPTS` definitive failures against the same
      target, a further action on that target is refused, so one bad name
      cannot turn into an unbounded chain of fallback calls.

    Successes do not block a *different* tool on the same target: "turn on the
    light" followed by "set it to half" is two real instructions.
    """

    exact: Dict[str, _Attempt] = field(default_factory=dict)
    failures: Dict[str, List[_Attempt]] = field(default_factory=dict)
    suppressed: int = 0

    def reset(self) -> None:
        self.exact.clear()
        self.failures.clear()

    @staticmethod
    def guards(function_name: str) -> bool:
        """Only Home Assistant state-changing intents are rate-limited here."""
        canonical = str(function_name or "").rsplit("__", 1)[-1]
        return canonical.startswith("Hass") and canonical not in READ_ONLY_INTENTS

    def check(self, function_name: str,
              arguments: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        """``None`` to dispatch, or the result to answer the model with."""
        if not self.guards(function_name):
            return None
        previous = self.exact.get(_exact_key(function_name, arguments))
        if previous is not None:
            self.suppressed += 1
            if not previous.outcome.definitive:
                detail = " ".join(str(previous.outcome.detail or "").split())[:_DETAIL_LIMIT]
                return {
                    "error": f"The outcome of {function_name} with these exact "
                             f"arguments is unknown: {detail or previous.outcome.kind}.",
                    "what_to_do": "Do not repeat the action automatically. Verify the "
                                  "current state, or ask the user for a new deliberate "
                                  "instruction before trying again.",
                    "state_changed": "unknown",
                }
            if previous.outcome.ok:
                return {
                    "error": f"{function_name} with these exact arguments already "
                             f"ran successfully during this request; it was not run "
                             f"again.",
                    "what_to_do": "Tell the user it is done. Do not call this again.",
                    "state_changed": False,
                }
            detail = " ".join(str(previous.outcome.detail or "").split())[:_DETAIL_LIMIT]
            return {
                "error": f"{function_name} with these exact arguments already "
                         f"failed during this request: {detail or previous.outcome.kind}",
                "what_to_do": "Do not repeat the identical call. Either correct the "
                              "arguments or tell the user it failed.",
                "state_changed": False,
            }
        prior = self.failures.get(_target_key(function_name, arguments)) or []
        if len(prior) >= MAX_FAILED_ATTEMPTS:
            self.suppressed += 1
            reasons = "; ".join(
                " ".join(str(a.outcome.detail or a.outcome.kind).split())[:80]
                for a in prior[-MAX_FAILED_ATTEMPTS:]
            )
            return {
                "error": f"{len(prior)} attempts to act on this target have already "
                         f"failed during this request: {reasons}. Nothing changed.",
                "what_to_do": "Stop calling tools for this. Tell the user it did not "
                              "work, or ask which device they mean.",
                "state_changed": False,
            }
        return None

    def record(self, function_name: str, arguments: Optional[Mapping[str, Any]],
               outcome: ToolOutcome) -> None:
        if not self.guards(function_name) or outcome.pending_confirmation:
            return
        attempt = _Attempt(function_name, outcome)
        # An uncertain state-changing call is the most important one to fence:
        # retrying it can duplicate a side effect that actually succeeded.
        self.exact[_exact_key(function_name, arguments)] = attempt
        if not outcome.ok and outcome.definitive:
            self.failures.setdefault(_target_key(function_name, arguments), []).append(attempt)
