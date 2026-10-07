"""GPT-Live wire protocol helpers (pure, no network).

Everything here mirrors the official OpenAI GPT-Live documentation
(developers.openai.com → Live: WebSockets, Delegation and tools, Managing
sessions, Migrate to GPT-Live). Kept free of asyncio and pipecat so the
protocol can be unit-tested byte-for-byte:

* client events: ``session.start``, ``session.input_audio.append``,
  ``response.item.create`` + ``response.create`` (function results),
  ``session.*.append`` context, ``session.input_audio.mute/unmute``,
  ``session.close``;
* server events: parsing, the ``response.event`` envelope, and the
  function-call collection rules for Responses delegation;
* conversion of cached pipecat context messages into the startup ``input``
  history; 500-token context-append chunking.

Output audio lives in ``app/live_audio.py``; the session configuration object
is strict and rejects unknown fields, so what goes into ``session.start``
belongs here and what comes back out of ``session.output_audio.delta``
belongs there.
"""
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
# "WebSocket input and output use raw, headerless, mono, signed 16-bit
# little-endian PCM sampled at 24,000 Hz" — and one format applies to both
# directions and cannot change during the session.
LIVE_SAMPLE_RATE = 24000
# session.input accepts at most 128 messages / 8,192 tokens.
MAX_INPUT_ITEMS = 128
MAX_INPUT_CHARS = 24000
# Each context append takes at most 500 tokens; we chunk against a lower
# budget because the count is an estimate.
MAX_APPEND_TOKENS = 450

CLIENT_EVENT_TYPES = frozenset({
    "session.start", "session.update", "session.input_audio.append",
    "session.input_audio.mute", "session.input_audio.unmute",
    "session.instructions.append", "session.thinking.append", "session.commentary.append",
    "response.item.create", "response.create", "session.close",
})


def new_event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# client events
# ---------------------------------------------------------------------------

def build_session_start(
    *,
    model: str,
    instructions: str,
    backend_model: str,
    backend_instructions: str,
    tools: List[dict],
    voice: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    service_tier: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    history: Optional[List[dict]] = None,
    sample_rate: int = LIVE_SAMPLE_RATE,
    include_audio_format: bool = True,
    event_id: Optional[str] = None,
) -> dict:
    """The first message on the socket.

    Tools live under ``delegation.responses.tools`` in the Responses function
    schema; ``parallel_tool_calls`` is False as the migration guide advises
    for a first migration (one call at a time, which also matches how the
    action gate and slow-tool acknowledgement reason about a turn).

    ``include_audio_format`` exists because the session configuration object
    is documented as strict (unknown fields are rejected) and the two published
    references disagree about whether ``audio.format`` is a field at all: the
    OpenAI WebSocket guide documents it with ``audio/pcm`` at 24 kHz as the
    default, while the Foundry event reference lists only ``audio.output.voice``
    and states the WebSocket format is fixed at 24 kHz PCM16 mono. Sending it
    is therefore a no-op when accepted and the probe (``app/live_probe.py``)
    can prove which reading the endpoint takes, with ``false`` as the fallback
    that cannot be rejected.
    """
    responses: Dict[str, Any] = {
        "model": backend_model,
        "instructions": backend_instructions,
        "tools": [responses_function_tool(t) for t in tools],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    if reasoning_effort:
        responses["reasoning"] = {"effort": reasoning_effort}
    if service_tier:
        responses["service_tier"] = service_tier
    if max_output_tokens:
        responses["max_output_tokens"] = max(16, int(max_output_tokens))
    audio: Dict[str, Any] = {}
    if include_audio_format:
        audio["format"] = {"type": "audio/pcm", "rate": sample_rate}
    if voice:
        audio["output"] = {"voice": voice}
    session: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "delegation": {"type": "responses", "responses": responses},
    }
    if audio:
        session["audio"] = audio
    if history:
        session["input"] = history
    return {"type": "session.start", "event_id": event_id or new_event_id("start"), "session": session}


def responses_function_tool(tool: dict) -> dict:
    """Our Realtime-shaped function tool as a Responses function tool.

    Both use the flat ``{"type":"function","name","description","parameters"}``
    shape; this keeps exactly those keys so stray Realtime-only fields never
    reach the backend.
    """
    out = {
        "type": "function",
        "name": tool["name"],
        "description": tool.get("description") or "",
        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
    }
    if "strict" in tool:
        out["strict"] = tool["strict"]
    return out


