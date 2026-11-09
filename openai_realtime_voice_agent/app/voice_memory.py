"""Voice-instructed persistent memory: the household teaches the assistant.

"Remember that..." appends a note to /share/voice-memory/memory.md (shared by
all device instances, survives rebuilds). Notes are speaker-attributed via the
voice verdict and folded into the system instructions at every session
creation, so spoken guidance becomes standing behavior — self-improvement by
voice. "Forget..." removes matching notes; "what do you remember" lists them.

Guardrails: only identified household speakers can add/remove (unknown/guest
voices are refused); hard caps keep the prompt overhead bounded.
"""
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, Dict, List, Optional

from app.transcript_logger import spoken_for_log

logger = logging.getLogger(__name__)

MEM_PATH = "/share/voice-memory/memory.md"
MAX_NOTES = 60
MAX_NOTE_CHARS = 300
# "Forget ..." needs a phrase of at least this many letters or digits, matched
# as whole words, and asks before removing more than FORGET_UNCONFIRMED_MAX
# notes or every note there is.
FORGET_MIN_CHARS = 3
FORGET_UNCONFIRMED_MAX = 2
_ATTRIBUTION = re.compile(r"\s*\(from [^()]*, \d{4}-\d{2}-\d{2}\)\s*$")


def _load() -> list:
    try:
        lines = [l for l in open(MEM_PATH).read().splitlines() if l.startswith("- ")]
        return lines[-MAX_NOTES:]
    except FileNotFoundError:
        return []


def _note_text(line: str) -> str:
    """A stored line without its "- " bullet and its attribution."""
    return _ATTRIBUTION.sub("", line[2:] if line.startswith("- ") else line).strip()


def _matching(lines: List[str], phrase: str) -> List[str]:
    """Notes whose text holds ``phrase`` as whole words (case-insensitive).
    The attribution is not searched: naming a person does not match every
    note they made."""
    words = phrase.split()
    pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)",
                         re.IGNORECASE)
    return [l for l in lines if pattern.search(_note_text(l))]


def _backup() -> None:
    """Keep the file as it was before a removal (memory.md.bak)."""
    if os.path.exists(MEM_PATH):
        shutil.copyfile(MEM_PATH, MEM_PATH + ".bak")


def _save(lines: list) -> None:
    os.makedirs(os.path.dirname(MEM_PATH), exist_ok=True)
    with open(MEM_PATH, "w") as f:
        f.write("# Voice-instructed memory (managed by the assistant)\n")
        f.write("\n".join(lines[-MAX_NOTES:]) + "\n")


def memory_instructions() -> str:
    """The block appended to session instructions."""
    notes = _load()
    if not notes:
        return ""
    return (
        "\n\nHOUSEHOLD MEMORY (standing notes the household asked you to keep; "
        "follow them):\n" + "\n".join(notes)
    )


def get_memory_tool_definitions() -> list:
    return [
        {"type": "function", "name": "remember",
         "description": ("Store a standing note/instruction/preference the household asks you "
                         "to remember ('remember that...', 'from now on...'). It becomes part "
                         "of your instructions in every future conversation."),
         "parameters": {"type": "object", "properties": {
             "note": {"type": "string", "description": "The note, concise, third person"}},
             "required": ["note"]}},
        {"type": "function", "name": "forget",
         "description": ("Remove remembered notes matching a phrase ('forget about the bins'). "
                         "The result lists the notes removed; read them back. If it asks for "
                         "confirmation, read the notes it would remove and call again with "
                         "confirm=true only after the user says yes."),
         "parameters": {"type": "object", "properties": {
             "matching": {"type": "string", "description": "Word/phrase identifying the note(s), "
                                                           "as the note words it"},
             "confirm": {"type": "boolean", "description": "The user confirmed removing the "
                                                           "notes a previous call listed"}},
             "required": ["matching"]}},
        {"type": "function", "name": "list_memories",
         "description": "List everything the household asked you to remember.",
         "parameters": {"type": "object", "properties": {}}},
    ]


def register_memory_tools(llm, get_speaker_name: Optional[Callable[[], Optional[str]]]) -> None:
    def _speaker() -> Optional[str]:
        if get_speaker_name is None:
            return None
        try:
            return get_speaker_name()
        except Exception:
            return None

    async def _remember(params) -> None:
        note = ((params.arguments or {}).get("note") or "").strip()[:MAX_NOTE_CHARS]
        who = _speaker()
        if not who:
            await params.result_callback({"error": (
                "Memory changes are reserved for identified household members, and "
                "the current speaker's voice was not recognized. Decline politely.")})
            return
        if not note:
            await params.result_callback({"error": "empty note"})
            return
        lines = _load()
        lines.append(f"- {note} (from {who}, {time.strftime('%Y-%m-%d')})")
        _save(lines)
        logger.info(f"🧠 remembered ({who}): {spoken_for_log(note)}")
        await params.result_callback({"status": "remembered",
                                      "note": "Active in all future conversations; confirm briefly."})

    async def _forget(params) -> None:
        pat = ((params.arguments or {}).get("matching") or "").strip()
        who = _speaker()
        if not who:
            await params.result_callback({"error": "Memory changes are reserved for identified household members."})
            return
        if len(re.sub(r"\W", "", pat)) < FORGET_MIN_CHARS:
            await params.result_callback({"error": (
                "Say which note to forget: a word or phrase from it. Nothing was removed.")})
            return
        confirmed = (params.arguments or {}).get("confirm") is True
        lines = _load()
        matched = _matching(lines, pat)
        if not matched:
            await params.result_callback({"removed": 0, "removed_notes": [],
                                          "note": "No note matches; nothing was removed."})
            return
        if not confirmed and (len(matched) > FORGET_UNCONFIRMED_MAX
                              or (len(matched) == len(lines) and len(lines) > 1)):
            logger.info(f"🧠 forget of {len(matched)} note(s) waits for a confirmation ({who})")
            await params.result_callback({
                "needs_confirmation": True,
                "would_remove": [_note_text(l) for l in matched],
                "note": ("Nothing was removed yet. Read these notes back and ask whether to "
                         "forget all of them; only then call forget again with confirm=true."),
            })
            return
        keep = [l for l in lines if l not in matched]
        _backup()
        _save(keep)
        logger.info(f"🧠 forgot {len(matched)} note(s) matching {spoken_for_log(pat)} ({who})")
        await params.result_callback({"removed": len(matched),
                                      "removed_notes": [_note_text(l) for l in matched],
                                      "note": "Tell the user which notes were forgotten."})

    async def _list(params) -> None:
        await params.result_callback({"memories": _load() or ["nothing yet"]})

    llm.register_function("remember", _remember)
    llm.register_function("forget", _forget)
    llm.register_function("list_memories", _list)
