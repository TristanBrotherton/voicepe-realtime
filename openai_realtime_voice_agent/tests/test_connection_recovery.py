"""Verify connection teardown stops ConnectionRecovery background work."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.device_registry import DeviceConnection
from app.providers.openai_realtime import SafeRealtimeLLMService
from app.phase_emitter import PhaseEmitter
from app.websocket_handler import ConnectionRecovery, WebSocketHandler
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService


async def main():
    recovery = ConnectionRecovery(object())
    refresh_task = asyncio.create_task(asyncio.Event().wait())
    recover_task = asyncio.create_task(asyncio.Event().wait())
    recovery._refresh_task = refresh_task
    recovery._recover_task = recover_task
    phase_emitter = PhaseEmitter(None)
    phase_task = asyncio.create_task(asyncio.Event().wait())
    phase_emitter._idle_task = phase_task

    connection = DeviceConnection(
        "kitchen", object(), recovery=recovery, phase_emitter=phase_emitter
    )
    await WebSocketHandler()._teardown(connection)

    assert refresh_task.cancelled()
    assert recover_task.cancelled()
    assert phase_task.cancelled()
    assert connection.recovery is None
    assert connection.phase_emitter is None

    class FakeRecovery:
        def __init__(self):
            self.reasons = []

        async def force_reconnect(self, reason):
            self.reasons.append(reason)

    handler = WebSocketHandler()
    handler.WEDGE_TIMEOUT_S = 0
    wedge_recovery = FakeRecovery()
    wedge_connection = DeviceConnection("kitchen", object(), recovery=wedge_recovery)
    wedge_phase = PhaseEmitter(None)
    await handler._wedge_check(wedge_connection, wedge_phase, 1.0)
    assert wedge_recovery.reasons == ["wedge: silent after wake"]

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingService:
        async def reset_conversation(self):
            started.set()
            await release.wait()

    live_recovery = ConnectionRecovery(BlockingService())
    live_connection = DeviceConnection("office", object(), recovery=live_recovery)
    wedge_task = asyncio.create_task(
        handler._wedge_check(live_connection, PhaseEmitter(None), 1.0)
    )
    await started.wait()
    await handler._teardown(live_connection)
    try:
        await wedge_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("wedge recovery survived connection teardown")

    # reset_conversation deliberately closes the old receive task. That must
    # not emit a second connection-death ErrorFrame into a stopped processor.
    service = object.__new__(SafeRealtimeLLMService)
    service._resetting_conversation = True
    errors = []

    async def receive_ended(_service):
        return None

    async def push_error(**kwargs):
        errors.append(kwargs)

    service.push_error = push_error
    with patch.object(OpenAIRealtimeLLMService, "_receive_task_handler", receive_ended):
        await service._receive_task_handler()
    assert errors == []
    print("ALL ASSERTIONS PASSED")


asyncio.run(main())


"""Recovery must know which engine it is nursing, and when not to nurse."""

import time as _time

import pytest

from app.provider_router import ProviderRouter


def _healthy(provider):
    """The backup answers its probe (switch-only-to-healthy, 2026-10-02)."""
    return True

from pipecat.frames.frames import ErrorFrame as _ErrorFrame
from pipecat.processors.frame_processor import FrameDirection


# A realistic OpenAI send-side death flood message. "Error sending client
# event: …" is pipecat's own OpenAIRealtimeLLMService._send_client_event
# wording (see app/websocket_handler.py's _DEATH_MARKERS comment) -- the
# "client event" phrase is required, paired with a close-code marker, for
# _is_dead_socket to treat this as OUR socket being closed (Fix 1, round 2:
# a bare "keepalive ping timeout" with no send-flood phrase must NOT repair).
_DEAD_SOCKET_MSG = "Error sending client event: sent 1011 (keepalive ping timeout)"


class FakeService:
    def __init__(self):
        self.resets = 0

    async def reset_conversation(self):
        self.resets += 1


def _recovery(provider, router, switched):
    async def on_failover():
        switched.append(True)

    return ConnectionRecovery(
        FakeService(),
        provider=provider,
        router=router,
        on_failover=on_failover,
    )


@pytest.mark.asyncio
async def test_a_dropped_socket_on_openai_is_repaired_in_place():
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error(_DEAD_SOCKET_MSG)
    assert rec._service.resets == 1
    assert switched == []


@pytest.mark.asyncio
async def test_a_close_code_without_the_send_flood_marker_does_not_repair():
    """Fix 1, round 2: before this task, a death marker only counted paired
    with "client event" in the message -- deliberately, since that phrase can
    only come from OpenAIRealtimeLLMService's own send path, so it is what
    tells OUR socket failing apart from some unrelated error that merely
    happens to mention a close code (e.g. a device-side disconnect surfacing
    the same numeric code). A bare close-code message, with no send-flood
    phrase, must not repair the connection -- it is still reported to the
    router (classify() has no such pairing requirement), just not repaired."""
    switched = []
    router = ProviderRouter("openai", None)
    rec = _recovery("openai", router, switched)
    await rec.handle_error("WebSocket closed unexpectedly: 1006")
    assert rec._service.resets == 0
    assert router._strikes.get("openai") == 1


@pytest.mark.asyncio
async def test_gemini_repairs_itself_so_we_keep_our_hands_off():
    """Fix 6: the old version of this test asserted only `switched == []`,
    which still passes if the self_heals guard is deleted (nothing here has a
    backup to switch to, so switched stays empty regardless). The actual
    headline behaviour -- Gemini's own reconnect logic is left alone -- is
    that reset_conversation is never called; assert that directly."""
    switched = []
    router = ProviderRouter("gemini", "openai", probe=_healthy)
    rec = _recovery("gemini", router, switched)
    await rec.handle_error("keepalive ping timeout")
    assert switched == []
    assert rec._service.resets == 0


@pytest.mark.asyncio
async def test_out_of_money_is_never_repaired_only_switched():
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error("You exceeded your current quota")
    assert rec._service.resets == 0
    assert switched == [True]
    assert router.current() == "gemini"


@pytest.mark.asyncio
async def test_a_tool_failure_neither_repairs_nor_switches():
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error("play_media failed: 500 Internal Server Error")
    assert rec._service.resets == 0
    assert switched == []
    assert router.current() == "openai"


@pytest.mark.asyncio
async def test_a_second_dropped_socket_switches_engine():
    """Only the router-strike accounting matters here (not the repair, hence
    the bare, non-send-flood message is fine): two TRANSIENT reports of the
    same failure must exceed the one-retry budget and switch engines."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error("keepalive ping timeout")
    rec._last_reported_at = 0.0  # step past the flood-collapse cooldown
    await rec.handle_error("keepalive ping timeout")
    assert switched == [True]


