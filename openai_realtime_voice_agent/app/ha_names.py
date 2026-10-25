"""Match a spoken device name to the entity Home Assistant actually has.

Home Assistant's tools (``GetLiveContext``, ``HassTurnOn``...) match a target
by exact name, and filter it by the ``domain`` and ``area`` the caller passes.
The router's backend fills those in from what it heard and what it guesses:
"Is the test switch on?" became ``GetLiveContext(name='Test Switch',
domain='switch', area=<its room>)``. The entity is an
``input_boolean``, so Home Assistant matched nothing and the user heard that
the switch was "not exposed". Speech recognition misses the same way
("test P.E. switch", "the switch", a plural, a dropped word).

:func:`resolve_target` maps the spoken name onto one exposed entity only when
one entity is clearly closest (spelling, a rough sound-alike key, and every
spoken word being part of its name), then passes Home Assistant that entity's
own name and domain, and drops an area or floor the entity is not in. An exact
name that already fits is left alone, and anything ambiguous is left for Home
Assistant to answer as before.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.ha_household import fold
from app.list_names import _sound

# Accept the best entity only above this score and this far ahead of the next.
MIN_SCORE = 0.85
MIN_LEAD = 0.08
# A domain or area hint that agrees with a candidate breaks near-ties.
HINT_BONUS = 0.04
# A candidate of another domain than the caller asked for must be this close:
# "Attic light" must not become a switch called "Attic Fan Plug Switch".
OTHER_DOMAIN_MIN = 0.95
# Domains a spoken "switch" or "toggle" can mean (helpers are toggles too).
SAME_KIND = (frozenset({"switch", "input_boolean"}),)
# Words that name a kind of device, not a device: "kitchen lights" is the
# kitchen's lights (an area and a domain), not an entity to guess.
KIND_WORDS = frozenset({"light", "lights", "lamp", "lamps", "switch", "switches", "fan", "fans",
                        "blind", "blinds", "shade", "shades", "speaker", "speakers", "tv"})

_FILLER = {"a", "an", "the", "my", "our", "please"}

# Tools whose ``name`` is an entity or device. Others reuse ``name`` for
# something else (a timer, a to-do list, a broadcast) and are never touched.
_NOT_ENTITY_TOOLS = frozenset({
    "HassListAddItem", "HassListCompleteItem", "HassShoppingListAddItem",
    "HassStartTimer", "HassCancelTimer", "HassCancelAllTimers", "HassIncreaseTimer",
    "HassDecreaseTimer", "HassPauseTimer", "HassUnpauseTimer", "HassTimerStatus",
    "HassBroadcast", "HassRespond", "HassNevermind", "HassGetCurrentTime",
    "HassGetCurrentDate",
})


def is_entity_tool(function_name: str) -> bool:
    base = (function_name or "").rsplit("__", 1)[-1]
    return base == "GetLiveContext" or (base.startswith("Hass") and base not in _NOT_ENTITY_TOOLS)


def words(text: str) -> List[str]:
    """Folded words without fillers; spelled-out letters joined ("p e" -> "pe")."""
    out: List[str] = []
    run = ""
    for w in fold(text).split():
        if len(w) == 1 and w.isalpha():
            run += w
            continue
        if run:
            out.append(run)
            run = ""
        if w not in _FILLER:
            out.append(w)
    if run:
        out.append(run)
    return out


@dataclass(frozen=True)
class Entity:
    name: str                 # Home Assistant's primary name (first in the Assist prompt)
    names: Tuple[str, ...]    # the name and its aliases
    domain: str
    areas: Tuple[str, ...]


def entities_from_overview(entries: Iterable[Dict[str, Any]]) -> List[Entity]:
    """Entities from :func:`app.ha_household.parse_overview` entries."""
    out = []
    for e in entries:
        names = tuple(n.strip() for n in str(e.get("names") or "").split(",") if n.strip())
        if not names:
            continue
        areas = tuple(a.strip() for a in str(e.get("areas") or "").split(",") if a.strip())
        out.append(Entity(names[0], names, str(e.get("domain") or ""), areas))
    return out


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _holds(spoken: List[str], name: str) -> bool:
    """Every spoken word is one of the name's words."""
    return {_stem(w) for w in spoken} <= {_stem(w) for w in words(name)}


