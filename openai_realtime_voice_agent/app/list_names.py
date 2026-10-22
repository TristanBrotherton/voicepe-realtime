"""Match a spoken to-do list name to the household's real lists.

Home Assistant's ``HassListAddItem`` / ``HassListCompleteItem`` match the
list by exact name, but the name reaches them through speech recognition. A
short, unusual list name was heard spelled differently, Home Assistant
answered ``MatchFailedReason.NAME`` and the item was not added. Curly apostrophes
("Annie’s"), a trailing "list" ("the packing list") and plurals miss the
same way.

:func:`resolve_list_name` maps the spoken name to one real list only when one
list is clearly closest, by spelling and by a rough sound-alike key; anything
ambiguous is left for Home Assistant to reject and the model to ask about.
"""
from __future__ import annotations

import difflib
import re
from typing import Iterable, Optional

# Accept the best list only above this score and this far ahead of the next.
MIN_SCORE = 0.7
MIN_LEAD = 0.1

_FILLER = {"a", "an", "the", "my", "our", "list", "lists"}


def _words(name: str) -> str:
    text = str(name or "").lower().replace("’", "'").replace("'", "")
    return " ".join(w for w in re.findall(r"[a-z0-9]+", text) if w not in _FILLER)


def _sound(words: str) -> str:
    """A rough sound-alike key: "teenie", "tini" and "tinny" share one."""
    s = re.sub(r"[^a-z]", "", words)
    for a, b in (("ph", "f"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("z", "s")):
        s = s.replace(a, b)
    s = re.sub(r"c(?=[eiy])", "s", s).replace("c", "k")
    s = re.sub(r"[aeiouy]+", "a", s)
    return re.sub(r"(.)\1+", r"\1", s)


def _score(spoken: str, real: str) -> float:
    plain = difflib.SequenceMatcher(None, spoken, real).ratio()
    sound = difflib.SequenceMatcher(None, _sound(spoken), _sound(real)).ratio()
    return (plain + sound) / 2


def resolve_list_name(spoken: str, lists: Iterable[str]) -> Optional[str]:
    """The real list name meant by ``spoken``, or None when unclear.

    The backend has passed its guesses as one name ("tini, tinny, tenny,
    hardware"); those resolve when every guess that matches a list
    matches the same one.
    """
    lists = [name for name in lists if str(name or "").strip()]
    guesses = [g for g in re.split(r",|/|\bor\b", str(spoken or ""), flags=re.I) if g.strip()]
    if len(guesses) > 1 and not any(_words(n) == _words(spoken) for n in lists):
        found = {_resolve_one(g, lists) for g in guesses} - {None}
        return found.pop() if len(found) == 1 else None
    return _resolve_one(spoken, lists)


def _resolve_one(spoken: str, lists: list) -> Optional[str]:
    want = _words(spoken)
    if not want:
        return None
    exact = [name for name in lists if _words(name) == want]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    ranked = sorted(((_score(want, _words(name)), name) for name in lists), reverse=True)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_LEAD:
        return None
    return ranked[0][1]