@pytest.mark.asyncio
async def test_out_of_money_reaches_the_router_through_an_ordinary_error_frame():
    """Ruling 2, made concrete: an out-of-money error carries none of the
    close-socket signatures (no "client event", no "session_expired", no
    "realtime receive loop") that used to gate whether an ErrorFrame got any
    decision at all. If process_frame still gated on those signatures before
    calling handle_error, this ErrorFrame would fall straight to the plain
    idle-unstick and the router would never be asked, so switched would stay
    empty. Going through process_frame itself (not calling handle_error
    directly, like the tests above) is what actually exercises that wiring."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    try:
        await rec.process_frame(
            _ErrorFrame("You exceeded your current quota"), FrameDirection.UPSTREAM
        )
        await rec._recover_task
        assert switched == [True]
        assert router.current() == "gemini"
    finally:
        await rec.close()


@pytest.mark.asyncio
async def test_a_rate_limit_gets_only_an_idle_nudge_never_a_reconnect():
    """Fix 1: classify() returns TRANSIENT for a rate limit -- and for any
    message it does not recognise at all -- not only for a dead socket. Using
    "not APP" as the repair trigger (what an earlier version of handle_error
    did) would call reset_conversation() on a merely-rate-limited but
    perfectly live session. Only an actual connection-death signature
    (_is_dead_socket) may repair; everything else must fall through to the
    exact old behaviour: reported to the router (so a money/auth failure
    hiding behind similar wording can still switch engines), never repaired,
    idle nudge fires so the device does not hang in `thinking`."""
    switched = []
    router = ProviderRouter("openai", None)
    rec = _recovery("openai", router, switched)
    idled = []

    async def fake_unstick(msg):
        idled.append(msg)

    rec._unstick_idle = fake_unstick
    try:
        await rec.process_frame(
            _ErrorFrame("Rate limit reached for requests"), FrameDirection.UPSTREAM
        )
        await rec._recover_task
        assert rec._service.resets == 0
        assert idled == ["Rate limit reached for requests"]
    finally:
        await rec.close()


@pytest.mark.asyncio
async def test_a_flood_of_identical_errors_is_handled_once():
    """Fix 2: a dead socket delivers the SAME message ~15x/s. In production
    process_frame's own `_reconnecting` guard stops most of that flood from
    ever reaching handle_error again -- but there is a real scheduling gap
    between creating the task and it actually setting `_reconnecting`, and
    calling handle_error directly (as this test does) removes that guard
    entirely. Without the flood-collapse window, the SECOND of these three
    identical calls would already push the strike count past the one-retry
    budget and switch engines (so a third call would land on gemini instead
    of openai, and the reconnect attempted here would never even be the
    second of two on the same engine) -- proved by asserting exactly one
    strike, no switch, and exactly one reset_conversation call across all
    three, i.e. that the whole trio was handled as a single occurrence."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error(_DEAD_SOCKET_MSG)
    await rec.handle_error(_DEAD_SOCKET_MSG)
    await rec.handle_error(_DEAD_SOCKET_MSG)
    assert switched == []
    # 2026-10-02: a dead socket that reconnects is no strike at all.
    assert not router._strikes.get("openai")
    assert rec._service.resets == 1