def input_audio_append(audio_b64: str) -> dict:
    return {"type": "session.input_audio.append", "audio": audio_b64}


def function_call_output(call_id: str, output: Any, event_id: Optional[str] = None) -> dict:
    """``response.item.create`` carrying one function result (a string)."""
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    return {
        "type": "response.item.create",
        "event_id": event_id or new_event_id("result"),
        "item": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def response_create(event_id: Optional[str] = None) -> dict:
    return {"type": "response.create", "event_id": event_id or new_event_id("continue")}


def context_append(kind: str, content: str, delegation_id: Optional[str] = None,
                   event_id: Optional[str] = None) -> dict:
    """``session.{instructions,thinking,commentary}.append``.

    ``delegation_id`` is a required field and ``null`` is meaningful
    (session-wide context), so it is always present.
    """
    assert kind in ("instructions", "thinking", "commentary"), kind
    return {
        "type": f"session.{kind}.append",
        "event_id": event_id or new_event_id(kind),
        "delegation_id": delegation_id,
        "content": content,
    }


def input_audio_mute(mute: bool, event_id: Optional[str] = None) -> dict:
    return {
        "type": "session.input_audio.mute" if mute else "session.input_audio.unmute",
        "event_id": event_id or new_event_id("mute"),
    }


def session_close(event_id: Optional[str] = None) -> dict:
    return {"type": "session.close", "event_id": event_id or new_event_id("close")}


# ---------------------------------------------------------------------------
# server events
# ---------------------------------------------------------------------------

def parse_server_event(message: Any) -> Optional[dict]:
    """A server event as a dict, or None for anything unparseable."""
    try:
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", "replace")
        evt = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(evt, dict) or not isinstance(evt.get("type"), str):
        return None
    return evt


def inner_event(evt: dict) -> dict:
    """The nested Responses event of a ``response.event`` envelope."""
    inner = evt.get("event")
    return inner if isinstance(inner, dict) else {}


def inner_type(evt: dict) -> str:
    return str(inner_event(evt).get("type") or "")


@dataclass
class FunctionCall:
    call_id: str
    name: str
    arguments: dict
    delegation_id: Optional[str]
    response_id: Optional[str]


@dataclass
class _PendingResponse:
    call_ids: set = field(default_factory=set)
    had_calls: bool = False
    finished: bool = False


_UNCORRELATED = "uncorrelated"


class DelegationLedger:
    """Function-call collection for Responses delegation.

    From the delegation guide: collect completed ``function_call`` items from
    nested ``response.output_item.done`` events (the arguments-done event
    alone lacks name and call_id; lifecycle snapshots carry ``output: []``),
    submit one ``response.item.create`` per call, and send ``response.create``
    only once every required result is in and the backend response has
    finished emitting items.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, _PendingResponse] = {}
        self._open: Dict[str, str] = {}  # call_id -> correlation key
        self._active: Dict[str, str] = {}  # delegation id -> current response key
        self.last_delegation_id: Optional[str] = None

    def _key(self, evt: dict) -> str:
        delegation = str(evt.get("delegation_id") or _UNCORRELATED)
        inner = inner_event(evt)
        response_id = inner.get("response_id") or (inner.get("response") or {}).get("id")
        if response_id:
            return f"{delegation}:{response_id}"
        return self._active.get(delegation, delegation)

    @property
    def open_calls(self) -> int:
        return len(self._open)

    def has_open(self, call_id: str) -> bool:
        return call_id in self._open

    @property
    def busy(self) -> bool:
        """A delegated response is running or waiting on our results."""
        return bool(self._open) or any(not p.finished for p in self._pending.values())

    def observe(self, evt: dict) -> Tuple[Optional[FunctionCall], bool]:
        """Feed one ``response.event`` envelope.

        Returns ``(function_call, continue_now)``: a call to execute, if the
        event completed one, and whether a ``response.create`` is due right
        now (a finished response whose calls are all answered).
        """
        if evt.get("type") != "response.event":
            return None, False
        key = self._key(evt)
        inner = inner_event(evt)
        kind = str(inner.get("type") or "")
        if kind == "response.created":
            delegation = str(evt.get("delegation_id") or _UNCORRELATED)
            self._active[delegation] = key
            self._pending.setdefault(key, _PendingResponse())
            return None, False
        if kind == "response.output_item.done":
            item = inner.get("item") or {}
            if item.get("type") != "function_call" or item.get("status", "completed") != "completed":
                return None, False
            call_id, name = item.get("call_id"), item.get("name")
            if not call_id or not name or call_id in self._open:
                return None, False
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except ValueError:
                arguments = {"_invalid_arguments": item.get("arguments")}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            pending = self._pending.setdefault(key, _PendingResponse())
            pending.call_ids.add(call_id)
            pending.had_calls = True
            self._open[call_id] = key
            response_id = inner.get("response_id") or (inner.get("response") or {}).get("id")
            return FunctionCall(call_id, name, arguments, evt.get("delegation_id"), response_id), False
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            pending = self._pending.get(key)
            if pending is None:
                return None, False
            pending.finished = True
            delegation = str(evt.get("delegation_id") or _UNCORRELATED)
            if self._active.get(delegation) == key:
                self._active.pop(delegation, None)
            return None, self._ready(key)
        return None, False

    def complete(self, call_id: str) -> bool:
        """A result was submitted for call_id. True when response.create is due."""
        key = self._open.pop(call_id, None)
        if key is None:
            return False
        pending = self._pending.get(key)
        if pending is not None:
            pending.call_ids.discard(call_id)
        return self._ready(key)

    def _ready(self, key: str) -> bool:
        pending = self._pending.get(key)
        if pending is None or not pending.finished or not pending.had_calls or pending.call_ids:
            return False
        del self._pending[key]
        return True

    def reset(self) -> None:
        self._pending.clear()
        self._open.clear()
        self._active.clear()


def backend_usage(evt: dict) -> Optional[dict]:
    """Token usage from a nested ``response.completed`` event, if any."""
    if inner_type(evt) != "response.completed":
        return None
    usage = (inner_event(evt).get("response") or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    return {
        "in_text": int(usage.get("input_tokens") or 0),
        "cached": int(details.get("cached_tokens") or 0),
        "out_text": int(usage.get("output_tokens") or 0),
        "reasoning": int(out_details.get("reasoning_tokens") or 0),
    }


# ---------------------------------------------------------------------------
# history seeding
# ---------------------------------------------------------------------------

def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and part.get("type") in (None, "text", "input_text", "output_text"):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(p.strip() for p in parts if p.strip()).strip()
    return ""


def history_to_session_input(messages: Iterable[dict], max_items: int = MAX_INPUT_ITEMS,
                             max_chars: int = MAX_INPUT_CHARS) -> List[dict]:
    """Cached pipecat context messages → ``session.input`` items.

    Only text user/assistant/developer messages are representable; tool calls,
    tool results and the leading system prompt (sent as ``instructions``) are
    skipped. The newest messages win when the caps bite.
    """
    items: List[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            # The current trusted prompt is already supplied through
            # session.instructions. Replaying a cached system prompt as a
            # developer message would duplicate it and could resurrect stale
            # runtime or tool policy after a configuration change.
            continue
        if role not in ("user", "assistant", "developer"):
            continue
        if message.get("tool_calls") or message.get("tool_call_id"):
            continue
        text = _message_text(message.get("content"))
        if not text or text == "IN_PROGRESS":
            continue
        part_type = "output_text" if role == "assistant" else "input_text"
        items.append({"type": "message", "role": role, "content": [{"type": part_type, "text": text}]})
    items = items[-max_items:]
    while items and sum(len(i["content"][0]["text"]) for i in items) > max_chars:
        items.pop(0)
    return items


# ---------------------------------------------------------------------------
# context appends
# ---------------------------------------------------------------------------

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def estimated_tokens(text: str) -> int:
    """~4 ASCII chars per token; non-ASCII counted as a token each."""
    quarters = sum(1 if ch.isascii() else 4 for ch in text)
    return (quarters + 3) // 4


def chunk_text(text: str, token_limit: int = MAX_APPEND_TOKENS) -> List[str]:
    """Split text into appends of at most ``token_limit`` estimated tokens."""
    text = (text or "").strip()
    if not text:
        return []
    if estimated_tokens(text) <= token_limit:
        return [text]
    chunks: List[str] = []
    current = ""
    for piece in _SENTENCE.split(text):
        piece = piece.strip()
        while estimated_tokens(piece) > token_limit:
            cut = token_limit * 4
            space = piece.rfind(" ", 0, cut)
            head, piece = (piece[:space], piece[space:]) if space > 0 else (piece[:cut], piece[cut:])
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head.strip())
            piece = piece.strip()
        if not piece:
            continue
        if current and estimated_tokens(f"{current} {piece}") > token_limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks
