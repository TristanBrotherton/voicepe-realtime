#!/usr/bin/env python3
"""Simulated Voice PE for a reproducible, measured demo.

Speaks the same WebSocket protocol as the real firmware (va_client, protocol
2) to a running add-on: a wake message, real-time 16 kHz PCM microphone audio,
then phase messages and 24 kHz reply audio back. For every scripted step it
measures, from the client side, what a person next to the device would feel:

  end_of_speech_to_first_audio_ms  last voiced frame sent -> first reply audio
                                   received. Includes the server's end-of-turn
                                   decision, the model, tools and the network;
                                   excludes the device's own playback buffer.
  reply_audio_s                    seconds of reply audio received
  audio_bursts                     separate stretches of reply audio (a slow
                                   lookup's acknowledgement shows up as 2+)
  interrupt_to_silence_ms          "stop" sent -> last reply audio received
  turn_ms                          wake -> idle

It never invents numbers. A failed step reports its outcome and no timings,
and --dry-run (a built-in simulated add-on, used to test this script without
a network or an API key) labels every figure as simulated.

Usage:
  OPENAI_API_KEY=... python3 demo/make_prompts.py      # once: synthesize prompts
  python3 demo/simulated_device.py --url ws://<demo-host>:8080/ [--token T] [--repeat 20]
  python3 demo/simulated_device.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import sys
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import websockets
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

HERE = Path(__file__).resolve().parent
MIC_RATE = 16000
REPLY_RATE = 24000
FRAME_MS = 20
FRAME_BYTES = MIC_RATE * 2 * FRAME_MS // 1000
SILENCE = b"\x00" * FRAME_BYTES
BURST_GAP_S = 0.7          # reply audio gaps longer than this split bursts
VOICED_RMS = 300           # frames quieter than this count as silence
SETTLE_S = 0.6             # quiet period that ends an interrupted reply
DRY_RUN_PROMPT_S = 1.2     # length of the stand-in prompt in dry runs


@dataclass
class Step:
    id: str
    text: str
    path: str = "fast"
    timeout_s: float = 20.0
    followup: bool = False
    interrupt_after_s: Optional[float] = None
    shows: str = ""
    behavior: str = ""      # dry-run only: "fail" or "silent" to exercise failures


@dataclass
class StepResult:
    id: str
    path: str
    outcome: str
    metrics: Dict[str, float] = field(default_factory=dict)
    detail: str = ""


def load_steps(path: Path) -> List[Step]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = set(Step.__dataclass_fields__)
    return [Step(**{k: v for k, v in raw.items() if k in fields}) for raw in data["steps"]]


# -- audio ------------------------------------------------------------------

def frame_rms(frame: bytes) -> float:
    count = len(frame) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f"<{count}h", frame[: count * 2])
    return math.sqrt(sum(s * s for s in samples) / count)


def split_frames(pcm: bytes) -> List[bytes]:
    frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    if frames and len(frames[-1]) < FRAME_BYTES:
        frames[-1] = frames[-1] + b"\x00" * (FRAME_BYTES - len(frames[-1]))
    # End of speech = the last voiced frame; trailing silence is not speech.
    while frames and frame_rms(frames[-1]) < VOICED_RMS:
        frames.pop()
    return frames


def load_prompt(audio_dir: Path, step: Step) -> bytes:
    path = Path(audio_dir) / f"{step.id}.wav"
    if not path.is_file():
        raise SystemExit(f"missing prompt {path} — run demo/make_prompts.py first")
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (MIC_RATE, 1, 2):
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit PCM")
        return wav.readframes(wav.getnframes())


def synthetic_prompt(seconds: Optional[float] = None) -> bytes:
    """A plain tone standing in for speech in dry runs (not speech)."""
    count = int(MIC_RATE * (seconds or DRY_RUN_PROMPT_S))
    return b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 220 * i / MIC_RATE)))
                    for i in range(count))


# -- the simulated device -----------------------------------------------------

class DeviceClient:
    def __init__(self, url: str, device_id: str, token: str = ""):
        self.url = url
        self.device_id = device_id
        self.token = token
        self.boot = uuid.uuid4().hex[:8]
        self.turns = 0
        self.hello: dict = {}
        self.audio: List[tuple] = []     # (arrival time, bytes)
        self.phases: List[tuple] = []    # (time, value, message)
        self.errors: List[tuple] = []    # (time, message)
        self.closed = False
        self._changed = asyncio.Event()
        self._ws = None
        self._reader = None

    async def __aenter__(self):
        sep = "&" if "?" in self.url else "?"
        url = f"{self.url}{sep}device_id={self.device_id}"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        self._ws = await connect(url, additional_headers=headers, max_size=None, open_timeout=10)
        self.hello = json.loads(await asyncio.wait_for(self._ws.recv(), 10))
        if self.hello.get("type") != "hello":
            raise RuntimeError(f"expected hello, got {self.hello!r}")
        await self._ws.send(json.dumps({"type": "start"}))
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _read(self):
        try:
            async for message in self._ws:
                now = time.monotonic()
                if isinstance(message, bytes):
                    self.audio.append((now, len(message)))
                else:
                    try:
                        data = json.loads(message)
                    except ValueError:
                        continue
                    if data.get("type") == "phase":
                        self.phases.append((now, data.get("value"), data))
                    elif data.get("type") == "error":
                        self.errors.append((now, data))
                self._changed.set()
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed = True
            self._changed.set()

    async def _wait(self, predicate, deadline: float) -> bool:
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.closed:
                return predicate()
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), min(remaining, 0.1))
            except asyncio.TimeoutError:
                pass
        return True

    async def run_step(self, step: Step, frames: List[bytes]) -> StepResult:
        audio0, phase0, error0 = len(self.audio), len(self.phases), len(self.errors)
        t_wake = time.monotonic()
        deadline = t_wake + step.timeout_s
        if not step.followup:
            self.turns += 1
            await self._ws.send(json.dumps({
                "type": "wake", "turn": f"{self.boot}-{self.turns}", "src": "button",
                "model": "demo_sim", "fw": "demo-sim",
            }))
        # Real-time microphone stream: the voiced prompt, then silence until
        # the reply starts (as the device keeps its mic open until then).
        next_t = time.monotonic()
        for frame in frames:
            await self._ws.send(frame)
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))
        t_speech_end = time.monotonic()
        while (len(self.audio) == audio0 and len(self.errors) == error0
               and not self.closed and time.monotonic() < deadline):
            await self._ws.send(SILENCE)
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

        if len(self.errors) > error0:
            return StepResult(step.id, step.path, "error", detail=str(self.errors[error0][1].get("message", ""))[:120])
        if self.closed:
            return StepResult(step.id, step.path, "disconnected")
        if len(self.audio) == audio0:
            return StepResult(step.id, step.path, "timeout", detail=f"no reply audio within {step.timeout_s:g} s")

        t_first = self.audio[audio0][0]
        metrics = {"end_of_speech_to_first_audio_ms": round((t_first - t_speech_end) * 1000)}
        if step.interrupt_after_s:
            await self._wait(lambda: self._idle_since(phase0) or time.monotonic() >= t_first + step.interrupt_after_s,
                             deadline)
            t_interrupt = time.monotonic()
            await self._ws.send(json.dumps({"type": "interrupt"}))
            # Quiet for SETTLE_S after the last audio frame = the reply stopped.
            while time.monotonic() < deadline:
                last = self.audio[-1][0]
                if time.monotonic() - max(last, t_interrupt) >= SETTLE_S:
                    break
                await asyncio.sleep(0.05)
            after = [t for t, _ in self.audio[audio0:] if t >= t_interrupt]
            t_quiet = after[-1] if after else t_interrupt
            metrics["interrupt_to_silence_ms"] = round((t_quiet - t_interrupt) * 1000)
            # The real device goes idle by itself on "stop"; the reply ending
            # is what matters, not the add-on's later idle message.
            t_end = self._idle_since(phase0) or t_quiet
        elif not await self._wait(lambda: self._idle_since(phase0), deadline):
            return StepResult(step.id, step.path, "timeout", metrics, "reply never finished (no idle)")
        else:
            t_end = self._idle_since(phase0)
        chunks = self.audio[audio0:]
        metrics["reply_audio_s"] = round(sum(n for _, n in chunks) / (REPLY_RATE * 2), 2)
        metrics["audio_bursts"] = 1 + sum(1 for (a, _), (b, _) in zip(chunks, chunks[1:]) if b - a > BURST_GAP_S)
        metrics["turn_ms"] = round((t_end - t_wake) * 1000)
        if not step.followup:
            await self._ws.send(json.dumps({"type": "flush"}))
        return StepResult(step.id, step.path, "ok", metrics)

    def _idle_since(self, phase0: int) -> Optional[float]:
        for t, value, _ in self.phases[phase0:]:
            if value == "idle":
                return t
        return None


# -- dry run: a simulated add-on, for testing this script only ---------------

class SimulatedAddon:
    """Plays the add-on's side of the protocol with canned timing."""

    def __init__(self):
        self.step: Optional[Step] = None
        self.server = None
        self.port = 0

    def expect(self, step: Step) -> None:
        self.step = step

    async def __aenter__(self):
        self.server = await serve(self._handle, "127.0.0.1", 0, max_size=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, ws):
        await ws.send(json.dumps({"type": "hello", "proto": 2, "follow_up_ms": 4000,
                                  "follow_up_open_delay_ms": 100, "simulated": True}))
        voiced, quiet, reply = False, 0, None
        async for message in ws:
            if isinstance(message, bytes):
                if reply is not None and not reply.done():
                    continue
                if frame_rms(message) >= VOICED_RMS:
                    voiced, quiet = True, 0
                elif voiced:
                    quiet += 1
                    if quiet >= 10:          # 200 ms of silence ends the turn
                        voiced = False
                        reply = asyncio.create_task(self._reply(ws, self.step))
            else:
                data = json.loads(message)
                if data.get("type") == "interrupt" and reply is not None:
                    reply.cancel()
                    await ws.send(json.dumps({"type": "phase", "value": "idle"}))

    async def _reply(self, ws, step: Optional[Step]):
        try:
            await self._play(ws, step)
        except websockets.ConnectionClosed:
            pass

    async def _play(self, ws, step: Optional[Step]):
        behavior = step.behavior if step else ""
        if behavior == "silent":
            return
        await ws.send(json.dumps({"type": "phase", "value": "thinking"}))
        if behavior == "fail":
            await ws.send(json.dumps({"type": "error", "message": "simulated failure", "audible": True}))
            return
        chunk = b"\x00" * (REPLY_RATE * 2 * 40 // 1000)          # 40 ms
        bursts = [(0.15, 0.6)]
        if step is not None and step.path == "slow":
            bursts = [(0.2, 0.5), (1.2, 0.8)]                       # ack, then answer
        if step is not None and step.interrupt_after_s:
            bursts = [(0.15, 6.0)]
        for delay, seconds in bursts:
            await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "phase", "value": "replying"}))
            for _ in range(int(seconds / 0.04)):
                await ws.send(chunk)
                await asyncio.sleep(0.04)
        await ws.send(json.dumps({"type": "phase", "value": "idle"}))


