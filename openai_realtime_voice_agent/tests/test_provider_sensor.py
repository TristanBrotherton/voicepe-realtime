"""A silent switch that spends money on the wrong account for three days is
exactly the kind of fault nobody notices. So it gets an entity.

Every assertion here is checked against what a broken or half-written
`SensorPublisher.provider()` would produce -- loose checks like "starts
with sensor.voicepe_" pass even when the entity id, the reason, or the
retry countdown are wrong, so the exact expected values are asserted
instead of just their shape.
"""

import asyncio
import logging

import pytest

from app import ha_sensors
from app.provider_router import ProviderRouter


def _healthy(provider):
    """The backup answers its probe (switch-only-to-healthy, 2026-10-02)."""
    return True

from app.websocket_handler import WebSocketHandler


class FakeClock:
    """A clock the test moves by hand, so no test depends on wall time."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _capture(monkeypatch, succeeds=True):
    """Patch ha_sensors._post and return the list of posts it records.

    The fake returns what the real `_post` returns: whether the write landed
    in Home Assistant. That matters -- the de-dup key is only recorded after
    a successful write -- so `succeeds=False` stands in for a supervisor that
    is briefly unreachable (an HA restart, say).
    """
    posts = []

    async def fake_post(entity, state, attrs):
        posts.append({"entity": entity, "state": state, "attrs": attrs})
        return succeeds

    monkeypatch.setattr(ha_sensors, "_post", fake_post)
    return posts


@pytest.mark.asyncio
async def test_the_sensor_says_which_engine_and_why(monkeypatch):
    posts = _capture(monkeypatch)

    clock = FakeClock()
    router = ProviderRouter("gemini", "openai", probe=_healthy, cooldown_s=1800.0, clock=clock)
    await router.report_failure("gemini", "insufficient_quota")
    await ha_sensors.SensorPublisher().provider(router.status())

    assert len(posts) == 1
    posted = posts[0]
    # Exact entity id, not just a prefix/suffix match -- a sensor published
    # under the wrong instance name would still satisfy startswith/endswith.
    assert posted["entity"] == f"sensor.voicepe_{ha_sensors._INST}_motor"
    assert posted["state"] == "openai"
    assert "quota" in posted["attrs"]["reason"]
    assert posted["attrs"]["primary"] == "gemini"
    assert posted["attrs"]["backup"] == "openai"
    # No time has passed on the fake clock, so the full cooldown is still
    # owed -- an exact figure, not just "some positive number" (a hardcoded
    # positive constant would also satisfy > 0).
    assert posted["attrs"]["retry_primary_in_s"] == 1800.0


@pytest.mark.asyncio
async def test_a_healthy_house_reports_no_reason(monkeypatch):
    posts = _capture(monkeypatch)

    await ha_sensors.SensorPublisher().provider(ProviderRouter("openai", "gemini", probe=_healthy).status())

    assert len(posts) == 1
    posted = posts[0]
    assert posted["state"] == "openai"
    assert posted["attrs"]["reason"] == ""
    assert posted["attrs"]["retry_primary_in_s"] == 0.0


@pytest.mark.asyncio
async def test_repeat_publish_with_nothing_changed_is_skipped(monkeypatch):
    """Session builds happen on every device (re)connect, which is often.

    Publishing to Home Assistant on every one of those, when the engine
    hasn't actually changed, would spam the state machine for no reason.
    The publisher should write once and then stay quiet until something
    about the engine/reason/switch actually moves.
    """
    posts = _capture(monkeypatch)

    clock = FakeClock()
    router = ProviderRouter("openai", "gemini", probe=_healthy, cooldown_s=1800.0, clock=clock)
    publisher = ha_sensors.SensorPublisher()

    await publisher.provider(router.status())
    await publisher.provider(router.status())
    await publisher.provider(router.status())

    assert len(posts) == 1


@pytest.mark.asyncio
async def test_a_real_switch_is_never_swallowed_by_the_dedup(monkeypatch):
    """The de-dup must not eat an actual engine change."""
    posts = _capture(monkeypatch)

    clock = FakeClock()
    router = ProviderRouter("gemini", "openai", probe=_healthy, cooldown_s=1800.0, clock=clock)
    publisher = ha_sensors.SensorPublisher()

    await publisher.provider(router.status())  # healthy: gemini
    await router.report_failure("gemini", "insufficient_quota")
    await publisher.provider(router.status())  # switched: openai

    assert len(posts) == 2
    assert posts[0]["state"] == "gemini"
    assert posts[1]["state"] == "openai"
    assert posts[1]["attrs"]["reason"] != ""


@pytest.mark.asyncio
async def test_a_switch_lost_to_an_unreachable_supervisor_is_published_next_time(monkeypatch):
    """Final review, Fix 4: the de-dup key must only be recorded once the
    write actually landed.

    `_post` swallows every failure at debug level, so a switch published
    while the supervisor is briefly unreachable -- an HA restart, which is a
    common reason the device reconnected at all -- vanishes. If the key were
    stored before the await, that lost write would be deduped away on every
    later connect and the sensor would show the OLD engine for the whole
    cooldown. Mutation check: record the key before the POST and the second
    publish here never happens (len(posts) == 1) and the engine stays wrong.
    """
    posts = _capture(monkeypatch, succeeds=False)

    clock = FakeClock()
    router = ProviderRouter("gemini", "openai", probe=_healthy, cooldown_s=1800.0, clock=clock)
    await router.report_failure("gemini", "insufficient_quota")
    publisher = ha_sensors.SensorPublisher()

    await publisher.provider(router.status())  # HA is down: attempted, lost
    assert len(posts) == 1

    # The device reconnects a moment later; HA is back.
    posts_ok = _capture(monkeypatch, succeeds=True)
    await publisher.provider(router.status())

    assert len(posts_ok) == 1, (
        "the switch that was lost to the failed write was deduped away -- "
        "the sensor keeps reporting the old engine"
    )
    assert posts_ok[0]["state"] == "openai"

    # And once it HAS landed, the de-dup takes over again as before.
    await publisher.provider(router.status())
    assert len(posts_ok) == 1


# --- Fix 1: the publish must never delay the connection it's reporting on ---
#
# serve_connection dispatches the provider-sensor publish as a background
# task rather than awaiting it, precisely so a slow or unreachable HA
# supervisor cannot hold up building the session. These run the REAL
# WebSocketHandler.serve_connection, with only the transport/pipeline/service
# stubbed out, so a regression back to `await self._publish_provider_status(...)`
# on the connection setup path would make these hang and fail on the
# wait_for timeout below instead of passing by coincidence.


class _FakeURL:
    query = "device_id=kitchen"


class _FakeWebSocket:
    url = _FakeURL()
    client = None

    async def accept(self):
        return None

    async def send_text(self, _payload):
        return None


class _NullTransport:
    def event_handler(self, _name):
        def register(fn):
            return fn
        return register


def _fake_build_pipeline(_connection, activity_callback=None):
    class _Runner:
        async def run(self, _task):
            return None

    class _Task:
        async def cancel(self):
            return None

    return object(), _Runner(), _Task()


def _make_handler(router):
    handler = WebSocketHandler()
    handler.router = router
    handler.create_transport = lambda _ws, _ser, _provider: _NullTransport()
    handler.build_pipeline = _fake_build_pipeline

    async def factory(_connection):
        return object()

    handler.openai_service_factory = factory
    return handler


@pytest.mark.asyncio
async def test_a_slow_publish_does_not_delay_the_connection(monkeypatch):
    """The brief prescribed awaiting the publish; that was wrong. If
    serve_connection awaited it, a supervisor that never answers would hang
    the whole connection for up to httpx's 8s timeout before the session is
    even built -- exactly what the module's own comment forbids."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_provider(_status):
        started.set()
        await release.wait()  # never set during this test

    monkeypatch.setattr(ha_sensors.PUBLISHER, "provider", slow_provider)

    handler = _make_handler(ProviderRouter("openai", "gemini", probe=_healthy))

    # If the publish were awaited on the setup path, this would still be
    # blocked on release.wait() and the wait_for below would time out.
    await asyncio.wait_for(handler.serve_connection(_FakeWebSocket()), timeout=1.0)

    # And the publish really was dispatched, not silently skipped. It is a
    # background task: serve_connection's fake setup path never suspends, so
    # the task has not had a turn yet when it returns -- give it one.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    release.set()  # let the background task finish so nothing lingers