def _score(spoken: List[str], name: str, sole_holder: bool = False) -> float:
    real = words(name)
    if not real:
        return 0.0
    a, b = " ".join(_stem(w) for w in spoken), " ".join(_stem(w) for w in real)
    plain = difflib.SequenceMatcher(None, a, b).ratio()
    sound = difflib.SequenceMatcher(None, _sound(a), _sound(b)).ratio()
    score = (plain + sound) / 2
    # Every spoken word is part of this name and of no other entity's ("test
    # switch" -> "Lab PE Test Switch"). Weighted by how much of the name
    # was said. A word shared by several entities ("powerwall") never wins.
    if sole_holder:
        said, have = {_stem(w) for w in spoken}, {_stem(w) for w in real}
        if said <= have:
            score = max(score, 0.7 + 0.3 * len(said) / len(have))
    return score


def _same_kind(a: str, b: str) -> bool:
    return a == b or any(a in kind and b in kind for kind in SAME_KIND)


def _hints(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _same(a: str, b: str) -> bool:
    return fold(a) == fold(b)


def _area_request(spoken: List[str], area_names: Iterable[str]) -> bool:
    """"kitchen lights", "the office": a room (plus kinds of device), which
    Home Assistant resolves itself from the area and domain."""
    rest = [w for w in spoken if w not in KIND_WORDS]
    return bool(rest) and any(rest == words(a) for a in area_names)


def best_entity(spoken_name: str, entities: List[Entity], domains: List[str] = (),
                areas: List[str] = (), area_names: Iterable[str] = ()
                ) -> Optional[Tuple[Entity, float, bool]]:
    """``(entity, score, exact)`` for the one entity ``spoken_name`` means,
    or None when no entity is clearly closest."""
    spoken = words(spoken_name)
    if not spoken:
        return None
    exact = [e for e in entities if any(words(n) == spoken for n in e.names)]
    if exact:
        fits = [e for e in exact if (not domains or e.domain in domains)
                and (not areas or any(_same(a, x) for a in areas for x in e.areas))
                and any(n.casefold() == spoken_name.strip().casefold() for n in e.names)]
        if fits:
            return None  # Home Assistant will find it exactly as asked
        if len({e.name for e in exact}) == 1 and len(exact) == 1:
            return exact[0], 1.0, True
        return None
    if _area_request(spoken, area_names):
        return None
    holders = [e for e in entities if any(_holds(spoken, n) for n in e.names)]
    sole = holders[0] if len(holders) == 1 else None
    ranked = []
    for e in entities:
        score = max(_score(spoken, n, e is sole) for n in e.names)
        if domains and not any(_same_kind(d, e.domain) for d in domains):
            if score < OTHER_DOMAIN_MIN:
                continue
        elif domains:
            score += HINT_BONUS
        if areas and any(_same(a, x) for a in areas for x in e.areas):
            score += HINT_BONUS
        ranked.append((score, e))
    ranked.sort(key=lambda r: r[0], reverse=True)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_LEAD:
        return None
    return ranked[0][1], ranked[0][0], False


def resolve_target(function_name: str, arguments: Dict[str, Any], entities: List[Entity],
                   floors_of_area: Optional[Dict[str, List[str]]] = None
                   ) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(arguments, note)``: the arguments with the spoken name, domain, area
    and floor fitted to the one entity meant, and a log note when anything
    changed. Unchanged (note None) when the tool does not target an entity,
    no name was given, or the match is not clear."""
    if not entities or not is_entity_tool(function_name) or not isinstance(arguments, dict):
        return arguments, None
    spoken = arguments.get("name")
    if not isinstance(spoken, str) or not spoken.strip():
        return arguments, None
    domains = _hints(arguments.get("domain"))
    areas = _hints(arguments.get("area"))
    area_names = set(floors_of_area or ()) | {a for e in entities for a in e.areas}
    found = best_entity(spoken, entities, domains, areas, area_names)
    if found is None:
        return arguments, None
    entity, score, exact = found
    out = dict(arguments)
    changes = []
    if not any(n.casefold() == spoken.strip().casefold() for n in entity.names):
        out["name"] = entity.name
        changes.append(f"name {spoken!r} -> {entity.name!r} ({score:.2f})")
    if domains and entity.domain and entity.domain not in domains:
        out["domain"] = [entity.domain] if isinstance(arguments.get("domain"), list) else entity.domain
        changes.append(f"domain {','.join(domains)} -> {entity.domain}")
    if areas and not any(_same(a, x) for a in areas for x in entity.areas):
        out.pop("area", None)
        changes.append(f"area {','.join(areas)} dropped (entity is in {', '.join(entity.areas) or 'no area'})")
    floor = arguments.get("floor")
    if isinstance(floor, str) and floor.strip() and floors_of_area is not None:
        entity_floors = [f for a in entity.areas for f in floors_of_area.get(fold(a)) or []]
        if not any(_same(floor, f) for f in entity_floors):
            out.pop("floor", None)
            changes.append(f"floor {floor} dropped")
    if not changes:
        return arguments, None
    return out, "; ".join(changes)
