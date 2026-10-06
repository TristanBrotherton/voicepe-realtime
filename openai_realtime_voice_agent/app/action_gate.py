"""Confirmation gate for consequential actions, enforced below the model.

Unlocking a door, opening a garage or gate, or disarming an alarm must not
happen on a single (possibly misheard, possibly overheard) utterance. The
gate sits in the tool-execution wrapper — the same place speaker gating
lives — so no prompt can talk the model past it:

1. A consequential call is NOT executed. The model gets a
   ``confirmation_required`` result carrying a short-lived ``confirm_id`` and
   is told to ask the user.
2. ``confirm_action(confirm_id)`` executes the original call only if
   - the request is still pending (default 30 s) on the SAME device,
   - the user has spoken again since the question (a new end-of-utterance
     on that device), and
   - no new wake started a different conversation in between.

What counts as consequential (``confirm_actions`` option, comma separated):
  lock     unlocking (Assist: HassTurnOff on the lock domain), opening locks
  garage   opening covers with device_class garage
  gate     opening covers with device_class gate
  door     opening covers with device_class door
  alarm    any alarm_control_panel action
Targets are taken from the tool arguments (domain/device_class) or resolved
from the entity name through Home Assistant's state list; a name that cannot
be resolved but reads like one of these targets ("front door", "garage") is
treated as consequential. Exposed scripts whose name/description mention
unlocking, disarming, garages or gates are gated, and ``confirm_tools`` adds
explicit tool names. Requests to the external agent (``ask_openclaw``) that
ask for such an action are gated by the same wording check.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

ALL_RULES = ("lock", "garage", "gate", "door", "alarm")
DEFAULT_RULES = ",".join(ALL_RULES)

_OPEN_INTENTS = {"HassTurnOn", "HassSetPosition", "HassOpen", "HassOpenCover"}
_UNLOCK_INTENTS = {"HassTurnOff", "HassUnlock", "HassOpen"}
_NAME_HINTS = {
    "lock": re.compile(r"\b(lock|deadbolt)\b", re.I),
    "garage": re.compile(r"\bgarage\b", re.I),
    "gate": re.compile(r"\bgate\b", re.I),
    "door": re.compile(r"\bdoor\b", re.I),
    "alarm": re.compile(r"\b(alarm|security system)\b", re.I),
}
_TOOL_TEXT_HINTS = re.compile(
    r"\b(unlock\w*|disarm\w*|garage|gates?|open\w*\s+(the\s+)?(front|back|side|garage)?\s*door)\b", re.I
)
_AGENT_REQUEST_HINTS = re.compile(
    r"\b(unlock\w*|disarm\w*|(open|raise|lift)\w*\s+(the\s+|my\s+)?(garage|gate|front door|back door|side door|door)"
    r"|(turn|switch|shut)\s+off\s+(the\s+)?(alarm|security))\b",
    re.I,
)


def _canonical_tool_name(function_name: str) -> str:
    """Return the MCP tool name without an optional server namespace.

    Home Assistant may expose intent tools as either ``HassTurnOff`` or a
    namespaced form such as ``intent__HassTurnOff``.  Safety classification
    must use the actual intent name; otherwise a namespaced intent falls
    through to the generic description scanner, where words such as "lock"
    in the broad tool description can incorrectly gate every ordinary switch
    or helper action.
    """
    return str(function_name or "").rsplit("__", 1)[-1]


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


@dataclass
class EntityInfo:
    entity_id: str
    name: str
    domain: str
    device_class: str = ""


@dataclass
class GateDecision:
    requires_confirmation: bool
    summary: str = ""
    rule: str = ""


@dataclass
class PendingAction:
    confirm_id: str
    device_id: str
    function_name: str
    arguments: Dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    summary: str
    created: float
    user_turn_seq: int
    wake_seq: int


class EntityDirectory:
    """Friendly name -> domain/device_class lookup over HA's state list (cached)."""

    def __init__(self, fetch_states: Optional[Callable[[], Awaitable[list]]] = None,
                 ttl_s: float = 120.0, clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch_states
        self._ttl = ttl_s
        self._clock = clock
        self._entities: List[EntityInfo] = []
        self._loaded_at = -1e9

    async def _refresh(self) -> None:
        if self._fetch is None or self._clock() - self._loaded_at < self._ttl:
            return
        try:
            states = await self._fetch()
        except Exception as e:
            logger.debug(f"entity directory refresh failed: {e!r}")
            return
        entities = []
        for state in states or []:
            entity_id = state.get("entity_id", "")
            attrs = state.get("attributes") or {}
            entities.append(EntityInfo(
                entity_id=entity_id,
                name=str(attrs.get("friendly_name") or entity_id),
                domain=entity_id.split(".", 1)[0],
                device_class=str(attrs.get("device_class") or ""),
            ))
        self._entities = entities
        self._loaded_at = self._clock()

    async def lookup(self, name: str) -> List[EntityInfo]:
        await self._refresh()
        needle = " ".join(str(name or "").lower().split())
        if not needle:
            return []
        exact = [e for e in self._entities if e.name.lower() == needle]
        if exact:
            return exact
        return [e for e in self._entities if needle in e.name.lower() or e.name.lower() in needle]


class ActionGate:
    def __init__(
        self,
        rules: Iterable[str] = ALL_RULES,
        extra_tools: Iterable[str] = (),
        directory: Optional[EntityDirectory] = None,
        tool_descriptions: Optional[Dict[str, str]] = None,
        window_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.rules = {r.strip().lower() for r in rules if r and r.strip()}
        self.extra_tools = {t.strip() for t in extra_tools if t and t.strip()}
        self.directory = directory or EntityDirectory()
        self.tool_descriptions = dict(tool_descriptions or {})
        self.window_s = window_s
        self._clock = clock
        self._pending: Dict[str, PendingAction] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.rules or self.extra_tools)

    async def reconcile_arguments(self, function_name: str, arguments: Optional[dict]) -> Dict[str, Any]:
        """Correct stale HA target constraints when the named entity is unambiguous.

        Assist intent tools accept domain and device-class constraints, but models
        occasionally label helpers as switches or a gate as a garage. Passing a
        stale constraint makes Home Assistant reject an otherwise exact friendly-
        name match. Use the entity directory only when it yields one target;
        ambiguous and unresolved names are left untouched. Reconciliation happens
        before the safety check, so a mislabeled lock or gate becomes *more*
        restricted, never less.
        """
        args = dict(arguments or {})
        canonical_name = _canonical_tool_name(function_name)
        name = str(args.get("name") or "").strip()
        if not canonical_name.startswith("Hass") or not name:
            return args

        matches = await self.directory.lookup(name)
        if len(matches) != 1:
            return args
        actual_domain = matches[0].domain
        actual_device_class = matches[0].device_class
        supplied_domains = {domain.lower() for domain in _as_list(args.get("domain"))}
        if supplied_domains and supplied_domains != {actual_domain}:
            args["domain"] = [actual_domain]
            logger.info(
                "reconciled Home Assistant target domain for %s: %s -> %s",
                canonical_name,
                sorted(supplied_domains),
                actual_domain,
            )

        supplied_classes = {
            device_class.lower() for device_class in _as_list(args.get("device_class"))
        }
        if (supplied_classes and actual_device_class
                and supplied_classes != {actual_device_class.lower()}):
            args["device_class"] = [actual_device_class]
            logger.info(
                "reconciled Home Assistant target device class for %s: %s -> %s",
                canonical_name,
                sorted(supplied_classes),
                actual_device_class,
            )
        return args

    # -- classification ---------------------------------------------------
    def _target_rule(self, domain: str, device_class: str, intent: str, args: dict) -> str:
        domain = domain.lower()
        device_class = device_class.lower()
        if domain == "lock" and "lock" in self.rules and intent in _UNLOCK_INTENTS:
            return "lock"
        if domain == "alarm_control_panel" and "alarm" in self.rules:
            return "alarm"
        if domain == "cover" and device_class in ("garage", "gate", "door") and device_class in self.rules:
            if intent in _OPEN_INTENTS:
                if intent == "HassSetPosition":
                    try:
                        if float(args.get("position", 100)) <= 0:
                            return ""
                    except (TypeError, ValueError):
                        pass
                return device_class
        return ""

    def _name_rule(self, name: str, intent: str) -> str:
        """Fallback when Home Assistant cannot resolve the name: judge by wording.

        "Turn off the front door" may be a lock (Assist unlocks with
        HassTurnOff), so door-like names are treated as locks for unlock
        intents and as door covers for open intents.
        """
        name = name or ""
        if ("lock" in self.rules and intent in _UNLOCK_INTENTS
                and (_NAME_HINTS["lock"].search(name) or _NAME_HINTS["door"].search(name))):
            return "lock"
        for rule in ("garage", "gate", "door"):
            if rule in self.rules and intent in _OPEN_INTENTS and _NAME_HINTS[rule].search(name):
                return rule
        if "alarm" in self.rules and _NAME_HINTS["alarm"].search(name):
            return "alarm"
        return ""

    async def check(self, function_name: str, arguments: Optional[dict]) -> GateDecision:
        args = dict(arguments or {})
        canonical_name = _canonical_tool_name(function_name)
        if canonical_name == "confirm_action" or not self.enabled:
            return GateDecision(False)
        if function_name in self.extra_tools or canonical_name in self.extra_tools:
            return GateDecision(True, f"run {function_name}", "confirm_tools")
        if canonical_name == "ask_openclaw":
            question = str(args.get("question") or "")
            if self.rules and _AGENT_REQUEST_HINTS.search(question):
                return GateDecision(True, f"ask the agent to: {question[:120]}", "agent_request")
            return GateDecision(False)
        if canonical_name.startswith("Hass"):
            domains = _as_list(args.get("domain"))
            classes = _as_list(args.get("device_class"))
            name = str(args.get("name") or "")
            for domain in domains or [""]:
                for device_class in classes or [""]:
                    rule = self._target_rule(domain, device_class, canonical_name, args)
                    if rule:
                        return GateDecision(True, self._summary(canonical_name, name or domain, rule), rule)
            if name:
                matches = await self.directory.lookup(name)
                for entity in matches:
                    rule = self._target_rule(entity.domain, entity.device_class, canonical_name, args)
                    if rule:
                        return GateDecision(True, self._summary(canonical_name, entity.name, rule), rule)
                if not matches:
                    rule = self._name_rule(name, canonical_name)
                    if rule:
                        return GateDecision(True, self._summary(canonical_name, name, rule), rule)
            return GateDecision(False)
        # Exposed scripts and other tools: judge by name + description.
        text = f"{function_name} {self.tool_descriptions.get(function_name, '')}".replace("_", " ")
        if self.rules and _TOOL_TEXT_HINTS.search(text):
            return GateDecision(True, f"run {function_name}", "script")
        return GateDecision(False)

    @staticmethod
    def _summary(intent: str, target: str, rule: str) -> str:
        verb = {"lock": "unlock", "alarm": "change the alarm"}.get(rule, "open")
        return f"{verb} {target}".strip()

    # -- pending confirmations -------------------------------------------
    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._pending.items() if now - p.created > self.window_s]:
            self._pending.pop(key, None)

    def request(self, device_id: str, function_name: str, arguments: dict,
                handler: Callable[..., Awaitable[Any]], summary: str,
                user_turn_seq: int, wake_seq: int) -> PendingAction:
        self._expire()
        for pending in self._pending.values():
            if (pending.device_id == device_id and pending.function_name == function_name
                    and pending.arguments == arguments):
                return pending
        pending = PendingAction(
            confirm_id=secrets.token_hex(3),
            device_id=device_id,
            function_name=function_name,
            arguments=dict(arguments),
            handler=handler,
            summary=summary,
            created=self._clock(),
            user_turn_seq=user_turn_seq,
            wake_seq=wake_seq,
        )
        self._pending[pending.confirm_id] = pending
        logger.info(f"🔐 confirmation required on {device_id}: {summary} (id {pending.confirm_id})")
        return pending

    def take(self, confirm_id: str, device_id: str, user_turn_seq: int, wake_seq: int):
        """Return (pending, None) when confirmation is valid, else (None, reason)."""
        self._expire()
        pending = self._pending.get(str(confirm_id or "").strip())
        if pending is None:
            return None, "no such pending action (it may have expired) — ask the user to repeat the request"
        if pending.device_id != device_id:
            return None, "that confirmation belongs to another device"
        if wake_seq != pending.wake_seq:
            self._pending.pop(pending.confirm_id, None)
            return None, "a new conversation started; the earlier request was cancelled"
        if user_turn_seq <= pending.user_turn_seq:
            return None, ("the user has not answered yet — ask the confirmation question and wait "
                          "for their reply before calling confirm_action")
        self._pending.pop(pending.confirm_id, None)
        return pending, None

    def cancel_device(self, device_id: str) -> None:
        for key in [k for k, p in self._pending.items() if p.device_id == device_id]:
            self._pending.pop(key, None)


