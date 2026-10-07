"""Switching engines means tearing the pipeline down and letting the device
call back. The device reconnects in about a second -- the same path every
add-on update already uses -- and the session is rebuilt with the other
engine, with the conversation restored from cache."""

import logging

import pytest

from app.websocket_handler import make_failover


class FakeTask:
    """`events` is the SAME list the owning FakeConnection appends to, so a
    test can see the relative order the two fakes were called in, not just
    each one's own final count."""

    def __init__(self, events):
        self.cancelled = 0
        self._events = events

    async def cancel(self):
        self.cancelled += 1
        self._events.append("cancel")


class FakeConnection:
    def __init__(self):
        self.events = []  # ordered log shared with self.task, see FakeTask
        self.task = FakeTask(self.events)
        self.device_id = "10.30.0.81"
        self.phases = []
        from app.session_state import SessionMaskin
        self.maskin = SessionMaskin(self.device_id, self.send_phase)

    async def send_phase(self, value):
        self.phases.append(value)
        self.events.append(f"phase:{value}")
        return True


@pytest.mark.asyncio
async def test_the_device_is_unstuck_before_the_pipeline_is_torn_down():
    # Otherwise the LED keeps blinking through the gap and the user thinks it
    # is still listening. Checking the two final counts alone (phases==
    # ["idle"], cancelled==1) cannot fail if the two steps run in the WRONG
    # order -- both would still happen exactly once. `events` is one list
    # both fakes append to, so the ORDER is what's asserted, not just that
    # both occurred.
    c = FakeConnection()
    await make_failover(c)()
    assert c.phases == ["idle"]
    assert c.task.cancelled == 1
    assert c.events == ["phase:idle", "cancel"]


@pytest.mark.asyncio
async def test_a_second_call_does_nothing():
    # Several error frames arrive for the same death. One teardown is enough.
    # Checking task.cancelled alone would miss a guard that only wraps the
    # cancel but forgets the phase emit (or vice versa), so both are pinned
    # via the same shared `events` log used above.
    c = FakeConnection()
    failover = make_failover(c)
    await failover()
    await failover()
    assert c.task.cancelled == 1
    assert c.events == ["phase:idle", "cancel"]


@pytest.mark.asyncio
async def test_a_connection_with_no_task_does_not_explode(caplog):
    # "Must not raise" alone proves the surrounding try/except exists, not
    # that the `task is None` guard does: task.cancel() on a bare `None`
    # raises AttributeError, and a plain `try: await task.cancel() except
    # Exception: log.error(...)` around the call swallows that exception
    # just as quietly, no guard required -- confirmed by mutation, see the
    # report. So this asserts what only the GUARD produces (its own "no
    # pipeline task" warning) and what only a swallowed exception would have
    # produced instead (the except-block's "failover teardown failed"
    # message), and requires the latter to be ABSENT -- i.e. nothing was
    # thrown and caught behind the scenes.
    c = FakeConnection()
    c.task = None
    with caplog.at_level(logging.WARNING, logger="app.websocket_handler"):
        await make_failover(c)()  # must not raise
    assert "no pipeline task to tear down" in caplog.text
    assert "failover teardown failed" not in caplog.text
