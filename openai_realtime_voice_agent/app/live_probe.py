"""Device-free GPT-Live protocol probe: run it instead of guessing.

The GPT-Live canary failed in a household with unintelligible output audio and
delegated actions that never executed, and the container was restarted before
complete logs survived. Neither failure can be diagnosed from a Voice PE: the
device, the firmware, the relay pacing and the model are all in the loop at
once. This module talks to the real ``/v1/live/sessions`` endpoint with **no
device, no Home Assistant and no household audio** and reports exactly what the
wire does:

* the audio configuration the server echoes in ``session.started`` (so an
  assumed format is never mistaken for a negotiated one);
* every ``session.output_audio.delta``: arrival time, ``start_ms``/``end_ms``,
  byte length, alignment, RMS/peak, and whether the server timeline is
  contiguous — the documentation says a gap between ranges is *omitted*
  silence, which decides whether an energy gate is appropriate at all;
* the delivery pace (audio seconds produced per wall-clock second), which
  decides whether the device can ever build a playout lead;
* the nested Responses delegation events for a real function call, captured
  verbatim so regression fixtures are transcribed from the wire rather than
  invented.

It drives the backend directly with ``response.item.create`` (a user message)
plus ``response.create``, which the documentation lists as the way to run a
delegated response. No Home Assistant tool is registered: the probe tool is a
pure echo, so a probe run can never change household state.

Usage (needs OPENAI_API_KEY; costs a few seconds of Live voice time)::

    python -m app.live_probe --seconds 12 --wav /tmp/live_probe.wav

``--wav`` is optional and must point outside the repository: this repository is
a public surface and tracked audio is rejected by the test suite.
"""
import argparse
import asyncio
import base64
import json
import os
import statistics
import struct
import sys
import time
from typing import Any, Dict, List, Optional

from websockets.asyncio.client import connect as websocket_connect

from app.live_protocol import (
    LIVE_SAMPLE_RATE,
    LIVE_URL,
    build_session_start,
    function_call_output,
    input_audio_append,
    parse_server_event,
    response_create,
    session_close,
)
from app.live_audio import AudioDelta, pcm16_stats

PROBE_TOOL = {
    "name": "probe_echo",
    "description": (
        "Diagnostic echo used only by the protocol probe. Returns its argument "
        "unchanged. It controls nothing and has no side effects."
    ),
    "parameters": {
        "type": "object",
        "properties": {"note": {"type": "string", "description": "Any short string."}},
        "required": ["note"],
    },
}

