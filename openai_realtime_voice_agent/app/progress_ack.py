"""Deterministic conversational progress acknowledgements.

The acknowledgement is not a success confirmation.  It says what the
assistant is starting while the real tool/model work continues.  Phrases are
derived from structured tool arguments when available, with a conservative
transcript fallback before the model has selected a tool.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from app.live_evidence import tool_name_tokens


_SPACE = re.compile(r"\s+")
_REQUEST_STARTS = (
    "what ", "where ", "when ", "who ", "why ", "how ", "is ", "are ",
    "can ", "could ", "would ", "will ", "do ", "does ", "did ",
    "tell ", "find ", "check ", "look ", "search ", "add ", "remove ",
    "delete ", "turn ", "switch ", "set ", "start ", "stop ", "play ",
    "pause ", "resume ", "open ", "close ", "lock ", "unlock ", "vacuum ",
)
_NON_REQUESTS = {
    "oh", "wow", "ugh", "huh", "hmm", "jesus", "thanks", "thank you",
    "never mind", "nevermind",
}
_INSTANT_LOCAL = re.compile(
    # Any standalone time/date wording stays silent. Before structured tool
    # selection we cannot distinguish a clock lookup from a web lookup that
    # happens to say “search”; false silence is safer than an irreversible cue
    # before a late GetDateTime choice.
    r"\b(?:time|date)\b|\bwhat day is it\b",
    re.IGNORECASE,
)

# A person-location request is recognised by its *shape*, never by who lives
# in the house: a "where is …" question or a "find/locate …" imperative whose
# object is a person — a third-person pronoun, or a capitalised name in the
# transcript as the model cased it. No name is special-cased, so the cue is
# identical for everyone and this public add-on carries nobody's name. A name
# the transcriber left lower-case simply does not match, and the turn falls
# back to waiting for structured tool evidence: the safe direction to be
# wrong in, because that path can only be quieter, never wrong out loud.
_WHERE = re.compile(r"\bwhere(?:['’]s| is| are)\b")
_PERSON_PRONOUN = re.compile(r"\b(?:she|he|they|her|him|them)\b")
_WHERE_NAME = re.compile(r"(?i:\bwhere(?:['’]s| is| are)\s+)([A-Z][a-z]{1,19})\b")
_FIND_PRONOUN = re.compile(r"\b(?:find|locate)\s+(?:her|him|them)\b")
_FIND_NAME = re.compile(r"(?i:\b(?:find|locate)\s+)([A-Z][a-z]{1,19})\b")
PERSON_LOCATION_CUE = "I’ll find them, let me look."


def _clean(value: Any, limit: int = 56) -> str:
    """Short speakable text, or empty when an argument is not safely useful."""
    if not isinstance(value, str):
        return ""
    text = _SPACE.sub(" ", value).strip(" \t\r\n.,;:!?\"'`")
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        return ""
    # Structured arguments occasionally contain entity ids.  They sound awful
    # and leak implementation detail; fall back to the area/domain instead.
    if "." in text and re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", text.lower()):
        return ""
    return text


def _bare(tool_name: str) -> str:
    return (tool_name or "").rsplit("__", 1)[-1]


def _name_suffix(speaker_name: Optional[str]) -> str:
    name = _clean(speaker_name, 24)
    return f", {name}" if name else ""


def looks_like_request(text: str) -> bool:
    """Conservative gate for the pre-tool acknowledgement."""
    value = _SPACE.sub(" ", (text or "").strip()).lower()
    if len(value) < 3 or value in _NON_REQUESTS:
        return False
    return value.endswith("?") or value.startswith(_REQUEST_STARTS)


def looks_like_instant_local(text: str) -> bool:
    """Requests whose direct local answer must not get a progress line."""
    return bool(_INSTANT_LOCAL.search(text or ""))


def looks_like_person_location(transcript: str) -> bool:
    """Is this a request for where a person is, judged only by its shape?"""
    value = _SPACE.sub(" ", (transcript or "").strip()).lower()
    if _WHERE.search(value) and (_PERSON_PRONOUN.search(value)
                                or _WHERE_NAME.search(transcript or "")):
        return True
    return bool(_FIND_PRONOUN.search(value) or _FIND_NAME.search(transcript or ""))


def early_progress_phrase(transcript: str) -> Optional[str]:
    """Return a pre-tool cue only for strong, predictably slow lookup intents.

    This deliberately does not cover generic questions or device actions.  It
    lets obvious searches feel responsive before backend tool selection while
    preserving the no-cue guarantee for time/date and other fast local reads.
    The returned phrases are finite and pre-warmed by ``SpokenPrompts``.
    """
    value = _SPACE.sub(" ", (transcript or "").strip()).lower()
    if not value or looks_like_instant_local(value):
        return None
    if looks_like_person_location(transcript):
        return PERSON_LOCATION_CUE
    lookup_markers = (
        "search the web", "web search", "look up", "check online",
        "find out", "today's weather", "todays weather", "the weather",
        "latest news", "current news",
    )
    if any(marker in value for marker in lookup_markers):
        return "Let me check, one second."
    return None


# The finite progress cues. When the assistant speaks in a Live-only voice,
# a filler cannot be synthesized at turn time (the speech endpoint does not
# offer that voice), so every cue the runtime can play is one of these,
# rendered once in the assistant's own voice and cached. Each states what is
# starting, names nobody, carries no gender and never claims success.
GENERIC_CUES: dict[str, str] = {
    "progress_check_that": "Let me check that.",
    "progress_turn_on": "I’ll turn that on for you.",
    "progress_turn_off": "I’ll turn that off for you.",
    "progress_list_add": "Adding that to the list.",
    "progress_list_update": "Updating the list.",
    "progress_adjust": "Adjusting that now.",
    "progress_stop": "Stopping that.",
    "progress_media": "Putting that on for you.",
    "progress_timer_set": "Setting the timer.",
    "progress_timer_cancel": "Cancelling the timer.",
    "progress_vacuum_start": "Starting the vacuum.",
    "progress_vacuum_home": "Sending the vacuum home.",
    "progress_car_climate": "Starting the car climate.",
}


def generic_progress_phrase(tool_name: str = "", transcript: str = "") -> str:
    """One of ``GENERIC_CUES``, from the tool's shape, else the request's.

    The same selection as :func:`progress_phrase` without the structured
    target or the speaker's name, so the result is always a pre-rendered
    phrase and never a sentence that has to be synthesized while the user
    waits.
    """
    bare = _bare(tool_name)
    lower = (transcript or "").lower()
    if bare in {"HassTurnOn", "HassLightSet"}:
        return GENERIC_CUES["progress_turn_on"]
    if bare == "HassTurnOff":
        return GENERIC_CUES["progress_turn_off"]
    if bare in {"HassListAddItem", "add_item"}:
        return GENERIC_CUES["progress_list_add"]
    if bare in {"HassListRemoveItem", "remove_item", "HassListCompleteItem", "complete_item"}:
        return GENERIC_CUES["progress_list_update"]
    if bare in {"HassSetPosition", "HassFanSetSpeed", "HassClimateSetTemperature",
                "HassSetVolume", "HassSetVolumeRelative"}:
        return GENERIC_CUES["progress_adjust"]
    if bare in {"HassStopMoving", "HassMediaPause"}:
        return GENERIC_CUES["progress_stop"]
    if bare in {"HassMediaUnpause", "HassMediaSearchAndPlay", "HassMediaNext", "HassMediaPrevious"}:
        return GENERIC_CUES["progress_media"]
    if bare == "set_timer":
        return GENERIC_CUES["progress_timer_set"]
    if bare in {"cancel_timer", "HassCancelAllTimers"}:
        return GENERIC_CUES["progress_timer_cancel"]
    if bare in {"HassVacuumStart", "HassVacuumCleanArea"}:
        return GENERIC_CUES["progress_vacuum_start"]
    if bare == "HassVacuumReturnToBase":
        return GENERIC_CUES["progress_vacuum_home"]
    if {"car", "climate", "on"} <= tool_name_tokens(tool_name):
        return GENERIC_CUES["progress_car_climate"]
    if not bare:
        if "shopping" in lower and any(word in lower for word in ("add ", "put ")):
            return GENERIC_CUES["progress_list_add"]
        if any(phrase in lower for phrase in ("turn on", "switch on")):
            return GENERIC_CUES["progress_turn_on"]
        if any(phrase in lower for phrase in ("turn off", "switch off")):
            return GENERIC_CUES["progress_turn_off"]
        if "timer" in lower and any(word in lower for word in ("set ", "start ")):
            return GENERIC_CUES["progress_timer_set"]
    return GENERIC_CUES["progress_check_that"]


def _target(arguments: Mapping[str, Any]) -> str:
    area = _clean(arguments.get("area"))
    name = _clean(arguments.get("name"))
    floor = _clean(arguments.get("floor"))
    return area or name or floor


def _domains(arguments: Mapping[str, Any]) -> set[str]:
    raw = arguments.get("domain")
    if isinstance(raw, str):
        return {raw.lower()}
    if isinstance(raw, (list, tuple, set)):
        return {str(value).lower() for value in raw if value}
    return set()


def _device_target(arguments: Mapping[str, Any]) -> str:
    area = _clean(arguments.get("area"))
    name = _clean(arguments.get("name"))
    target = area or name or _clean(arguments.get("floor"))
    domains = _domains(arguments)
    if "light" in domains:
        if area:
            return f"the {area} lights"
        if name:
            return name if name.lower().startswith("the ") else f"the {name}"
        return "the lights"
    if target:
        return target if target.lower().startswith("the ") else f"the {target}"
    return "that"


def progress_phrase(tool_name: str = "", arguments: Optional[Mapping[str, Any]] = None,
                    speaker_name: Optional[str] = None, transcript: str = "") -> str:
    """Return one in-progress phrase; never a claim that the action succeeded."""
    args = arguments or {}
    bare = _bare(tool_name)
    suffix = _name_suffix(speaker_name)
    lower = (transcript or "").lower()

    if bare in {"web_search", "ask_openclaw", "recall_memory", "GetLiveContext",
                "GetDateTime"}:
        return f"Let me check that{suffix}."

    if bare in {"HassListAddItem", "add_item"}:
        item = _clean(args.get("item"))
        list_name = _clean(args.get("list") or args.get("list_name"))
        destination = list_name or "the shopping list"
        if destination and not destination.lower().startswith("the "):
            destination = f"the {destination}"
        subject = item or "that"
        return f"Adding {subject} to {destination}{suffix}."
    if bare in {"HassListRemoveItem", "remove_item"}:
        item = _clean(args.get("item")) or "that"
        return f"Removing {item} from the list{suffix}."
    if bare in {"HassListCompleteItem", "complete_item"}:
        item = _clean(args.get("item")) or "that"
        return f"Marking {item} complete{suffix}."

    if bare in {"HassTurnOn", "HassLightSet"}:
        return f"I’ll turn on {_device_target(args)} for you{suffix}."
    if bare == "HassTurnOff":
        return f"I’ll turn off {_device_target(args)} for you{suffix}."
    if bare in {"HassSetPosition", "HassFanSetSpeed", "HassClimateSetTemperature",
                "HassSetVolume", "HassSetVolumeRelative"}:
        return f"Adjusting {_device_target(args)}{suffix}."
    if bare in {"HassStopMoving", "HassMediaPause"}:
        return f"Stopping {_device_target(args)}{suffix}."
    if bare in {"HassMediaUnpause", "HassMediaSearchAndPlay"}:
        return f"Putting that on for you{suffix}."
    if bare in {"HassMediaNext", "HassMediaPrevious"}:
        return f"Changing that for you{suffix}."

    if bare == "set_timer":
        return f"Setting the timer{suffix}."
    if bare in {"cancel_timer", "HassCancelAllTimers"}:
        return f"Cancelling the timer{suffix}."
    if bare == "HassVacuumStart":
        return f"Starting the vacuum{suffix}."
    if bare == "HassVacuumReturnToBase":
        return f"Sending the vacuum home{suffix}."
    if bare == "HassVacuumCleanArea":
        return f"Starting that cleaning job{suffix}."
    # A car-climate script, matched on its name's *tokens* so that no one
    # installation's script id becomes a public default. This is the one
    # house-specific cue that survives the privacy change, because it names a
    # thing rather than a person: `CarClimateOn` and any vendor-prefixed
    # `…car…climate…on` id both reach it.
    if {"car", "climate", "on"} <= tool_name_tokens(tool_name):
        return f"Starting the car climate{suffix}."

    # Before a structured tool call exists, use only narrow intent cues.  The
    # wording remains a progress statement, never a success claim.
    if "shopping" in lower and any(word in lower for word in ("add ", "put ")):
        return f"Adding that to the shopping list{suffix}."
    if any(phrase in lower for phrase in ("turn on", "switch on")):
        return f"I’ll turn that on for you{suffix}."
    if any(phrase in lower for phrase in ("turn off", "switch off")):
        return f"I’ll turn that off for you{suffix}."
    if "timer" in lower and any(word in lower for word in ("set ", "start ")):
        return f"Setting the timer{suffix}."
    return f"Let me check that{suffix}."
