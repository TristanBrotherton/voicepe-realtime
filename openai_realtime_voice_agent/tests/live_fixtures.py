"""Protocol-faithful GPT-Live replay fixtures.

Every shape here was transcribed from real ``/v1/live/sessions`` captures taken
with ``python -m app.live_probe`` on 2026-10-07 (three sessions, 1,048 output
audio deltas, two autonomously-delegated Home Assistant function calls), so a
regression test can replay the wire rather than an assumption about it.

Facts the captures fixed, each of which a test here depends on:

* ``session.output_audio.delta`` carries ``delta`` and **no** ``start_ms`` or
  ``end_ms`` — 0 of 1,048 deltas had them, although the published reference
  documents them. ``timed_output_audio_deltas`` exists to replay the
  documented variant too, so neither reading can break the runtime.
* Deltas are 4,800 bytes (100 ms of mono PCM16 @ 24 kHz), with a short final
  one (1,920 bytes observed). Always an even length.
* The stream is continuous, **silence included**: runs of thousands of
  milliseconds of digital-zero deltas sit between replies, and there is no
  output-audio-done event.
* Quiet-but-audible frames exist at the edges of speech: peaks of 44, 60, 82,
  122, 185, 232 and 456 were measured inside replies, while true idle frames
  peak at 0-3. ``QUIET_SPEECH_PEAK`` is taken from that measurement and is the
  audio the canary's energy gate deleted.
* ``response.event`` envelopes carry the outer ``delegation_id`` and nest the
  Responses event under ``event``; a completed call arrives as
  ``response.output_item.done`` with ``{id, type, status, arguments, call_id,
  name}`` and the backend then emits ``response.completed`` whose
  ``response.output`` is ``[]``.
"""
import array
import base64
import json
import math

DELTA_MS = 100
SAMPLE_RATE = 24000
DELTA_BYTES = SAMPLE_RATE * 2 * DELTA_MS // 1000  # 4800

# Peak of the quietest frames measured *inside* a real reply. A gate with the
# canary's 120.0 RMS threshold deletes these; they are speech.
QUIET_SPEECH_PEAK = 120
# Peak of the dither in real idle frames (0-3 measured).
IDLE_PEAK = 2


def tone(ms: int, freq: float = 220.0, amplitude: int = 6000,
         rate: int = SAMPLE_RATE, start_sample: int = 0) -> bytes:
    """Phase-continuous mono PCM16 sine, so a splice is detectable.

    ``start_sample`` continues an earlier call's phase exactly, which is what
    makes "was a chunk dropped, duplicated or re-ordered?" a byte-level
    question rather than a judgement call.
    """
    count = int(rate * ms / 1000)
    samples = array.array("h", (
        int(round(amplitude * math.sin(2 * math.pi * freq * (start_sample + i) / rate)))
        for i in range(count)
    ))
    return samples.tobytes()


def silence(ms: int, peak: int = 0, rate: int = SAMPLE_RATE) -> bytes:
    """Inaudible audio: exact zeros, or the low dither real idle frames carry."""
    count = int(rate * ms / 1000)
    if peak == 0:
        return b"\x00\x00" * count
    return array.array("h", ((peak if i % 2 else -peak) for i in range(count))).tobytes()


def samples_of(pcm: bytes) -> array.array:
    out = array.array("h")
    out.frombytes(pcm)
    return out


# ---------------------------------------------------------------------------
# One reply, as the wire delivers it
# ---------------------------------------------------------------------------
# Laid out to exercise every decision the assembler makes, with the durations
# that were actually measured:
#
#   1. 300 ms of leading digital silence          -> forwarded (< max_silence)
#   2. 2,000 ms of speech                         -> forwarded untouched
#   3. 600 ms of quiet-but-audible speech tail    -> forwarded (the gate ate it)
#   4. 600 ms inter-sentence pause (idle dither)  -> forwarded (< max_silence)
#   5. 1,500 ms of speech, phase-continuous       -> forwarded untouched
#   6. 3,000 ms of idle silence between replies   -> 800 ms forwarded, rest cut
REPLY_PLAN = (
    ("silence", 300, 0),
    ("speech", 2000, 6000),
    ("speech", 600, QUIET_SPEECH_PEAK),
    ("silence", 600, IDLE_PEAK),
    ("speech", 1500, 6000),
    ("silence", 3000, 0),
)

# The same shape, scaled down. pipecat's websocket output transport paces
# writes to the device at exactly 1.0x real time (see
# FastAPIWebsocketOutputTransport._write_audio_sleep), so an end-to-end test
# costs one second of wall clock per second of reply. The short plan keeps the
# forwarded total at 2,400 ms — an exact multiple of the transport's 80 ms
# chunk, so no partial chunk is left buffered and "every byte arrived" is an
# exact assertion.
SHORT_PLAN = (
    ("silence", 240, 0),
    ("speech", 480, 6000),
    ("speech", 160, QUIET_SPEECH_PEAK),
    ("silence", 400, IDLE_PEAK),
    ("speech", 320, 6000),
    ("silence", 1600, 0),
)