# Shaped like the Home Assistant MCP tools the add-on really exposes, so the
# backend's willingness to call them is tested faithfully. The probe NEVER
# connects to Home Assistant: these handlers are echoes inside this process and
# cannot change any household state.
HA_SHAPED_TOOLS = [
    {
        "name": "HassTurnOff",
        "description": "Turns off/closes a device or entity.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name of the device."},
                "area": {"type": "string", "description": "The area."},
                "domain": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
        },
    },
    {
        "name": "GetLiveContext",
        "description": (
            "Use this tool when the user asks a question about the CURRENT state of "
            "a device or entity."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

# Speech synthesis for the input side, so a spoken household request can be
# replayed without a Voice PE, a microphone or anybody's voice.
TTS_URL = "https://api.openai.com/v1/audio/speech"
TTS_MODEL = "gpt-4o-mini-tts"

SPEAK_PROMPT = (
    "Say exactly this and nothing else: "
    "The quick brown fox jumps over the lazy dog. She sells seashells by the sea shore."
)
TOOL_PROMPT = (
    "Call the probe_echo tool once with note set to \"canary\", then say the word "
    "it returned."
)


def _wav(path: str, pcm: bytes, rate: int) -> None:
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    with open(path, "wb") as handle:
        handle.write(header + pcm)


class LiveProbe:
    """One probe session. Collects evidence; draws no conclusions."""

    def __init__(self, *, api_key: str, model: str, backend_model: str, voice: Optional[str],
                 send_format: bool, seconds: float, url: str = LIVE_URL,
                 spoken: Optional[List[str]] = None, drive_backend: bool = True,
                 ha_tools: bool = True, tts_voice: str = "alloy") -> None:
        self.api_key = api_key
        self.model = model
        self.backend_model = backend_model
        self.voice = voice
        self.send_format = send_format
        self.seconds = seconds
        self.url = url
        self.spoken = list(spoken or [])
        self.drive_backend = drive_backend
        self.ha_tools = ha_tools
        self.tts_voice = tts_voice

        self.events: List[dict] = []          # every server event, audio truncated
        self.deltas: List[AudioDelta] = []
        self.audio = bytearray()
        self.response_events: List[dict] = []  # verbatim response.event envelopes
        self.errors: List[dict] = []
        self.session: Dict[str, Any] = {}
        self.transcript_in: List[dict] = []
        self.transcript_out: List[str] = []
        self.delegations: List[dict] = []
        self.tool_calls: List[dict] = []
        self.answered: List[str] = []
        self.continued = False
        self.phases: List[dict] = []
        self._t0 = 0.0
        self._first_audio_mono: Optional[float] = None
        self._socket = None
        self._closed = asyncio.Event()
        self._mic_busy = False

    # -- wire ----------------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        await self._socket.send(json.dumps(payload))

    def _start_event(self) -> dict:
        tools = [PROBE_TOOL] + (HA_SHAPED_TOOLS if self.ha_tools else [])
        return build_session_start(
            model=self.model,
            instructions=(
                "You are a smart-home voice assistant in a live spoken conversation. "
                "Carry out the user's requests with the backend. Keep every reply "
                "to one short sentence.\n\nDelegation policy:\nBackend tools:\n"
                "- Smart home: read and control Home Assistant devices.\n"
                "- Diagnostic echo.\n\nDelegate to the backend when the request needs "
                "any backend tool, a lookup, or careful reasoning. Delegate before "
                "giving an answer that depends on backend work. Report an action as "
                "done only after the backend confirms it."
            ),
            backend_model=self.backend_model,
            backend_instructions=(
                "You are the backend of a smart-home voice assistant. Carry out "
                "requests with the available tools immediately. Never claim an action "
                "succeeded unless its tool result confirms it. Reply in one short "
                "sentence."
            ),
            tools=tools,
            voice=self.voice,
            reasoning_effort="low",
            sample_rate=LIVE_SAMPLE_RATE,
            include_audio_format=self.send_format,
        )

    async def _silence_filler(self) -> None:
        """A continuous 24 kHz mic stream, as the documentation asks for."""
        chunk = input_audio_append(
            base64.b64encode(b"\x00\x00" * (LIVE_SAMPLE_RATE // 10)).decode("ascii")
        )
        try:
            while True:
                await asyncio.sleep(0.1)
                if not self._mic_busy:
                    await self._send(chunk)
        except (asyncio.CancelledError, Exception):
            return

    # -- synthesised spoken input -------------------------------------------

    async def _tts_pcm(self, text: str) -> bytes:
        """24 kHz mono PCM16 for ``text`` from the OpenAI speech endpoint.

        Used so a spoken household request can be replayed with no microphone
        and nobody's recorded voice.
        """
        import urllib.request

        body = json.dumps({
            "model": TTS_MODEL, "voice": self.tts_voice, "input": text,
            "response_format": "pcm",
        }).encode("utf-8")
        request = urllib.request.Request(
            TTS_URL, data=body,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )

        def _fetch() -> bytes:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()

        return await asyncio.to_thread(_fetch)

    async def _say(self, text: str) -> int:
        """Stream synthesised speech into the session at real-time pace."""
        pcm = await self._tts_pcm(text)
        if len(pcm) % 2:
            pcm = pcm[:-1]
        chunk = (LIVE_SAMPLE_RATE // 10) * 2  # 100 ms
        self._mic_busy = True
        try:
            for offset in range(0, len(pcm), chunk):
                await self._send(
                    input_audio_append(
                        base64.b64encode(pcm[offset:offset + chunk]).decode("ascii")
                    )
                )
                await asyncio.sleep(0.1)
        finally:
            self._mic_busy = False
        return len(pcm)

    # -- script --------------------------------------------------------------

    async def run(self) -> dict:
        async with websocket_connect(
            uri=self.url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            max_size=16 * 1024 * 1024,
        ) as socket:
            self._socket = socket
            self._t0 = time.monotonic()
            await self._send(self._start_event())
            reader = asyncio.create_task(self._read())
            try:
                await asyncio.wait_for(self._started(), 20)
            except asyncio.TimeoutError:
                reader.cancel()
                return self.report(note="session.started never arrived")
            filler = asyncio.create_task(self._silence_filler())
            try:
                # 1. Spoken requests, exactly as a household turn arrives: does
                #    GPT-Live create a delegation on its OWN initiative?
                for phrase in self.spoken:
                    await self._phase(f"spoken: {phrase}", self._spoken_turn, phrase)
                # 2. Speech only: pace, format, timeline contiguity.
                if self.drive_backend:
                    await self._phase("driven: speech", self._driven_speech)
                    # 3. A delegated function call driven from the client.
                    await self._phase("driven: tool", self._driven_tool)
            finally:
                filler.cancel()
                try:
                    await self._send(session_close())
                    await asyncio.wait_for(self._closed.wait(), 5)
                except Exception:
                    pass
                reader.cancel()
        return self.report()

    async def _started(self) -> None:
        while not self.session:
            if self.errors:
                raise asyncio.TimeoutError
            await asyncio.sleep(0.02)

    async def _ask(self, text: str) -> None:
        """Drive the delegated backend with a user message, as documented."""
        await self._send({
            "type": "response.item.create",
            "event_id": f"probe_msg_{int(time.time() * 1000)}",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
        })
        await self._send(response_create())

    async def _phase(self, label: str, body, *args) -> None:
        """Run one scripted phase and record what the session did during it."""
        mark = {
            "label": label,
            "at_s": round(time.monotonic() - self._t0, 2),
            "deltas_before": len(self.deltas),
            "delegations_before": len(self.delegations),
            "tool_calls_before": len(self.tool_calls),
        }
        await body(*args)
        mark.update({
            "deltas": len(self.deltas) - mark.pop("deltas_before"),
            "delegations": len(self.delegations) - mark.pop("delegations_before"),
            "tool_calls": len(self.tool_calls) - mark.pop("tool_calls_before"),
            "until_s": round(time.monotonic() - self._t0, 2),
        })
        self.phases.append(mark)

    async def _spoken_turn(self, phrase: str) -> None:
        await self._say(phrase)
        await asyncio.sleep(self.seconds)

    async def _driven_speech(self) -> None:
        await self._ask(SPEAK_PROMPT)
        await asyncio.sleep(self.seconds)

    async def _driven_tool(self) -> None:
        await self._ask(TOOL_PROMPT)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not self.continued:
            await asyncio.sleep(0.05)
        await asyncio.sleep(4)

    # -- receive -------------------------------------------------------------

    async def _read(self) -> None:
        async for message in self._socket:
            evt = parse_server_event(message)
            if evt is None:
                self.events.append({"type": "<unparseable>"})
                continue
            await self._handle(evt)

    async def _handle(self, evt: dict) -> None:
        kind = evt["type"]
        if kind == "session.output_audio.delta":
            self._on_audio(evt)
        else:
            self.events.append(_truncate(evt))
        if kind == "session.started":
            self.session = evt.get("session") or {}
        elif kind == "session.output_transcript.delta":
            if isinstance(evt.get("delta"), str):
                self.transcript_out.append(evt["delta"])
        elif kind == "session.input_transcript.delta":
            self.transcript_in.append(
                {"delta": evt.get("delta"), "start_ms": evt.get("start_ms")}
            )
        elif kind == "session.delegation.created":
            delegation = dict(evt.get("delegation") or {})
            # A delegation with no client_event_id was created by GPT-Live on
            # its own initiative. That is the whole question for the tool path.
            delegation["client_event_id"] = evt.get("client_event_id")
            delegation["autonomous"] = not evt.get("client_event_id")
            delegation["offset_ms"] = evt.get("offset_ms")
            self.delegations.append(delegation)
        elif kind == "error":
            self.errors.append(evt.get("error") or {})
        elif kind == "session.closed":
            self._closed.set()
        elif kind == "response.event":
            await self._on_response_event(evt)

    def _on_audio(self, evt: dict) -> None:
        now = time.monotonic()
        try:
            pcm = base64.b64decode(evt.get("delta") or "")
        except (TypeError, ValueError):
            self.events.append({"type": "session.output_audio.delta", "error": "undecodable"})
            return
        if self._first_audio_mono is None:
            self._first_audio_mono = now
        stats = pcm16_stats(pcm)
        self.deltas.append(AudioDelta(
            arrival_s=now - self._t0,
            start_ms=evt.get("start_ms"),
            end_ms=evt.get("end_ms"),
            nbytes=len(pcm),
            rms=stats.rms,
            peak=stats.peak,
            all_zero=stats.all_zero,
        ))
        self.audio.extend(pcm)

    async def _on_response_event(self, evt: dict) -> None:
        self.response_events.append(_truncate(evt))
        inner = evt.get("event") or {}
        if inner.get("type") != "response.output_item.done":
            return
        item = inner.get("item") or {}
        if item.get("type") != "function_call":
            return
        self.tool_calls.append({
            "name": item.get("name"), "call_id": item.get("call_id"),
            "arguments": item.get("arguments"), "status": item.get("status"),
            "delegation_id": evt.get("delegation_id"),
            "at_s": round(time.monotonic() - self._t0, 2),
        })
        # Answer it exactly as the runtime does, then continue the response.
        # The probe's handlers are echoes: nothing outside this process is
        # touched, whatever the tool is called.
        await self._send(function_call_output(
            item.get("call_id") or "",
            {"status": "probe_echo", "name": item.get("name"),
             "arguments": item.get("arguments")},
        ))
        await self._send(response_create())
        self.answered.append(str(item.get("call_id") or ""))
        self.continued = True

    # -- report --------------------------------------------------------------

    def report(self, note: str = "") -> dict:
        gaps, overlaps, timed = [], 0, 0
        previous_end = None
        for delta in self.deltas:
            if delta.start_ms is None or delta.end_ms is None:
                continue
            timed += 1
            if previous_end is not None:
                difference = delta.start_ms - previous_end
                if difference > 0:
                    gaps.append(difference)
                elif difference < 0:
                    overlaps += 1
            previous_end = delta.end_ms
        audio_seconds = len(self.audio) / (LIVE_SAMPLE_RATE * 2)
        span = 0.0
        if self.deltas and self._first_audio_mono is not None:
            span = self.deltas[-1].arrival_s - (self._first_audio_mono - self._t0)
        interarrival = sorted(
            round(b.arrival_s - a.arrival_s, 4)
            for a, b in zip(self.deltas, self.deltas[1:])
        )

        def pct(fraction: float):
            if not interarrival:
                return None
            index = min(len(interarrival) - 1, int(fraction * len(interarrival)))
            return interarrival[index]

        # Clock drift: audio seconds delivered minus wall-clock seconds elapsed.
        # Negative means the stream falls behind real time, which drains any
        # fixed playout lead the relay hands the device.
        drift_s = round(audio_seconds - span, 3) if span > 0 else None
        return {
            "note": note,
            "request": {
                "model": self.model, "backend_model": self.backend_model,
                "voice": self.voice, "audio_format_sent": self.send_format,
            },
            "session_started": self.session,
            "negotiated_audio": (self.session or {}).get("audio"),
            "errors": self.errors,
            "event_type_counts": _counts([e.get("type") for e in self.events]),
            "audio": {
                "deltas": len(self.deltas),
                "bytes": len(self.audio),
                "seconds": round(audio_seconds, 3),
                "odd_length_deltas": sum(1 for d in self.deltas if d.nbytes % 2),
                "all_zero_deltas": sum(1 for d in self.deltas if d.all_zero),
                "delta_bytes_min": min((d.nbytes for d in self.deltas), default=0),
                "delta_bytes_max": max((d.nbytes for d in self.deltas), default=0),
                "delta_bytes_median": int(statistics.median([d.nbytes for d in self.deltas])) if self.deltas else 0,
                "peak": max((d.peak for d in self.deltas), default=0),
                "timed_deltas": timed,
                "timeline_gaps": len(gaps),
                "timeline_gap_ms_total": sum(gaps),
                "timeline_gap_ms_max": max(gaps, default=0),
                "timeline_overlaps": overlaps,
                "wall_clock_span_s": round(span, 3),
                "realtime_ratio": round(audio_seconds / span, 3) if span > 0.2 else None,
                "drift_s": drift_s,
                "interarrival_s_p50": pct(0.50),
                "interarrival_s_p95": pct(0.95),
                "interarrival_s_p99": pct(0.99),
                "interarrival_s_max": interarrival[-1] if interarrival else None,
            },
            "phases": self.phases,
            "transcript_in": self.transcript_in,
            "transcript_out_chars": len("".join(self.transcript_out)),
            "delegation": {
                "created": self.delegations,
                "autonomous_count": sum(1 for d in self.delegations if d.get("autonomous")),
                "client_driven_count": sum(1 for d in self.delegations if not d.get("autonomous")),
                "response_event_count": len(self.response_events),
                "inner_types": _counts([(e.get("event") or {}).get("type") for e in self.response_events]),
                "function_calls": self.tool_calls,
                "answered_call_ids": self.answered,
                "continued": self.continued,
            },
        }


def _counts(values) -> dict:
    out: Dict[str, int] = {}
    for value in values:
        if value:
            out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _truncate(evt: dict) -> dict:
    """A log-safe copy: base64 audio replaced by its length."""
    out = {}
    for key, value in evt.items():
        if key in ("delta", "audio") and isinstance(value, str) and len(value) > 64:
            out[key] = f"<{len(value)} b64 chars>"
        else:
            out[key] = value
    return out


async def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Device-free GPT-Live protocol probe.")
    parser.add_argument("--model", default=os.environ.get("LIVE_MODEL") or "gpt-live-1")
    parser.add_argument("--backend-model", default=os.environ.get("LIVE_BACKEND_MODEL") or "gpt-6-luna")
    parser.add_argument("--voice", default="marin")
    parser.add_argument("--seconds", type=float, default=12.0,
                        help="how long to observe after each scripted prompt")
    parser.add_argument("--no-audio-format", action="store_true",
                        help="omit session.audio.format (the session config is strict)")
    parser.add_argument("--speak", action="append", default=[], metavar="PHRASE",
                        help="synthesise PHRASE and stream it in as spoken input "
                             "(repeatable); tests whether GPT-Live delegates by itself")
    parser.add_argument("--tts-voice", default="alloy")
    parser.add_argument("--no-drive", action="store_true",
                        help="skip the client-driven response.create phases")
    parser.add_argument("--no-ha-tools", action="store_true",
                        help="offer only the echo tool, no Home-Assistant-shaped ones")
    parser.add_argument("--wav", default="", help="write the concatenated PCM here (outside the repo)")
    parser.add_argument("--events", default="", help="write every server event here as JSON lines")
    parser.add_argument("--response-events", default="",
                        help="write the verbatim response.event envelopes here as JSON lines")
    parser.add_argument("--deltas", default="",
                        help="write one JSON line per output_audio delta (arrival, size, level)")
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2

    probe = LiveProbe(
        api_key=api_key, model=args.model, backend_model=args.backend_model,
        voice=args.voice, send_format=not args.no_audio_format, seconds=args.seconds,
        spoken=args.speak, drive_backend=not args.no_drive,
        ha_tools=not args.no_ha_tools, tts_voice=args.tts_voice,
    )
    report = await probe.run()
    if args.wav and probe.audio:
        _wav(args.wav, bytes(probe.audio), LIVE_SAMPLE_RATE)
        report["wav"] = args.wav
    for path, rows in ((args.events, probe.events),
                       (args.response_events, probe.response_events),
                       (args.deltas, [d.__dict__ for d in probe.deltas])):
        if not path:
            continue
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
    print(json.dumps(report, indent=2, default=str))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