@pytest.mark.asyncio
async def test_a_duplicate_dead_socket_message_does_not_nudge_a_fresh_turns_phase():
    """Fix 2, round 2: a duplicate dead-socket message inside the flood-
    collapse window must report itself as "handled" (True), not "nothing to
    do" (False) -- False would send it through _route_error's plain
    idle-unstick, which goes through PhaseEmitter.force_idle() and sets
    _suppress_thinking. If the earlier repair already succeeded and the user
    has since started a fresh turn, that straggler frame would clobber the
    NEW turn's phase -- the exact race documented at
    app/phase_emitter.py:35-48. Driven through the real process_frame path
    (not handle_error directly) so _route_error's nudge-or-not decision is
    actually exercised."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    idled = []

    async def fake_unstick(msg):
        idled.append(msg)

    rec._unstick_idle = fake_unstick
    try:
        await rec.process_frame(_ErrorFrame(_DEAD_SOCKET_MSG), FrameDirection.UPSTREAM)
        await rec._recover_task
        # A second, duplicate frame arrives right after the repair succeeded
        # (e.g. one last queued mic frame's send failure from the dead
        # socket, or a straggler from the flood).
        await rec.process_frame(_ErrorFrame(_DEAD_SOCKET_MSG), FrameDirection.UPSTREAM)
        if rec._recover_task is not None:
            await rec._recover_task
        assert idled == []
        assert rec._service.resets == 1
    finally:
        await rec.close()


@pytest.mark.asyncio
async def test_two_different_death_messages_in_a_row_repair_only_once():
    """Final review, Fix 2: the repair COOLDOWN, which is a different guard
    from the message-dedup and cannot be replaced by it.

    One socket death arrives twice, under two different strings: the reader
    dies first ("realtime receive loop died: …") and is repaired; when that
    repair finishes, the sends queued against the dead socket surface the
    same event as "Error sending client event: sent 1011 …". The dedup is
    keyed by message text, so it cannot see those as duplicates -- without
    the cooldown the second one repairs again immediately, and every repair
    now also re-seeds the conversation onto a session that just got one.

    Run on the SHIPPED default (no backup engine), which is where this
    actually bites: with a backup configured the second failure fails over
    instead. The strike assertion is what proves the two guards are doing
    different jobs -- the second message WAS reported to the router (so it
    was not deduped away), and was still not repaired a second time."""
    switched = []
    router = ProviderRouter("openai", None)  # default config: no failover
    rec = _recovery("openai", router, switched)
    try:
        await rec.handle_error("realtime receive loop died: ConnectionClosed")
        assert rec._service.resets == 1  # the real repair

        await rec.handle_error(
            "Error sending client event: sent 1011 (keepalive ping timeout)"
        )

        assert rec._service.resets == 1, (
            "a second, differently-worded report of the SAME socket death "
            "repaired again inside the cooldown"
        )
        # A reconnected socket is no strike (2026-10-02), so "not deduped"
        # is read off the dedup's own record instead of the strike count.
        assert rec._last_error_message.startswith("Error sending client event"), (
            "the second message was deduped, so this test is not exercising "
            "the cooldown"
        )
        assert not router._strikes.get("openai")
        assert switched == []
    finally:
        await rec.close()


@pytest.mark.asyncio
async def test_a_different_message_right_after_is_not_deduped():
    """The flood-collapse window is keyed by message text, not merely by
    time, so a genuinely different failure right after a first one must still
    be reported (mutation check for "keyed by message": if the dedup ignored
    the message and only checked the time window, this second, different
    failure would be silently dropped too)."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error("keepalive ping timeout")
    await rec.handle_error("You exceeded your current quota")
    assert switched == [True]
    assert router.current() == "gemini"