def reply_segments(plan=REPLY_PLAN):
    """The planned reply as ``(kind, pcm)`` pairs, phase-continuous in speech."""
    out = []
    phase = 0
    for kind, ms, level in plan:
        if kind == "speech":
            pcm = tone(ms, amplitude=level, start_sample=phase)
        else:
            pcm = silence(ms, peak=level)
        phase += len(pcm) // 2
        out.append((kind, pcm))
    return out


def reply_pcm(plan=REPLY_PLAN) -> bytes:
    return b"".join(pcm for _, pcm in reply_segments(plan))


def expected_forwarded(plan=REPLY_PLAN, max_silence_ms: int = 800,
                       silence_peak: int = 4) -> bytes:
    """What a correct assembler must hand the device, byte for byte.

    Computed from the plan independently of the implementation: every delta is
    forwarded except those falling in a run of inaudible audio that has already
    lasted longer than ``max_silence_ms``.
    """
    out = bytearray()
    run_ms = 0.0
    for delta in output_audio_deltas(reply_pcm(plan)):
        pcm = _decode(delta["delta"])
        peak = max((abs(s) for s in samples_of(pcm)), default=0)
        duration = 1000.0 * (len(pcm) // 2) / SAMPLE_RATE
        if peak <= silence_peak:
            run_ms += duration
            if run_ms > max_silence_ms:
                continue
        else:
            run_ms = 0.0
        out.extend(pcm)
    return bytes(out)


def speech_spans(plan=REPLY_PLAN):
    """``(offset, pcm)`` of each speech segment inside :func:`reply_pcm`."""
    offset = 0
    out = []
    for kind, pcm in reply_segments(plan):
        if kind == "speech":
            out.append((offset, pcm))
        offset += len(pcm)
    return out


def _decode(text: str) -> bytes:
    return base64.b64decode(text)


def _encode(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def chunk(pcm: bytes, size: int = DELTA_BYTES):
    """Split into wire-sized deltas; the last one is short, as observed."""
    return [pcm[offset:offset + size] for offset in range(0, len(pcm), size)]


def output_audio_deltas(pcm: bytes = None, size: int = DELTA_BYTES):
    """``session.output_audio.delta`` events exactly as the endpoint sends them.

    No timing fields: the real endpoint omits them.
    """
    pcm = reply_pcm() if pcm is None else pcm
    return [
        {"type": "session.output_audio.delta", "delta": _encode(piece),
         "event_id": f"event_audio_{index}"}
        for index, piece in enumerate(chunk(pcm, size))
    ]


def timed_output_audio_deltas(pcm: bytes = None, size: int = DELTA_BYTES):
    """The documented variant, with the ``start_ms``/``end_ms`` server timeline.

    Replayed so the runtime stays correct if the endpoint ever starts sending
    what its reference describes.
    """
    pcm = reply_pcm() if pcm is None else pcm
    events = []
    cursor = 0
    for index, piece in enumerate(chunk(pcm, size)):
        duration = 1000 * (len(piece) // 2) // SAMPLE_RATE
        events.append({
            "type": "session.output_audio.delta", "delta": _encode(piece),
            "start_ms": cursor, "end_ms": cursor + duration,
            "event_id": f"event_audio_{index}",
        })
        cursor += duration
    return events


# ---------------------------------------------------------------------------
# Responses delegation, protocol-faithful synthetic shapes
# ---------------------------------------------------------------------------
DELEGATION_ID = "delegation_fixture_1"
RESPONSE_ID = "response_fixture_1"
TURN_OFF_CALL_ID = "call_fixture_turn_off"
TURN_OFF_ARGUMENTS = '{"name":"kitchen lights","area":"kitchen","domain":["light"]}'
UNLOCK_CALL_ID = "call_fixture_unlock"
UNLOCK_ARGUMENTS = '{"name":"entry lock","domain":["lock"]}'

# ---------------------------------------------------------------------------
# A failed light action with optional-argument placeholders
# ---------------------------------------------------------------------------
# The identifiers and entity names below are synthetic. Both action calls
# preserve the observed protocol shape and completed their whole
# lifecycle — observed, dispatched, submitted, continued, 0 abandoned — and
# Atrium Lamp stayed off, because GPT-Live's delegated backend fills every
# optional parameter with a placeholder and Home Assistant's intent slot
# schema rejects an empty string.
CONTEXT_CALL_ID = "call_fixture_context"
CONTEXT_ARGUMENTS = '{"name":"atrium lamp","domain":"light","area":"kitchen"}'
CONTEXT_RESULT = {
    "success": True,
    "result": ("Live Context: An overview of the areas and the devices in this "
               "smart home:\n- names: Atrium Lamp\n  domain: light\n  "
               "state: 'off'\n  areas: Kitchen\n"),
}

LIGHT_SET_CALL_ID = "call_fixture_light_set"
LIGHT_SET_ARGUMENTS = (
    '{"name":"Atrium Lamp","area":"Kitchen","floor":"","domain":["light"],'
    '"color":"","temperature":0,"brightness":100}'
)
TURN_ON_CALL_ID = "call_fixture_turn_on"
TURN_ON_ARGUMENTS = (
    '{"name":"Atrium Lamp","area":"Kitchen","floor":"","domain":["light"],'
    '"device_class":[]}'
)

# What Home Assistant returns for each invalid-slot shape above.
# pipecat passes these through to the model untouched and logs them as
# "completed successfully", because to pipecat any returned string is a result.
INVALID_SLOTS_LIGHT_SET = "Error calling tool: Received invalid slot info for HassLightSet"
INVALID_SLOTS_TURN_ON = "Error calling tool: Received invalid slot info for HassTurnOn"
# A generic ambiguous-area failure.
NO_MATCH_RESULT = {"success": False,
                   "error": "No exposed entities matched name 'kitchen lights'"}

# Argument shapes Home Assistant's intent validation rejects. Each entry is
# (tool, parameter, value, Home Assistant's message). A sanitizer that leaves
# any of these in place reproduces the canary exactly.
HA_REJECTED_SLOTS = (
    ("HassTurnOn", "floor", "", "string value is empty at 'floor.value'"),
    ("HassTurnOn", "area", "   ", "string value is empty at 'area.value'"),
    ("HassTurnOn", "name", "", "string value is empty at 'name.value'"),
    ("HassTurnOn", "device_class", "", "value must be one of [...]"),
    ("HassLightSet", "floor", "", "string value is empty at 'floor.value'"),
    ("HassLightSet", "color", "", "not a valid value: Unknown color at 'color.value'"),
)

# Representative MCP tool definitions for the two tools involved, in the OpenAI
# function format main.py builds from mcp_tools_schema.standard_tools. Note
# what is NOT here: nothing is required, and HassLightSet.temperature is the
# only numeric declared with a minimum and no maximum, because it is colour
# temperature in Kelvin.
LIGHT_SET_TOOL = {
    "type": "function",
    "name": "light__HassLightSet",
    "description": "Sets the brightness percentage or color of a light",
    "parameters": {
        "type": "object",
        "properties": {
            "area": {"type": "string"},
            "brightness": {
                "description": ("The brightness percentage of the light between 0 "
                                "and 100, where 0 is off and 100 is fully lit"),
                "maximum": 100, "minimum": 0, "type": "integer",
            },
            "color": {"type": "string"},
            "domain": {"items": {"enum": ["light"], "type": "string"}, "type": "array"},
            "floor": {"type": "string"},
            "name": {"type": "string"},
            "temperature": {"minimum": 0, "type": "integer"},
        },
        "required": [],
    },
}
TURN_ON_TOOL = {
    "type": "function",
    "name": "intent__HassTurnOn",
    "description": ("Turns on/opens/presses a device or entity. For locks, this "
                    "performs a 'lock' action."),
    "parameters": {
        "type": "object",
        "properties": {
            "area": {"type": "string"},
            "device_class": {"items": {"enum": ["tv", "speaker", "outlet", "switch",
                                                "garage", "gate", "door", "window"],
                                       "type": "string"}, "type": "array"},
            "domain": {"items": {"type": "string"}, "type": "array"},
            "floor": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": [],
    },
}


def delegation_created(delegation_id: str = DELEGATION_ID, offset_ms: int = 23000,
                       target: str = "responses", response_id: str = RESPONSE_ID):
    """``session.delegation.created``, as GPT-Live sends it unprompted.

    The real captures carried no ``client_event_id`` on these: GPT-Live created
    the delegated work on its own initiative from spoken input.
    """
    return {
        "type": "session.delegation.created", "offset_ms": offset_ms,
        "event_id": "event_EWMkDrSSL341udB48QU0r",
        "delegation": {"id": delegation_id, "type": "delegation",
                       "response_id": response_id, "target": target},
    }


def envelope(inner: dict, delegation_id: str = DELEGATION_ID, event_id: str = "event_r1"):
    return {"type": "response.event", "event_id": event_id,
            "delegation_id": delegation_id, "event": inner}


def response_created(delegation_id: str = DELEGATION_ID):
    return envelope({
        "type": "response.created", "sequence_number": 0,
        "response": {"id": RESPONSE_ID, "object": "response", "status": "in_progress",
                     "model": "gpt-6-luna", "output": [], "parallel_tool_calls": False,
                     "tool_choice": "auto", "tools": [], "usage": None},
    }, delegation_id)


def function_call_added(call_id: str, name: str, delegation_id: str = DELEGATION_ID,
                        item_id: str = "fc_09f37c83836cf8d9006ac6546a0cb887d08a6b68aadc0c5769"):
    """``response.output_item.added``: name and call_id, arguments still empty.

    Deliberately part of the replay: acting on this event would dispatch a
    call with no arguments.
    """
    return envelope({
        "type": "response.output_item.added", "output_index": 0, "sequence_number": 2,
        "item": {"id": item_id, "type": "function_call", "status": "in_progress",
                 "arguments": "", "call_id": call_id, "name": name},
    }, delegation_id)


def function_call_arguments_delta(fragment: str, delegation_id: str = DELEGATION_ID,
                                  item_id: str = "fc_09f37c83836cf8d9006ac6546a0cb887d08a6b68aadc0c5769"):
    return envelope({
        "type": "response.function_call_arguments.delta", "delta": fragment,
        "item_id": item_id, "output_index": 0, "sequence_number": 3,
        "obfuscation": "sNUKVfj6YqjBBD",
    }, delegation_id)


def function_call_arguments_done(arguments: str, delegation_id: str = DELEGATION_ID,
                                 item_id: str = "fc_09f37c83836cf8d9006ac6546a0cb887d08a6b68aadc0c5769"):
    """Carries the arguments but neither ``name`` nor ``call_id``.

    Replayed so a regression that starts dispatching on this event — and so
    loses the call identity — fails loudly.
    """
    return envelope({
        "type": "response.function_call_arguments.done", "arguments": arguments,
        "item_id": item_id, "output_index": 0, "sequence_number": 27,
    }, delegation_id)


def function_call_done(call_id: str, name: str, arguments: str,
                       delegation_id: str = DELEGATION_ID,
                       item_id: str = "fc_09f37c83836cf8d9006ac6546a0cb887d08a6b68aadc0c5769"):
    return envelope({
        "type": "response.output_item.done", "output_index": 0, "sequence_number": 28,
        "item": {"id": item_id, "type": "function_call", "status": "completed",
                 "arguments": arguments, "call_id": call_id, "name": name},
    }, delegation_id)


def response_completed(delegation_id: str = DELEGATION_ID, usage: dict = None):
    """``response.completed``. Note ``output`` is ``[]`` even after a call."""
    return envelope({
        "type": "response.completed", "sequence_number": 29,
        "response": {
            "id": RESPONSE_ID, "object": "response", "status": "completed",
            "model": "gpt-6-luna", "output": [], "error": None,
            "incomplete_details": None, "tools": [], "instructions": None,
            "usage": usage or {
                "input_tokens": 851,
                "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                "output_tokens": 38,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 889,
            },
        },
    }, delegation_id)


def function_call_sequence(call_id: str, name: str, arguments: str,
                           delegation_id: str = DELEGATION_ID):
    """The full event run for one delegated call, in captured order."""
    events = [delegation_created(delegation_id), response_created(delegation_id),
              function_call_added(call_id, name, delegation_id)]
    for index in range(0, len(arguments), 8):
        events.append(function_call_arguments_delta(arguments[index:index + 8], delegation_id))
    events.append(function_call_arguments_done(arguments, delegation_id))
    events.append(function_call_done(call_id, name, arguments, delegation_id))
    events.append(response_completed(delegation_id))
    return events


def transcript_in(text: str, start_ms: int = 21000):
    """``session.input_transcript.delta`` events, 200 ms apart as observed."""
    events = []
    for index, word in enumerate(text.split()):
        events.append({
            "type": "session.input_transcript.delta",
            "delta": (" " if index else "") + word,
            "start_ms": start_ms + 200 * index, "end_ms": start_ms + 200 * (index + 1),
            "event_id": f"event_in_{index}",
        })
    return events


SESSION_STARTED = {
    "type": "session.started",
    "event_id": "event_started",
    "session": {
        "id": "live_u7_EWMkBp3oMfCljTSR8IPte1PCDWRPopVk",
        "expires_at": 1791389832,
        "model": "gpt-live-1",
        "instructions": "Be concise.",
        "audio": {"output": {"voice": "marin"},
                  "format": {"type": "audio/pcm", "rate": 24000}},
        "delegation": {"type": "responses", "responses": {"model": "gpt-6-luna"}},
        "status": "active",
        "input": [],
    },
}


def session_started(session_id: str = "sess_1", audio: dict = None):
    event = json.loads(json.dumps(SESSION_STARTED))
    event["session"]["id"] = session_id
    if audio is not None:
        event["session"]["audio"] = audio
    return event
