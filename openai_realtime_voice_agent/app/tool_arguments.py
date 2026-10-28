"""Shape delegated tool arguments to what the tool's schema actually accepts.

Why this exists
---------------

GPT-Live's delegated backend can fill in **every** optional parameter of a
tool, using a type-appropriate placeholder when it has no value to supply. A
captured failure had this protocol shape (with synthetic entity names)::

    light__HassLightSet {"name": "Atrium Lamp", "area": "Kitchen", "floor": "",
                         "domain": ["light"], "color": "", "temperature": 0,
                         "brightness": 100}
    intent__HassTurnOn  {"name": "Atrium Lamp", "area": "Kitchen", "floor": "",
                         "domain": ["light"], "device_class": []}

Both came back ``Error calling tool: Received invalid slot info for ...`` and
Atrium Lamp stayed off. Every placeholder is schema-legal in the MCP tool
definition — ``floor`` is just ``{"type": "string"}`` — but Home Assistant
validates intent slots a second time, and that schema is stricter. Checked
against Home Assistant's intent validation with the payload shapes above:

===================================================  ==================================================
Payload                                              Home Assistant's verdict
===================================================  ==================================================
``HassLightSet`` as GPT-Live sent it                 ``string value is empty at 'floor.value'``
...with ``floor`` removed                            ``not a valid value: Unknown color at 'color.value'``
...with ``floor`` and ``color`` removed              accepted
``HassTurnOn`` as GPT-Live sent it                   ``string value is empty at 'floor.value'``
...with ``floor`` removed                            accepted
===================================================  ==================================================

The cause is ``DynamicServiceIntentHandler.slot_schema``, which every ``Hass*``
intent inherits::

    {vol.Any("name", "area", "floor"): non_empty_string, ...}

``non_empty_string`` rejects ``""`` and whitespace. So **one** empty-string
placeholder makes every Home Assistant action fail, before any entity is even
resolved — which is why the model could see the whole tool lifecycle complete
(observed, dispatched, submitted, continued, 0 abandoned) and still truthfully
report that control had failed.

What is removed, and what is not
--------------------------------

Only placeholders, and only from parameters the schema does not require:

* ``""`` and whitespace-only strings — never meaningful to any tool here;
* ``[]`` and ``{}`` — an empty constraint list is the same as no constraint.
  (``device_class: []`` happens to pass Home Assistant; it is still noise, and
  ``device_class: ""`` does *not* pass, so both are normalised away.)
* a zero that is **out of the parameter's domain**, listed explicitly in
  :data:`PLACEHOLDER_ZEROS` rather than guessed.

Everything else is passed through untouched. In particular a meaningful zero is
kept: ``brightness: 0`` is "off", ``position: 0`` is "closed",
``volume_level: 0`` is "muted", and ``HassClimateSetTemperature.temperature: 0``
is a legitimate setpoint in Celsius. Those parameters are all declared with
``minimum: 0, maximum: 100`` (or no bounds at all) in the real MCP schemas;
``HassLightSet.temperature`` is the only one declared ``{"minimum": 0}`` with no
maximum, because it is colour temperature in Kelvin, where 0 is not a colour.

A required parameter is never removed even when it is empty: dropping it would
turn an honest "that value is empty" into a confusing "that parameter is
missing".

Never broaden a call
--------------------

One removal is not safe in general. Home Assistant resolves an intent's target
from ``name``/``area``/``floor``, and when **none** of them is supplied it
matches *every* exposed entity the other constraints allow. So dropping an
empty ``name`` from ``HassTurnOff {"name": "", "domain": ["light"]}`` would
turn "act on nothing", which Home Assistant rejects, into "act on every light"
— a far worse outcome than the bug being fixed.

So a targeting parameter is only dropped while at least one *real* target
remains. That is the canary's case (``floor: ""`` alongside a real ``name`` and
``area``); a call whose only target is empty keeps it and is rejected, exactly
as it is today.

With that rule, sanitizing can only ever *narrow* or leave unchanged what a
call acts on: nothing non-empty is added or reordered, and a value is only
reshaped to its declared type (``"light"`` -> ``["light"]``, ``"40"`` -> ``40``),
never given another meaning. The speaker and
action gates read the sanitized arguments, so no confirmation can be skipped by
it. See ``app/tool_guards.py``.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

# (canonical tool name, parameter) pairs whose zero is a placeholder rather
# than a value, because zero is outside the parameter's domain. Keyed on the
# canonical name so a server namespace change cannot silently disable it.
# How Home Assistant decides what an intent acts on. With none of these
# supplied it matches everything the remaining constraints allow, so an empty
# one is only dropped while a real one survives.
TARGET_PARAMETERS = ("name", "area", "floor")

PLACEHOLDER_ZEROS = frozenset({
    # Colour temperature in Kelvin. 0 K is not a colour; the real schema is
    # {"minimum": 0, "type": "integer"} with no maximum, and Home Assistant's
    # cv.positive_int accepts it, so it reaches light.turn_on as a real request.
    ("HassLightSet", "temperature"),
})


def canonical_tool_name(function_name: str) -> str:
    """``light__HassLightSet`` -> ``HassLightSet``; anything else unchanged."""
    return str(function_name or "").rsplit("__", 1)[-1]


def _parameters(schema: Optional[Mapping[str, Any]]) -> Tuple[Dict[str, Any], set]:
    """``(properties, required)`` from one OpenAI-format function tool."""
    if not isinstance(schema, Mapping):
        return {}, set()
    params = schema.get("parameters")
    if not isinstance(params, Mapping):
        return {}, set()
    properties = params.get("properties")
    required = params.get("required")
    return (
        dict(properties) if isinstance(properties, Mapping) else {},
        set(required) if isinstance(required, (list, tuple, set)) else set(),
    )


def _is_placeholder(function_name: str, key: str, value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    if isinstance(value, bool):
        return False  # False is a value, not an absence
    if isinstance(value, (int, float)):
        if value != 0:
            return False
        return (canonical_tool_name(function_name), key) in PLACEHOLDER_ZEROS
    return False


def _types(spec: Any) -> set:
    """The JSON types one property schema allows (``type``, a type list, or
    ``anyOf``/``oneOf`` branches); empty when the schema does not say."""
    if not isinstance(spec, Mapping):
        return set()
    kind = spec.get("type")
    types = {kind} if isinstance(kind, str) else {t for t in kind if isinstance(t, str)} \
        if isinstance(kind, (list, tuple)) else set()
    for key in ("anyOf", "oneOf"):
        for branch in spec.get(key) or []:
            types |= _types(branch)
    return types


def _fits(value: Any, types: set) -> bool:
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, int):
        return bool(types & {"integer", "number"})
    if isinstance(value, float):
        return "number" in types or ("integer" in types and value.is_integer())
    if isinstance(value, str):
        return "string" in types
    if isinstance(value, (list, tuple)):
        return "array" in types
    if isinstance(value, Mapping):
        return "object" in types
    return value is None and "null" in types


_INTEGER = re.compile(r"([+-]?\d+)(?:\.0+)?")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")


def coerce_to_schema(value: Any, spec: Any) -> Any:
    """``value`` in the type its property schema declares, when it is a plain
    form of it: a scalar where a list is declared becomes a one-item list, a
    numeric string where a number is declared becomes that number. Anything
    else is returned unchanged (the server then rejects it, and that is
    reported as a failure).

    Booleans are read from "true"/"false" where a boolean is declared.

    Home Assistant's MCP tools type ``domain`` and ``device_class`` as arrays;
    the router and the voice both send ``"domain": "light"``, which Home
    Assistant refused with "Input validation error: 'light' is not of type
    'array'" (Kitchen 2026-10-28: the garden lights stayed on)."""
    types = _types(spec)
    if not types or _fits(value, types):
        return value
    if "array" in types and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        items = spec.get("items") if isinstance(spec, Mapping) else None
        return [coerce_to_schema(value, items) if items else value]
    if isinstance(value, str):
        text = value.strip()
        whole = _INTEGER.fullmatch(text)
        if "integer" in types and whole:
            return int(whole.group(1))  # never through float: no overflow, no lost digits
        if "number" in types and _NUMBER.fullmatch(text):
            number = float(text)
            if number == number and abs(number) != float("inf"):
                return number
        if "boolean" in types and text.lower() in ("true", "false"):
            return text.lower() == "true"
    return value