@pytest.mark.asyncio
async def test_a_failing_publish_is_logged_not_lost(caplog, monkeypatch):
    """Dispatching the publish as a background task must not make a real
    failure vanish into asyncio's own "Task exception was never retrieved"
    warning -- _publish_provider_status catches it and logs it itself, so the
    conversation proceeds AND the failure is still visible."""
    async def boom(_status):
        raise RuntimeError("HA supervisor unreachable")

    monkeypatch.setattr(ha_sensors.PUBLISHER, "provider", boom)

    handler = _make_handler(ProviderRouter("openai", "gemini", probe=_healthy))

    with caplog.at_level(logging.DEBUG, logger="app.websocket_handler"):
        await handler.serve_connection(_FakeWebSocket())
        # Let the background task actually run to completion.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert any("provider sensor failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_the_task_reference_is_held_then_dropped_when_done(monkeypatch):
    """asyncio only keeps a WEAK reference to a running task -- something
    must hold a strong one for its lifetime, or it can be garbage-collected
    mid-publish. This checks both halves: the reference exists (and is a
    real, not-yet-finished Task -- not discarded, not a coincidental None)
    while the publish is outstanding, and disappears once the publish is
    actually done, so a connection that stays open for hours doesn't keep
    accumulating finished Task objects."""
    release = asyncio.Event()
    calls = []

    async def slow_provider(_status):
        calls.append("started")
        await release.wait()
        calls.append("finished")

    monkeypatch.setattr(ha_sensors.PUBLISHER, "provider", slow_provider)

    handler = _make_handler(ProviderRouter("openai", "gemini", probe=_healthy))
    seen = {}

    async def factory(connection):
        seen["connection"] = connection
        return object()

    handler.openai_service_factory = factory

    # serve_connection's own body never truly suspends in this stubbed
    # setup, so plainly awaiting it (no wait_for/create_task wrapper) lets
    # it run to completion without ever handing control back to the loop --
    # the sibling task it creates is scheduled, but gets no turn to run
    # during this call. That is exactly the moment a bare
    # `asyncio.create_task(...)` with no assignment would be eligible for
    # garbage collection: nothing but our own attribute has a strong
    # reference to it here.
    await handler.serve_connection(_FakeWebSocket())

    connection = seen["connection"]
    task = connection.provider_status_task
    assert isinstance(task, asyncio.Task)
    assert not task.done()
    assert calls == []  # confirms it truly hasn't run yet, not just "held"

    release.set()
    await task  # give it its turn; it runs straight through once resumed

    assert calls == ["started", "finished"]
    # Dropped: once the task is actually done, the connection no longer
    # references it -- it does not accumulate forever.
    assert connection.provider_status_task is None
