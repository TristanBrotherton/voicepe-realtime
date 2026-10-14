"""Fix 4: the frame that means "the assistant finished answering" is
BotStoppedSpeakingFrame, but it never reaches ConnectionRecovery directly --
PhaseEmitter sits downstream of it in the pipeline and is the one that
already decides, with its debounce and in-flight-tool checks, when a reply is
genuinely over. This proves PhaseEmitter fires the success callback exactly
when that real end-of-turn idle happens, and never for a forced/aborted one.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.phase_emitter import PhaseEmitter, TurnLiveness


async def _noop(_value):
    return None


@pytest.mark.asyncio
async def test_a_clean_reply_signals_turn_success():
    calls = []
    pe = PhaseEmitter(_noop, idle_debounce_s=0)
    pe.set_turn_success_handler(lambda: calls.append(True))
    await pe._emit_idle_after_debounce()
    assert calls == [True]


@pytest.mark.asyncio
async def test_a_tool_still_running_does_not_signal_turn_success():
    """The debounce elapsing while a tool call is still in flight means the
    turn is NOT over -- PhaseEmitter shows `thinking` instead of `idle` here,
    so success must not fire either."""
    calls = []
    liveness = TurnLiveness()
    liveness.tool_started()
    pe = PhaseEmitter(_noop, idle_debounce_s=0, liveness=liveness)
    pe.set_turn_success_handler(lambda: calls.append(True))
    try:
        await pe._emit_idle_after_debounce()
        assert calls == []
    finally:
        # The "thinking" branch arms the watchdog task -- close() cancels it
        # so it doesn't stay pending after the test ends.
        await pe.close()


@pytest.mark.asyncio
async def test_a_forced_idle_does_not_signal_turn_success():
    """force_idle is the turn-DEATH path (rate limit, reconnect, watchdog) --
    the opposite of a clean finish. It must never be mistaken for success."""
    calls = []
    pe = PhaseEmitter(_noop, idle_debounce_s=0)
    pe.set_turn_success_handler(lambda: calls.append(True))
    await pe.force_idle("turn declared dead")
    assert calls == []


@pytest.mark.asyncio
async def test_a_gap_inside_one_reply_does_not_reach_idle():
    """Measured live 2026-09-09: one Gemini answer arrived in three bursts,
    with 6.7 s and 4.7 s of silence between them. Each gap outlasted the 1.5 s
    debounce, so the phase went replying -> idle -> replying twice inside one
    answer, and the device played its START CHIME on every way back in. The
    engine's own end-of-turn says when the answer is really finished; a timer
    can only guess."""
    import asyncio

    phases = []

    async def send_phase(value):
        phases.append(value)

    pe = PhaseEmitter(send_phase, idle_debounce_s=0.05)
    pe._mid_turn_grace_s = 5.0
    pe._seen_engine_end = True  # this engine does signal; a past turn proved it
    pe._model_turn_open = True  # the model started speaking, has not finished

    task = asyncio.create_task(pe._emit_idle_after_debounce())
    await asyncio.sleep(0.3)  # well past the debounce
    assert phases == []       # still mid-answer: no idle, so no chime

    pe._model_turn_open = False  # LLMFullResponseEndFrame arrives
    await asyncio.wait_for(task, timeout=2.0)
    assert phases == ["idle"]
    await pe.close()


@pytest.mark.asyncio
async def test_a_missing_end_of_turn_still_reaches_idle_on_the_cap():
    """The grace must never become a hang: an engine that sends no end-of-turn,
    or a reply that dies half-way, still has to release the device."""
    import asyncio

    phases = []

    async def send_phase(value):
        phases.append(value)

    pe = PhaseEmitter(send_phase, idle_debounce_s=0.05)
    pe._mid_turn_grace_s = 0.3
    pe._seen_engine_end = True
    pe._model_turn_open = True  # and it never closes

    await asyncio.wait_for(pe._emit_idle_after_debounce(), timeout=3.0)
    assert phases == ["idle"]
    await pe.close()


@pytest.mark.asyncio
async def test_a_finished_turn_still_idles_on_the_plain_debounce():
    """The common case must not get slower: once the engine has said the turn
    is over, the debounce alone decides, exactly as before."""
    import asyncio
    import time

    phases = []

    async def send_phase(value):
        phases.append(value)

    pe = PhaseEmitter(send_phase, idle_debounce_s=0.05)
    pe._mid_turn_grace_s = 5.0
    pe._model_turn_open = False

    t0 = time.monotonic()
    await pe._emit_idle_after_debounce()
    assert phases == ["idle"]
    assert time.monotonic() - t0 < 1.0  # not waiting on the grace
    await pe.close()


@pytest.mark.asyncio
async def test_an_engine_that_never_signals_is_never_waited_for():
    """The regression this guard exists for. LLMFullResponseEndFrame is
    consumed by LLMAssistantAggregator before it can reach PhaseEmitter, so on
    Gemini the grace was spent in full on EVERY turn: the device sat shut for
    eight seconds after each answer and the user had to repeat himself (live
    2026-09-09 19:04). An engine that has never signalled must behave exactly
    as it did before the grace existed."""
    import asyncio
    import time

    phases = []

    async def send_phase(value):
        phases.append(value)

    pe = PhaseEmitter(send_phase, idle_debounce_s=0.05)
    pe._mid_turn_grace_s = 5.0
    pe._model_turn_open = True   # a reply is open...
    pe._seen_engine_end = False  # ...but nothing has ever told us they end

    t0 = time.monotonic()
    await asyncio.wait_for(pe._emit_idle_after_debounce(), timeout=2.0)

    assert phases == ["idle"]
    assert time.monotonic() - t0 < 1.0
    await pe.close()


@pytest.mark.asyncio
async def test_the_direct_call_arms_the_grace_for_later_turns():
    """The engine's service calls note_engine_turn_complete directly. That is
    both this turn's end AND the proof that future turns may be waited for."""
    pe = PhaseEmitter(_noop, idle_debounce_s=0)
    assert pe._seen_engine_end is False

    await pe.note_engine_turn_complete()

    assert pe._seen_engine_end is True
    assert pe._model_turn_open is False
    await pe.close()


@pytest.mark.asyncio
async def test_thinking_with_nothing_from_the_engine_goes_idle(monkeypatch):
    """US-060 AC-8, Kontor 2026-10-14: a bare "Aha" cut him, Gemini sent nothing at all, and the LED sat in
    "thinking" until the next question. The watchdog must release it -- and only when no tool is running."""
    monkeypatch.setattr(PhaseEmitter, "THINKING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(PhaseEmitter, "WATCHDOG_POLL_S", 0.01)
    sent = []

    async def send(v):
        sent.append(v)

    pe = PhaseEmitter(send, idle_debounce_s=0)
    await pe._emit("thinking")
    await pe._thinking_watchdog()
    assert sent[-1] == "idle"

    liveness = TurnLiveness()
    liveness.tool_started()
    sent.clear()
    pe = PhaseEmitter(send, idle_debounce_s=0, liveness=liveness)
    await pe._emit("thinking")
    import asyncio
    task = asyncio.create_task(pe._thinking_watchdog())
    await asyncio.sleep(0.2)
    assert sent == ["thinking"]   # a running tool keeps the turn alive, with no cap
    task.cancel()


def test_the_watchdog_does_not_leave_him_thinking_for_long():
    assert PhaseEmitter.THINKING_TIMEOUT_S <= 8.0