def confirmation_result(pending: PendingAction, window_s: float) -> dict:
    return {
        "status": "confirmation_required",
        "confirm_id": pending.confirm_id,
        "action": pending.summary,
        "instructions": (
            f"This action needs the user's confirmation and has NOT been done. Ask one short "
            f"yes/no question: confirm they want to {pending.summary}. Call confirm_action with "
            f"this confirm_id only if their next reply clearly says yes. If they say no or "
            f"anything else, do not call any tool. The request expires in {int(window_s)} seconds."
        ),
    }


def get_confirm_tool_definition() -> dict:
    return {
        "type": "function",
        "name": "confirm_action",
        "description": (
            "Carry out an action that was held for confirmation, after the user clearly said "
            "yes to your confirmation question. Never call this before the user has answered."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confirm_id": {"type": "string", "description": "The confirm_id you were given"},
            },
            "required": ["confirm_id"],
        },
    }


def replace_arguments(params, function_name: str, arguments: dict):
    """Clone pipecat FunctionCallParams with the held call's name and arguments."""
    try:
        return dataclasses.replace(params, function_name=function_name, arguments=arguments)
    except TypeError:
        clone = type("HeldParams", (), {})()
        for key, value in vars(params).items():
            setattr(clone, key, value)
        clone.function_name = function_name
        clone.arguments = arguments
        return clone


def describe_args(arguments: dict) -> str:
    try:
        return json.dumps(arguments, sort_keys=True)[:200]
    except (TypeError, ValueError):
        return str(arguments)[:200]
