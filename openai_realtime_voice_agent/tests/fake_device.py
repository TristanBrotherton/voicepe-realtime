"""A model of the device's speaker path, on a virtual clock.

Three pieces, for end-to-end tests of the speaker owner (app/speaker_owner.py)
without hardware:

- ``VirtualTimeLoop``: an asyncio event loop whose clock jumps straight to
  the next scheduled callback, so a 30 s scenario runs in milliseconds and
  every run of a seed is identical.
- ``FakeDevice``: the firmware's speaker path in both framings. ``utt1``:
  utterance descriptors over one ring, a cold-start prime, the start rule,
  ``utt_ack`` (playing, progress, done, cut, dropped) and the discard window
  between an interrupt and its ``interrupt_ack``. ``legacy``: one byte stream,
  prebuffer plus prime on a cold chain, and an interrupt that suppresses audio
  until the next turn. A 48 B/ms consumer records exactly what was heard.
- ``Scenario`` / ``run_scenario`` / ``check_properties``: scripted producers
  (model speech streamed with measured jitter, complete clips and
  announcements), injected interrupts and link loss, and the properties
  P1, P2, P5, P6 and P7 of the design checked against the heard stream.

PCM encodes its origin: every sample of scenario item ``k`` is
``(k + 1) * 256 + w`` with ``w`` a 20 ms word counter, so the heard stream
decodes back to (item, position) and a cut, a gap or an interleave is visible.
"""
from __future__ import annotations

import asyncio
import json
import random
import selectors
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app import speaker_owner as so

BYTES_PER_S = 48000
SAMPLES_PER_WORD = 480  # 20 ms


# ---------------------------------------------------------------------------
# virtual time
# ---------------------------------------------------------------------------