def sanitize_tool_arguments(
    function_name: str,
    arguments: Optional[Mapping[str, Any]],
    schema: Optional[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Drop placeholder arguments and fit the rest to the schema's types
    (:func:`coerce_to_schema`). Returns ``(arguments, removed_keys)``.

    ``schema`` is the tool's own OpenAI-format definition, used to leave
    required parameters alone and to read each parameter's type. Without it every placeholder is dropped, which
    is the safe direction: none of the tools in this add-on has a required
    parameter for which an empty string is a legitimate value.

    Pure: the input mapping is never modified, and key order is preserved so a
    log of the remaining keys stays comparable across turns.
    """
    args = dict(arguments or {})
    if not args:
        return args, []
    properties, required = _parameters(schema)
    keep_targets = not any(
        key in args and not _is_placeholder(function_name, key, args[key])
        for key in TARGET_PARAMETERS
    )
    clean: Dict[str, Any] = {}
    removed: List[str] = []
    for key, value in args.items():
        placeholder = _is_placeholder(function_name, key, value)
        if key in required or (keep_targets and key in TARGET_PARAMETERS):
            # Kept as sent: a required parameter, or a target whose removal
            # would widen the call from "nothing" to "everything" (let it be
            # rejected instead). A placeholder is never reshaped ("" stays "",
            # so Home Assistant's "that value is empty" stays honest).
            clean[key] = value if placeholder else coerce_to_schema(value, properties.get(key))
            continue
        if placeholder:
            removed.append(key)
            continue
        clean[key] = coerce_to_schema(value, properties.get(key))
    return clean, removed


def tool_schema_index(tools: Optional[List[Mapping[str, Any]]]) -> Dict[str, Mapping[str, Any]]:
    """``{tool name: definition}`` for the tools declared to the session."""
    index: Dict[str, Mapping[str, Any]] = {}
    for tool in tools or []:
        if isinstance(tool, Mapping):
            name = tool.get("name")
            if isinstance(name, str) and name:
                index[name] = tool
    return index
