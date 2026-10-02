"""The 2026-10-02 office outage, as tests.

12:10:37 and 12:33:35 OpenAI closed an idle session server-side
("realtime receive loop died: ConnectionClosedError(None, None, None)"). Both
reconnected fine, but each was charged as a strike, so the second one moved
the house to a backup that could not hear for half an hour. A rate limit was
also a strike and ended the turn in silence after the tool had already run.
"""

import pytest

from app.provider_router import ProviderRouter
from app.websocket_handler import ConnectionRecovery

_SERVER_CLOSE = "realtime receive loop died: ConnectionClosedError(None, None, None)"
_RATE_LIMIT = (
    "Rate limit reached for gpt-realtime-2 (for limit gpt-4o-realtime) in organization "
    "org-x on tokens per min (TPM): Limit 40000, Used 26728, Requested 15233. "
    "Please try again in 2.941s. Visit https://platform.openai.com/account/rate-limits"
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeService:
    def __init__(self, reconnects=True):
        self.resets = 0
        self.retries = 0
        self.fallbacks = 0
        self._reconnects = reconnects
        self._websocket = object()

    async def reset_conversation(self):
        self.resets += 1
        if not self._reconnects:
            self._websocket = None  # pipecat's _connect swallows the error like this

    async def retry_response(self):
        self.retries += 1

    async def say_rate_limited(self):
        self.fallbacks += 1


def _recovery(router, service, switched):
    async def on_failover():
        switched.append(True)

    return ConnectionRecovery(service, provider="openai", router=router, on_failover=on_failover)


# --- 1. a server-side close of a healthy session is not a strike -------------


@pytest.mark.asyncio
async def test_two_server_closes_reconnect_the_same_engine_and_never_switch():
    switched = []
    router = ProviderRouter("openai", "gemini")
    rec = _recovery(router, FakeService(), switched)
    await rec.handle_error(_SERVER_CLOSE)
    rec._last_reported_at = rec._last_attempt = 0.0  # 23 minutes later
    await rec.handle_error(_SERVER_CLOSE)
    assert rec._service.resets == 2
    assert switched == []
    assert router.current() == "openai"
    assert not router._strikes.get("openai")


@pytest.mark.asyncio
async def test_a_reconnect_that_does_not_come_back_is_a_strike():
    switched = []
    router = ProviderRouter("openai", None)
    rec = _recovery(router, FakeService(reconnects=False), switched)
    await rec.handle_error(_SERVER_CLOSE)
    assert router._strikes.get("openai") == 1


# --- 2. a rate limit waits, asks again, and is never a strike ---------------


def test_the_wait_is_read_from_the_message_and_capped():
    from app.provider_failures import retry_after_s

    assert retry_after_s(_RATE_LIMIT) == pytest.approx(2.941)
    assert retry_after_s("Please try again in 769ms.") == pytest.approx(0.769)
    assert retry_after_s("Please try again in 20s.") == 5.0


def test_a_rate_limit_is_never_a_strike():
    router = ProviderRouter("openai", "gemini")
    for _ in range(3):
        assert router.report_failure("openai", _RATE_LIMIT) == "openai"
    assert not router._strikes.get("openai")


@pytest.mark.asyncio
async def test_a_rate_limit_retries_the_answer_once_then_says_so(monkeypatch):
    import app.websocket_handler as wh

    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(wh.asyncio, "sleep", fake_sleep)
    switched = []
    router = ProviderRouter("openai", "gemini")
    rec = _recovery(router, FakeService(), switched)

    assert await rec.handle_error(_RATE_LIMIT) is True
    assert waits == [pytest.approx(2.941)]
    assert rec._service.retries == 1
    assert rec._service.fallbacks == 0

    # The retry was rate limited as well: tell the user, do not loop.
    assert await rec.handle_error(_RATE_LIMIT) is True
    assert rec._service.retries == 1
    assert rec._service.fallbacks == 1

    # The fallback itself rate limited: nothing more but the idle nudge.
    assert await rec.handle_error(_RATE_LIMIT) is False
    assert (rec._service.retries, rec._service.fallbacks) == (1, 1)
    assert switched == []
    assert not router._strikes.get("openai")


# --- 3. only a backup known to be healthy is switched to --------------------


def _has_probe():
    import inspect

    return "probe" in inspect.signature(ProviderRouter.__init__).parameters


def test_a_backup_nobody_has_seen_working_is_not_switched_to():
    router = ProviderRouter("openai", "gemini", clock=FakeClock())
    assert router.report_failure("openai", "insufficient_quota") == "openai"


def test_a_backup_that_fails_its_probe_is_not_switched_to():
    assert _has_probe()
    router = ProviderRouter("openai", "gemini", clock=FakeClock(), probe=lambda p: False)
    assert router.report_failure("openai", "insufficient_quota") == "openai"


def test_a_backup_that_passes_its_probe_is_switched_to():
    assert _has_probe()
    asked = []
    router = ProviderRouter("openai", "gemini", clock=FakeClock(),
                            probe=lambda p: asked.append(p) or True)
    assert router.report_failure("openai", "insufficient_quota") == "gemini"
    assert asked == ["gemini"]


def test_a_backup_that_answered_within_the_cooldown_needs_no_probe():
    clock = FakeClock()
    router = ProviderRouter("openai", "gemini", clock=clock, probe=lambda p: False)
    router.note_success("gemini")
    clock.t += 600
    assert router.report_failure("openai", "insufficient_quota") == "gemini"
    # ... but not once that proof is older than the cooldown.
    router2 = ProviderRouter("openai", "gemini", clock=clock, probe=lambda p: False)
    router2.note_success("gemini")
    clock.t += router2.cooldown_s + 1
    assert router2.report_failure("openai", "insufficient_quota") == "openai"


def test_the_add_on_wires_a_real_probe(monkeypatch):
    import app.main as main

    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "gemini")
    monkeypatch.setattr(main, "probe_engine", lambda p: p == "gemini")
    router = main.build_router()
    assert router.report_failure("openai", "insufficient_quota") == "gemini"


@pytest.mark.asyncio
async def test_the_openai_service_retries_and_speaks_out_of_band():
    from types import SimpleNamespace

    from app.providers.openai_realtime import SafeRealtimeLLMService as S

    sent, created = [], []

    async def ws_send(payload):
        sent.append(payload)

    async def create():
        created.append(True)

    fake = SimpleNamespace(_current_assistant_response=None, _websocket=object(),
                           _ws_send=ws_send, _create_response=create,
                           RATE_LIMIT_FALLBACK=S.RATE_LIMIT_FALLBACK)
    await S.retry_response(fake)
    assert created == [True]
    fake._current_assistant_response = object()  # a reply already under way
    await S.retry_response(fake)
    assert created == [True]

    await S.say_rate_limited(fake)
    response = sent[0]["response"]
    assert sent[0]["type"] == "response.create"
    assert response["conversation"] == "none" and response["input"] == []

    fake._websocket = None  # no audio path: the caller falls back to idle
    with pytest.raises(RuntimeError):
        await S.say_rate_limited(fake)