@pytest.mark.asyncio
async def test_a_failover_decision_with_no_switch_callback_yet_still_lets_the_idle_nudge_fire():
    """Fix 3: on_failover is None until Task 8 wires it in. If handle_error
    returned True whenever the router decided to switch -- regardless of
    whether anything could act on that decision -- the device would get
    neither an engine switch (nothing rebuilds the connection) nor an idle
    nudge (True suppresses it): stuck in `thinking` forever. Driving the real
    process_frame -> _route_error path (not just reading handle_error's
    return value, as an earlier version of this test did) is what actually
    proves the nudge fires, not merely that nothing else happened."""
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = ConnectionRecovery(FakeService(), provider="openai", router=router, on_failover=None)
    idled = []

    async def fake_unstick(msg):
        idled.append(msg)

    rec._unstick_idle = fake_unstick
    try:
        await rec.process_frame(
            _ErrorFrame("You exceeded your current quota"), FrameDirection.UPSTREAM
        )
        await rec._recover_task
        assert router.current() == "gemini"  # the router still switched...
        assert rec._service.resets == 0
        assert idled == ["You exceeded your current quota"]  # ...but the device is nudged, not left hanging
    finally:
        await rec.close()


@pytest.mark.asyncio
async def test_note_turn_success_resets_the_strike_budget_under_the_shipped_config():
    """Fix 4: with this house's shipped config (transcription_language empty)
    OpenAI never emits a TranscriptionFrame at all, so the reset signal
    cannot depend on one. note_turn_success() is what PhaseEmitter calls when
    a reply genuinely finishes (wired regardless of transcription config), so
    calling it directly here proves the reset works with no transcription
    frame anywhere in the picture -- exactly the shipped default. Mutation
    check: without note_turn_success clearing the strike, this test fails
    exactly like test_a_second_dropped_socket_switches_engine passes (switched
    becomes [True]) -- confirming the assertion depends on the reset."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    await rec.handle_error(_DEAD_SOCKET_MSG)
    # Step past BOTH windows, which guard different things (see handle_error):
    # the flood-collapse dedup on the repeated message, and the repair
    # cooldown on repairing again. These two hiccups are meant to be far
    # apart in time -- a good turn happens between them.
    rec._last_reported_at = 0.0
    rec._last_attempt = 0.0

    # A turn finishes cleanly on this engine in between the two hiccups --
    # never a TranscriptionFrame, exactly like OpenAI with transcription off.
    rec.note_turn_success()

    await rec.handle_error(_DEAD_SOCKET_MSG)

    assert switched == []
    assert router.current() == "openai"
    assert rec._service.resets == 2


@pytest.mark.asyncio
async def test_note_turn_success_only_clears_this_connections_own_engine():
    """note_success must be keyed by the engine that is actually running, not
    fired blindly -- a success signal from a stale/other connection must not
    reset an unrelated engine's strikes. (ProviderRouter.note_success is
    itself keyed by provider name; this proves ConnectionRecovery passes its
    own _provider, not something else.)"""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("gemini", router, switched)  # this connection IS gemini
    router._strikes["gemini"] = 1  # simulate an earlier hiccup already charged
    router._strikes["openai"] = 1  # a DIFFERENT engine's charge -- must survive
    rec.note_turn_success()
    assert "gemini" not in router._strikes
    assert router._strikes.get("openai") == 1


@pytest.mark.asyncio
async def test_a_successful_wedge_repair_resets_the_strike_budget():
    """Fix 4's second half: "let a successful repair reset the budget too."
    force_reconnect's wedge repair never goes through handle_error/
    report_failure at all (a wedge is detected positively, from silence, not
    from an ErrorFrame) -- so crediting it immediately, unlike a reactive
    handle_error repair, cannot erase a strike that a second real failure
    still needed to be measured against."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    router._strikes["openai"] = 1  # a prior hiccup already charged
    rec._last_attempt = 0.0
    await rec.force_reconnect("wedge: silent after wake")
    assert "openai" not in router._strikes
    assert rec._service.resets == 1