class _VirtualSelector:
    def __init__(self, real, loop):
        self._real = real
        self._loop = loop

    def select(self, timeout=None):
        ready = self._real.select(0)
        if ready:
            return ready
        if timeout is None:
            raise RuntimeError("virtual loop: nothing scheduled and nothing ready (deadlock)")
        if timeout > 0:
            self._loop._virtual_now += timeout
        return []

    def __getattr__(self, name):
        return getattr(self._real, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop on virtual time: sleeping costs nothing."""

    def __init__(self):
        super().__init__(selectors.DefaultSelector())
        self._virtual_now = 1000.0
        self._selector = _VirtualSelector(self._selector, self)

    def time(self):
        return self._virtual_now


def run_virtual(coro):
    loop = VirtualTimeLoop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# PCM that says where it came from
# ---------------------------------------------------------------------------

def tagged_pcm(key: int, ms: int) -> bytes:
    if not 0 <= key < 126:
        raise ValueError("item keys run 0..125")
    samples = 24 * ms
    return b"".join(struct.pack("<h", (key + 1) * 256 + (i // SAMPLES_PER_WORD) % 256)
                    for i in range(samples))


def decode_heard(segments: List[Tuple[float, bytes]]) -> List[Tuple[int, float, int]]:
    """Heard segments -> [(key, start time, nbytes)] runs, silence skipped."""
    runs: List[List] = []
    for t0, data in segments:
        for i in range(0, len(data) - 1, 2):
            (v,) = struct.unpack_from("<h", data, i)
            if v == 0:
                continue
            key = v // 256 - 1
            t = t0 + (i // 2) / 24000.0
            if runs and runs[-1][0] == key and t - runs[-1][3] < 0.002:
                runs[-1][2] += 2
                runs[-1][3] = t + 1 / 24000.0
            else:
                runs.append([key, t, 2, t + 1 / 24000.0])
    return [(k, t, n) for k, t, n, _ in runs]


# ---------------------------------------------------------------------------
# the device
# ---------------------------------------------------------------------------

@dataclass
class DeviceUtt:
    id: Optional[int]
    kind: str = ""
    rx: bytearray = field(default_factory=bytearray)
    end: Optional[int] = None
    fed: int = 0
    state: str = "queued"
    first_rx: Optional[float] = None
    started: bool = False
    primed: bool = False
    und: int = 0
    dry: bool = False
    last_progress: float = 0.0


class FakeDevice:
    """The firmware's speaker path (see the module docstring)."""

    def __init__(self, mode: str = so.UTT, *, prime_ms: int = 200, cushion_ms: int = 0,
                 prebuffer_ms: int = 150, latency_s: float = 0.004, jitter_s: float = 0.004,
                 tick_s: float = 0.01, rng: Optional[random.Random] = None):
        self.mode = mode
        self.prime_bytes = 48 * prime_ms
        self.cushion_bytes = 48 * cushion_ms
        self.prebuffer_bytes = 48 * prebuffer_ms
        self.latency_s = latency_s
        self.jitter_s = jitter_s
        self.tick_s = tick_s
        self.rng = rng or random.Random(0)
        self.loop = asyncio.get_event_loop()
        self.owner: Optional[so.SpeakerOwner] = None
        self.on_interrupt = None          # async callback(msg) on the add-on side
        self.records: Dict[int, DeviceUtt] = {}
        self.order: List[DeviceUtt] = []
        self.heard: List[Tuple[float, bytes]] = []
        self.primes: List[float] = []     # times a cold prime started
        self._rx: Optional[DeviceUtt] = None
        self._stream = DeviceUtt(id=None)
        self._priming = 0
        self._last_fed: Optional[float] = None
        self._seq = 0
        self._discard_until_ack: Optional[int] = None
        self._suppressed = False
        self._link_up = True
        self._down_at: Optional[float] = None
        self._to_device_at = 0.0
        self._to_owner_at = 0.0
        self._ticker = None
        self.received_text: List[dict] = []
        # A text frame lost on its way in (older firmware parsed each piece of
        # a text frame split across TCP reads on its own, so a split
        # `utt_end` never ended its utterance): callable(msg) -> True to lose.
        self.drop_text = None

    # -- wiring --------------------------------------------------------------

    def attach(self, owner: so.SpeakerOwner) -> None:
        self.owner = owner
        self._ticker = self.loop.call_soon(self._tick)

    def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None

    def _arrival(self, attr: str) -> float:
        now = self.loop.time()
        at = max(now + self.latency_s + self.rng.random() * self.jitter_s,
                 getattr(self, attr) + 1e-6)
        setattr(self, attr, at)
        return at

    async def send_text(self, text: str) -> None:
        """Owner -> device (a control frame)."""
        if not self._link_up:
            raise ConnectionError("link down")
        if self.drop_text is not None and self.drop_text(json.loads(text)):
            return
        self.loop.call_at(self._arrival("_to_device_at"), self._receive, text)

    async def send_bytes(self, data: bytes) -> None:
        """Owner -> device (audio)."""
        if not self._link_up:
            raise ConnectionError("link down")
        self.loop.call_at(self._arrival("_to_device_at"), self._receive, bytes(data))

    def _to_owner(self, msg: dict) -> None:
        if not self._link_up:
            return
        self.loop.call_at(self._arrival("_to_owner_at"), self._deliver, msg)

    def _deliver(self, msg: dict) -> None:
        if not self._link_up or self.owner is None:
            return
        if msg.get("type") == "utt_ack":
            self.owner.on_ack(msg)
        elif msg.get("type") == "interrupt" and self.on_interrupt is not None:
            self.loop.create_task(self.on_interrupt(msg))

    # -- inbound -------------------------------------------------------------

    def _receive(self, frame) -> None:
        if not self._link_up:
            return
        if isinstance(frame, str):
            msg = json.loads(frame)
            self.received_text.append(msg)
            if self.mode == so.LEGACY:
                return  # older firmware ignores unknown text frames
            kind = msg.get("type")
            if self._discard_until_ack is not None:
                if kind == "interrupt_ack" and msg.get("seq") == self._discard_until_ack:
                    self._discard_until_ack = None
                return
            if kind == "utt_start":
                if self._rx is not None and self._rx.end is None:
                    # As the firmware: a start before the previous end closes
                    # the previous one with what it received.
                    self._rx.end = len(self._rx.rx)
                rec = DeviceUtt(id=msg["id"], kind=msg.get("kind", ""))
                self.records[rec.id] = rec
                self.order.append(rec)
                self._rx = rec
            elif kind == "utt_end":
                rec = self.records.get(msg.get("id"))
                if rec is not None:
                    rec.end = msg.get("bytes")
                if self._rx is rec:
                    self._rx = None
            return
        now = self.loop.time()
        if self.mode == so.LEGACY:
            if self._suppressed:
                return
            if self._stream.first_rx is None:
                self._stream.first_rx = now
            self._stream.rx.extend(frame)
            return
        if self._discard_until_ack is not None or self._rx is None:
            return
        if self._rx.first_rx is None:
            self._rx.first_rx = now
        self._rx.rx.extend(frame)

    # -- playback ------------------------------------------------------------

    def _warm(self, now: float) -> bool:
        return self._last_fed is not None and now - self._last_fed <= 0.6

    def _current(self) -> Optional[DeviceUtt]:
        if self.mode == so.LEGACY:
            return self._stream
        for rec in self.order:
            if rec.state in ("queued", "playing"):
                return rec
        return None

    def _tick(self) -> None:
        now = self.loop.time()
        budget = int(self.tick_s * BYTES_PER_S)
        offset = 0
        while budget > 0:
            if self._priming > 0:
                n = min(budget, self._priming)
                self._priming -= n
                budget -= n
                offset += n
                self._last_fed = now
                continue
            rec = self._current()
            if rec is None:
                break
            avail = len(rec.rx) - rec.fed
            if self.mode == so.LEGACY:
                if avail <= 0:
                    break
                if not self._warm(now) and not rec.primed:
                    if avail < self.prebuffer_bytes:
                        break
                    rec.primed = True
                    self._priming = self.prime_bytes
                    self.primes.append(now)
                    continue
            elif not rec.started:
                if avail <= 0 and rec.end is None:
                    break
                if not self._warm(now) and not rec.primed:
                    rec.primed = True
                    self._priming = self.prime_bytes
                    self.primes.append(now)
                    continue
                if (self.cushion_bytes and avail < self.cushion_bytes and rec.end is None
                        and now - (rec.first_rx or now) < 0.3):
                    break
                rec.started = True
                rec.state = "playing"
                rec.last_progress = now
                self._to_owner({"type": "utt_ack", "id": rec.id, "st": "playing", "fed": 0,
                                "wait_ms": int(round((now - (rec.first_rx or now)) * 1000))})
            if avail <= 0:
                if self.mode != so.LEGACY and rec.end is not None and rec.fed >= rec.end:
                    self._finish(rec, "done")
                    continue
                if not rec.dry:
                    rec.und += 1
                    rec.dry = True
                break
            n = min(avail, budget)
            n -= n % 2
            if n <= 0:
                break
            self.heard.append((now + offset / BYTES_PER_S, bytes(rec.rx[rec.fed:rec.fed + n])))
            rec.fed += n
            rec.dry = False
            budget -= n
            offset += n
            self._last_fed = now
            if self.mode == so.LEGACY:
                if rec.fed >= len(rec.rx):
                    # fully played: keep the ring small, the stream continues
                    del rec.rx[:rec.fed]
                    rec.fed = 0
                continue
            if rec.end is not None and rec.fed >= rec.end:
                self._finish(rec, "done")
        if self.mode == so.LEGACY and self._stream.primed and not self._warm(now):
            self._stream.primed = False
        for rec in self.order:
            if rec.state == "playing" and now - rec.last_progress >= 1.0:
                rec.last_progress = now
                self._to_owner({"type": "utt_ack", "id": rec.id, "st": "progress", "fed": rec.fed})
        self._ticker = self.loop.call_at(now + self.tick_s, self._tick)

    def _finish(self, rec: DeviceUtt, state: str) -> None:
        rec.state = state
        msg = {"type": "utt_ack", "id": rec.id, "st": state, "fed": rec.fed}
        if state in ("done", "cut"):
            msg["und"] = rec.und
        self._to_owner(msg)

    # -- injected events -----------------------------------------------------

    def interrupt(self, reason: str = "stop") -> None:
        """A user interrupt on the device: flush, report, discard until ack."""
        now = self.loop.time()
        self._priming = 0
        self._last_fed = None
        if self.mode == so.LEGACY:
            self._stream.rx.clear()
            self._stream.fed = 0
            self._stream.primed = False
            self._suppressed = True
            self._to_owner({"type": "interrupt"})
            return
        self._seq += 1
        playing = next((r for r in self.order if r.state == "playing"), None)
        msg = {"type": "interrupt", "reason": reason, "seq": self._seq}
        if playing is not None:
            msg.update(utt=playing.id, fed=playing.fed)
        self._to_owner(msg)
        for rec in self.order:
            if rec.state == "playing":
                self._finish(rec, "cut")
            elif rec.state == "queued":
                self._finish(rec, "dropped")
        self._rx = None
        self._discard_until_ack = self._seq
        del now

    def stall(self, seconds: float, uplink: bool = False) -> None:
        """A Wi-Fi stall: nothing sent to the device from now on arrives for
        ``seconds`` (and, with ``uplink``, nothing it sends back either).
        The order is kept; what was held arrives in a burst."""
        until = self.loop.time() + seconds
        self._to_device_at = max(self._to_device_at, until)
        if uplink:
            self._to_owner_at = max(self._to_owner_at, until)

    def resume(self) -> None:
        """Legacy: the next turn's `listening` ends the post-stop suppression."""
        self._suppressed = False

    def link_down(self) -> None:
        """The WebSocket drops. The device keeps playing what it holds."""
        self._link_up = False
        self._down_at = self.loop.time()
        self._rx = None
        for rec in self.order:
            if rec.state in ("queued", "playing") and rec.end is None:
                # never completed on the wire: it plays what it has, then ends
                rec.end = len(rec.rx)


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

@dataclass
class Item:
    key: int
    kind: str
    turn: str
    at: float           # seconds after the scenario starts
    ms: int             # audio length
    streamed: bool = False
    pace: float = 1.05  # model speed (audio seconds per wall second)
    spikes: Tuple[float, ...] = ()   # extra stalls (s) at random points


@dataclass
class Scenario:
    items: List[Item]
    interrupts: List[Tuple[float, str]] = field(default_factory=list)
    disconnect_at: Optional[float] = None
    mode: str = so.UTT
    seed: int = 0
    horizon: float = 0.0


@dataclass
class Result:
    scenario: Scenario
    utts: Dict[int, so.Utterance]
    pcm: Dict[int, bytes]
    first_feed: Dict[int, float]
    fed_at: Dict[int, List[Tuple[float, int]]]
    device: FakeDevice
    owner: so.SpeakerOwner
    heads: List[int]
    head_time: Dict[int, float]
    interrupt_times: List[float]
    t0: float


async def _produce(owner: so.SpeakerOwner, item: Item, pcm: bytes, result: Result,
                   loop, rng: random.Random, t0: float) -> None:
    await asyncio.sleep(max(0.0, t0 + item.at - loop.time()))
    if not item.streamed:
        result.first_feed[item.key] = loop.time()
        result.fed_at[item.key] = [(loop.time(), len(pcm))]
        result.utts[item.key] = owner.submit(item.kind, pcm, turn=item.turn)
        return
    utt = owner.open(item.kind, turn=item.turn)
    result.utts[item.key] = utt
    period = 0.095
    pos = 0
    spikes = list(item.spikes)
    while pos < len(pcm):
        if spikes and rng.random() < 0.15:
            await asyncio.sleep(spikes.pop())
        n = int(BYTES_PER_S * period * item.pace * rng.uniform(0.95, 1.06))
        n -= n % 2
        if pos == 0:
            result.first_feed[item.key] = loop.time()
        if not owner.feed(utt, pcm[pos:pos + n]) and utt.terminal:
            return
        pos += n
        result.fed_at.setdefault(item.key, []).append((loop.time(), pos))
        await asyncio.sleep(period * rng.uniform(0.9, 1.1))
    owner.end(utt)


async def _run(scenario: Scenario) -> Result:
    loop = asyncio.get_running_loop()
    rng = random.Random(scenario.seed)
    device = FakeDevice(scenario.mode, rng=random.Random(scenario.seed + 1))
    owner = so.SpeakerOwner(device.send_text, device.send_bytes, framing=scenario.mode,
                            clock=loop.time, prime_ms=200 if scenario.mode == so.UTT else 350)
    result = Result(scenario, {}, {}, {}, {}, device, owner, [], {}, [], loop.time())

    def _observe(utt, event):
        if event == "head":
            key = next((k for k, u in result.utts.items() if u is utt), None)
            if key is not None:
                result.heads.append(key)
                result.head_time[key] = loop.time()
    owner.observers.append(_observe)

    async def _on_interrupt(msg):
        owner.interrupt(msg.get("seq"), msg.get("reason") or "stop", msg.get("utt"), msg.get("fed"))
        await asyncio.sleep(0.03)  # the add-on tells the model, then acks
        await owner.acknowledge_interrupt(msg.get("seq"))
    device.on_interrupt = _on_interrupt
    device.attach(owner)

    t0 = result.t0
    for item in scenario.items:
        result.pcm[item.key] = tagged_pcm(item.key, item.ms)
    tasks = [loop.create_task(_produce(owner, item, result.pcm[item.key], result, loop, rng, t0))
             for item in scenario.items]

    async def _inject():
        events = [(t, "interrupt", r) for t, r in scenario.interrupts]
        if scenario.disconnect_at is not None:
            events.append((scenario.disconnect_at, "disconnect", ""))
        for t, what, reason in sorted(events):
            await asyncio.sleep(max(0.0, t0 + t - loop.time()))
            if what == "interrupt":
                result.interrupt_times.append(loop.time())
                device.interrupt(reason)
                if scenario.mode == so.LEGACY:
                    loop.call_later(0.3, device.resume)
            else:
                device.link_down()
                owner.link_lost()
    tasks.append(loop.create_task(_inject()))
    horizon = scenario.horizon or (max([i.at + i.ms / 1000.0 * 2 for i in scenario.items] + [0])
                                   + 6.0)
    await asyncio.sleep(horizon)
    for task in tasks:
        if not task.done():
            task.cancel()
    for outcome in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(outcome, Exception) and not isinstance(outcome, asyncio.CancelledError):
            raise outcome
    device.stop()
    if owner._task is not None:
        owner._task.cancel()
        await asyncio.gather(owner._task, return_exceptions=True)
    return result


def run_scenario(scenario: Scenario) -> Result:
    return run_virtual(_run(scenario))


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

def heard_by_key(result: Result) -> Tuple[Dict[int, bytes], List[Tuple[int, float, int]],
                                          Dict[int, Tuple[float, float]]]:
    runs = decode_heard(result.device.heard)
    data: Dict[int, bytearray] = {}
    span: Dict[int, Tuple[float, float]] = {}
    for t0, seg in result.device.heard:
        for i in range(0, len(seg) - 1, 2):
            (v,) = struct.unpack_from("<h", seg, i)
            if v == 0:
                continue
            key = v // 256 - 1
            data.setdefault(key, bytearray()).extend(seg[i:i + 2])
            t = t0 + (i // 2) / 24000.0
            first, _ = span.get(key, (t, t))
            span[key] = (first, t + 1 / 24000.0)
    return {k: bytes(v) for k, v in data.items()}, runs, span


def check_properties(result: Result) -> List[str]:
    """P1, P2, P5, P6, P7 over one run; returns the violations."""
    sc = result.scenario
    errors: List[str] = []
    heard, runs, span = heard_by_key(result)
    items = {i.key: i for i in sc.items}
    lost_at = sc.disconnect_at
    interrupts = result.interrupt_times
    # P1 whole or nothing (or a prefix ended by a user interrupt / link loss)
    for key, item in items.items():
        pcm = result.pcm[key]
        got = heard.get(key, b"")
        utt = result.utts.get(key)
        if got != pcm[:len(got)]:
            errors.append(f"P1 item {key}: heard bytes are not a prefix of its audio")
            continue
        if 0 < len(got) < len(pcm):
            outcome = utt.state if utt is not None else None
            # The device's truth: it stopped at a user interrupt. (With legacy
            # framing the add-on's outcome is an estimate; P7 covers utt1.)
            at_interrupt = key in span and any(
                abs(span[key][1] - t) <= 0.05 for t in interrupts)
            if outcome not in (so.CUT, so.LOST) and not (sc.mode == so.LEGACY and at_interrupt):
                errors.append(f"P1 item {key} ({item.kind}): partial {len(got)}/{len(pcm)} "
                              f"with outcome {outcome}")
        if not got and item.kind not in so.DROPPABLE and utt is not None:
            submitted = result.t0 + item.at
            excused = (utt.state == so.LOST
                       or any(t >= submitted - 0.01 for t in interrupts)
                       or utt.reason == "overflow")
            if not excused:
                errors.append(f"P1 item {key} ({item.kind}): must-finish never heard "
                              f"(outcome {utt.state}/{utt.reason})")
    # P2 no interleaving; heard order is the owner's order
    seen: List[int] = []
    for key, _t, _n in runs:
        if seen and seen[-1] == key:
            continue
        if key in seen:
            errors.append(f"P2 item {key} heard again after another utterance (interleave)")
        seen.append(key)
    head_order = [k for k in result.heads if k in heard]
    if [k for k in seen] != head_order[:len(seen)]:
        errors.append(f"P2 heard order {seen} differs from the owner's order {head_order}")
    # P5 at most one acknowledgement per turn
    per_turn: Dict[str, int] = {}
    for key, item in items.items():
        if item.kind in so.DROPPABLE and heard.get(key):
            per_turn[item.turn] = per_turn.get(item.turn, 0) + 1
    for turn, n in per_turn.items():
        if n > 1:
            errors.append(f"P5 turn {turn}: {n} acknowledgements heard")
    # P6 owner-added delay: cold start <= startup + 60 ms; back to back <= 60 ms
    # The device's own start: its prime, after (legacy) a prebuffer the source
    # has to fill first. Only what the owner adds on top counts against it.
    prime = result.device.prime_bytes / BYTES_PER_S
    link = result.device.latency_s + result.device.jitter_s + result.device.tick_s
    ordered = [k for k in seen]
    for idx, key in enumerate(ordered):
        start, _end = span[key]
        head_t = result.head_time.get(key)
        ready = max(result.first_feed.get(key, start), head_t or 0.0)
        if sc.mode == so.LEGACY:
            need = min(result.device.prebuffer_bytes, len(result.pcm[key]))
            filled = next((t for t, cum in result.fed_at.get(key, []) if cum >= need), ready)
            ready = max(ready, filled)
        if idx == 0 or span[ordered[idx - 1]][1] < ready - 0.6:
            # A legacy clip keeps its lead-in silence (old firmware has no
            # per-utterance start rule to protect its first syllable).
            lead_in = (so.LEGACY_LEAD_IN_MS / 1000.0
                       if sc.mode == so.LEGACY and items[key].kind == so.CLIP else 0.0)
            if start - ready > prime + lead_in + link + 0.06:
                errors.append(f"P6 item {key}: cold start {int((start - ready) * 1000)} ms")
        else:
            prev_end = span[ordered[idx - 1]][1]
            interrupted = any(prev_end - 0.05 <= t <= start for t in interrupts)
            if (not interrupted and result.first_feed.get(key, 1e9) < prev_end - 0.1
                    and (head_t or 1e9) < prev_end - 0.1 and start - prev_end > 0.06):
                errors.append(f"P6 item {key}: {int((start - prev_end) * 1000)} ms gap back to back")
    # P7 the add-on's outcome and fed equal the device's (utt1, link up)
    if sc.mode == so.UTT and lost_at is None:
        for key, utt in result.utts.items():
            rec = result.device.records.get(utt.id)
            dev_state = rec.state if rec is not None else so.DROPPED
            dev_fed = rec.fed if rec is not None else 0
            if rec is not None and rec.state in ("queued", "playing"):
                continue  # still playing at the horizon
            if utt.state != dev_state:
                errors.append(f"P7 item {key}: add-on {utt.state}, device {dev_state}")
            elif utt.state != so.DROPPED and utt.fed != dev_fed:
                errors.append(f"P7 item {key}: add-on fed {utt.fed}, device {dev_fed}")
    return errors


# ---------------------------------------------------------------------------
# random scenarios
# ---------------------------------------------------------------------------

def random_scenario(seed: int, mode: str) -> Scenario:
    rng = random.Random(seed)
    items: List[Item] = []
    key = 0
    t = 0.2
    interrupts: List[Tuple[float, str]] = []
    turns = rng.randint(1, 4)
    for n in range(turns):
        turn = f"t{n + 1}"
        if rng.random() < 0.6:
            ack_kind = rng.choice([so.ACK, so.CLIP])
            items.append(Item(key, ack_kind, turn, t, rng.randint(400, 900),
                              streamed=ack_kind == so.ACK))
            key += 1
            if rng.random() < 0.3:  # a second acknowledgement (one-ack rule)
                items.append(Item(key, so.CLIP, turn, t + rng.uniform(0.0, 0.6),
                                  rng.randint(400, 900)))
                key += 1
        answer = rng.choice([so.REPLY, so.RESULT, so.PROMPT])
        start = t + rng.uniform(0.1, 1.5)
        ms = rng.randint(600, 4000)
        items.append(Item(key, answer, turn, start, ms, streamed=True,
                          pace=rng.uniform(0.98, 1.11),
                          spikes=tuple(rng.choice([0.16, 0.38]) for _ in range(rng.randint(0, 2)))))
        key += 1
        end = start + ms / 1000.0
        if rng.random() < 0.3:
            items.append(Item(key, so.ANNOUNCE, "", rng.uniform(t, end + 1.0), rng.randint(800, 2500)))
            key += 1
        if rng.random() < 0.25:
            at = rng.uniform(start, end + 0.5)
            interrupts.append((at, rng.choice(["stop", "wake", "button"])))
            t = max(end, at) + (0.6 if mode == so.LEGACY else 0.2) + rng.uniform(0.2, 1.0)
        else:
            t = end + rng.uniform(0.3, 2.0)
    disconnect = None
    if rng.random() < 0.1:
        disconnect = rng.uniform(0.5, t)
    if mode == so.LEGACY:
        # the old firmware drops audio for ~0.3 s after an interrupt until the
        # next turn's listening; real turns cannot start inside that window
        items = [i for i in items
                 if not any(at - 0.05 <= i.at <= at + 0.45 for at, _ in interrupts)]
    return Scenario(items, interrupts, disconnect, mode, seed)