# -- reporting ----------------------------------------------------------------

def nearest_rank(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(q * len(ordered) + 0.5 - 1e-9)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(runs: List[List[StepResult]]) -> dict:
    by_path: Dict[str, dict] = {}
    for results in runs:
        for r in results:
            entry = by_path.setdefault(r.path, {"steps": 0, "ok": 0, "latencies": []})
            entry["steps"] += 1
            if r.outcome == "ok":
                entry["ok"] += 1
                entry["latencies"].append(r.metrics["end_of_speech_to_first_audio_ms"])
    out = {}
    for path, entry in sorted(by_path.items()):
        lat = entry.pop("latencies")
        out[path] = dict(entry, success_rate=round(entry["ok"] / entry["steps"], 3),
                         end_of_speech_to_first_audio_ms={"n": len(lat), "p50": nearest_rank(lat, 0.5),
                                                          "p90": nearest_rank(lat, 0.9)})
    return out


def fmt_ms(value) -> str:
    return "-" if value is None else (f"{value / 1000:.1f} s" if value >= 10000 else f"{value:.0f} ms")


def print_table(results: List[StepResult], out=sys.stdout) -> None:
    head = f"{'step':<12} {'path':<5} {'outcome':<12} {'speech end→audio':>16} {'reply':>7} {'bursts':>6} {'stop→quiet':>10} {'turn':>8}"
    print(head, file=out)
    for r in results:
        m = r.metrics
        reply = f"{m['reply_audio_s']:.1f} s" if "reply_audio_s" in m else "-"
        print(f"{r.id:<12} {r.path:<5} {r.outcome:<12} {fmt_ms(m.get('end_of_speech_to_first_audio_ms')):>16} "
              f"{reply:>7} {str(m.get('audio_bursts', '-')):>6} {fmt_ms(m.get('interrupt_to_silence_ms')):>10} "
              f"{fmt_ms(m.get('turn_ms')):>8}" + (f"  {r.detail}" if r.detail else ""), file=out)


async def run_demo(steps: List[Step], url: str = "", token: str = "", dry_run: bool = False,
                   repeat: int = 1, audio_dir: Path = HERE / "prompts", pause_s: float = 2.0,
                   device_id: str = "demo", out=sys.stdout) -> dict:
    frames = {s.id: split_frames(synthetic_prompt() if dry_run else load_prompt(audio_dir, s)) for s in steps}
    runs: List[List[StepResult]] = []
    addon = SimulatedAddon() if dry_run else None
    if addon is not None:
        await addon.__aenter__()
        url = f"ws://127.0.0.1:{addon.port}/"
    try:
        for run in range(1, repeat + 1):
            results: List[StepResult] = []
            try:
                async with DeviceClient(url, device_id, token) as device:
                    open_delay = (device.hello.get("follow_up_open_delay_ms") or 700) / 1000
                    for index, step in enumerate(steps):
                        if addon is not None:
                            addon.expect(step)
                        nxt = steps[index + 1] if index + 1 < len(steps) else None
                        result = await device.run_step(step, frames[step.id])
                        results.append(result)
                        if result.outcome == "disconnected":
                            break
                        await asyncio.sleep(open_delay if nxt is not None and nxt.followup else pause_s)
            except (OSError, websockets.WebSocketException, RuntimeError, asyncio.TimeoutError) as e:
                results.append(StepResult("connect", "-", "error", detail=f"{type(e).__name__}: {e}"[:120]))
            print(f"\nrun {run}/{repeat}", file=out)
            print_table(results, out)
            runs.append(results)
    finally:
        if addon is not None:
            await addon.__aexit__(None, None, None)
    return {
        "schema": "voicepe.demo.results/1",
        "dry_run": dry_run,
        "note": ("SIMULATED add-on: these figures test the demo script and are not measurements"
                 if dry_run else "client-side measurements against a live add-on"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runs": [[asdict(r) for r in results] for results in runs],
        "summary": summarize(runs),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scripted, measured Voice PE demo against a simulated device.")
    parser.add_argument("--url", default=os.environ.get("VOICEPE_DEMO_URL", ""),
                        help="add-on WebSocket, e.g. ws://demo-ha.local:8080/")
    parser.add_argument("--token", default=os.environ.get("VOICEPE_DEMO_TOKEN", ""),
                        help="device token, if the add-on requires one")
    parser.add_argument("--device-id", default="demo")
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.json")
    parser.add_argument("--audio-dir", type=Path, default=HERE / "prompts")
    parser.add_argument("--repeat", type=int, default=1, help="rehearse N runs (ship a live demo at >= 19/20)")
    parser.add_argument("--pause", type=float, default=2.0, help="seconds between steps")
    parser.add_argument("--json", type=Path, help="write raw results here")
    parser.add_argument("--dry-run", action="store_true", help="use a built-in simulated add-on (tests the script)")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.url:
        parser.error("--url is required (or use --dry-run)")
    steps = load_steps(args.scenarios)
    print(f"Voice PE demo: {len(steps)} steps x {args.repeat} run(s)")
    if args.dry_run:
        print("DRY RUN: simulated add-on. The figures below test this script; they are NOT measurements.")
    report = asyncio.run(run_demo(steps, args.url, args.token, args.dry_run, args.repeat,
                                  args.audio_dir, args.pause, args.device_id))
    print("\nsummary:", json.dumps(report["summary"], indent=2))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    failed = sum(1 for run in report["runs"] for r in run if r["outcome"] != "ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