@pytest.mark.asyncio
async def test_a_failed_wedge_repair_does_not_credit_success():
    """Fix 3: the whole point of `_recover`'s boolean return is that a FAILED
    repair must not credit success -- without checking it, force_reconnect
    would call note_success unconditionally and this test alone would not
    tell the difference from the passing case above. Make reset_conversation
    itself fail, and the earlier strike must survive."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    router._strikes["openai"] = 1  # a prior hiccup already charged

    async def failing_reset():
        raise RuntimeError("still broken")

    rec._service.reset_conversation = failing_reset
    rec._last_attempt = 0.0
    await rec.force_reconnect("wedge: silent after wake")
    assert router._strikes.get("openai") == 1  # NOT cleared -- the repair failed


@pytest.mark.asyncio
async def test_a_successful_proactive_refresh_credits_success():
    """Fix 3: the proactive-refresh credit path had no test at all. Ages the
    session past REFRESH_AGE_S/REFRESH_QUIET_S by hand (rather than sleeping
    through the real 55-minute/60-second windows) and drives the extracted
    _maybe_proactive_refresh directly -- the same decision+credit code the
    background loop calls after each REFRESH_CHECK_S sleep."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    router._strikes["openai"] = 1  # a prior hiccup already charged
    now = _time.monotonic()
    rec._connected_at = now - rec.REFRESH_AGE_S - 1
    rec._last_input_audio = now - rec.REFRESH_QUIET_S - 1
    rec._last_attempt = 0.0
    await rec._maybe_proactive_refresh()
    assert "openai" not in router._strikes
    assert rec._service.resets == 1


@pytest.mark.asyncio
async def test_a_failed_proactive_refresh_does_not_credit_success():
    """The other half of Fix 3's proactive-refresh coverage: a failed
    scheduled refresh must not erase a strike a real recurring failure still
    needs to be measured against."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    router._strikes["openai"] = 1  # a prior hiccup already charged

    async def failing_reset():
        raise RuntimeError("still broken")

    rec._service.reset_conversation = failing_reset
    now = _time.monotonic()
    rec._connected_at = now - rec.REFRESH_AGE_S - 1
    rec._last_input_audio = now - rec.REFRESH_QUIET_S - 1
    rec._last_attempt = 0.0
    await rec._maybe_proactive_refresh()
    assert router._strikes.get("openai") == 1  # NOT cleared -- the refresh failed


@pytest.mark.asyncio
async def test_a_non_repairing_error_does_not_delay_the_wedge_detector():
    """Minor fix: _last_attempt must be stamped ONLY when a repair is
    actually attempted. A rate limit (reported to the router, never repaired)
    must not touch it -- otherwise it would needlessly delay force_reconnect's
    wedge detector, which gates on this same field, by up to
    RECONNECT_COOLDOWN_S for no reason."""
    switched = []
    router = ProviderRouter("openai", None)
    rec = _recovery("openai", router, switched)
    await rec.handle_error("Rate limit reached for requests")
    assert rec._last_attempt == 0.0


@pytest.mark.asyncio
async def test_the_wedge_repair_stands_back_from_a_self_healing_engine():
    """Live 2026-09-09, three times in ten minutes: every quiet wake on Gemini
    ran the whole wedge repair. `_recover` emits a real idle phase at the
    device FIRST (the chime and the LED, mid-conversation) and only then hits
    `service has no reset_conversation()` and gives up -- while pipecat was
    already reconnecting the session itself. handle_error had the self_heals
    guard from the start; force_reconnect, which is the path the wedge
    detector actually uses, did not."""
    switched = []
    router = ProviderRouter("gemini", "openai", probe=_healthy)
    rec = _recovery("gemini", router, switched)
    idled = []
    rec._emit_idle = lambda value: idled.append(value)
    rec._last_attempt = 0.0

    await rec.force_reconnect("wedge: silent after wake")

    assert rec._service.resets == 0        # no repair attempted
    assert idled == []                     # and no chime at the device
    assert switched == []


@pytest.mark.asyncio
async def test_the_wedge_repair_still_runs_for_an_engine_that_cannot_heal():
    """The guard above must not be a blanket off-switch: OpenAI Realtime has
    no reconnect logic of its own, so its wedge repair is the only thing that
    gets a half-open socket back."""
    switched = []
    router = ProviderRouter("openai", "gemini", probe=_healthy)
    rec = _recovery("openai", router, switched)
    rec._last_attempt = 0.0

    await rec.force_reconnect("wedge: silent after wake")

    assert rec._service.resets == 1


@pytest.mark.asyncio
async def test_the_proactive_refresh_loop_never_starts_for_a_self_healing_engine():
    """The 60-minute cap is OpenAI Realtime's. On Gemini this loop could only
    ever reach the same dead `_recover` path -- idle the device, then fail --
    so it must return before its first sleep instead of polling forever."""
    router = ProviderRouter("gemini", "openai", probe=_healthy)
    rec = _recovery("gemini", router, [])
    rec.REFRESH_CHECK_S = 0

    # Returns rather than looping: without the guard this never completes.
    await asyncio.wait_for(rec._proactive_refresh_loop(), timeout=1.0)
    assert rec._service.resets == 0
