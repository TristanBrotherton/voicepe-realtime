"""Who runs, who is broken, and when do we try the good one again."""

import pytest

from app.provider_router import ProviderRouter


def _healthy(provider):
    """The backup answers its probe (switch-only-to-healthy, 2026-10-02)."""
    return True



class FakeClock:
    """A clock the test moves by hand, so no test ever sleeps."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


def test_the_primary_runs_when_nothing_is_wrong(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    assert r.current() == "gemini"


def test_out_of_money_switches_at_once(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    assert r.report_failure("gemini", "insufficient_quota") == "openai"
    assert r.current() == "openai"


def test_a_dropped_socket_gets_one_more_try_first(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    assert r.report_failure("gemini", "keepalive ping timeout") == "gemini"
    assert r.current() == "gemini"
    # Second time is the switch.
    assert r.report_failure("gemini", "keepalive ping timeout") == "openai"


def test_our_own_fault_never_switches(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    r.report_failure("gemini", "play_media failed: 500 Internal Server Error")
    r.report_failure("gemini", "play_media failed: 500 Internal Server Error")
    r.report_failure("gemini", "play_media failed: 500 Internal Server Error")
    assert r.current() == "gemini"


def test_the_retry_budget_resets_after_a_good_turn(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    r.report_failure("gemini", "keepalive ping timeout")
    r.note_success("gemini")
    # The earlier hiccup must not count towards the next one.
    assert r.report_failure("gemini", "keepalive ping timeout") == "gemini"


def test_the_primary_is_tried_again_after_the_cooldown(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, cooldown_s=1800.0, clock=clock)
    r.report_failure("gemini", "insufficient_quota")
    assert r.current() == "openai"
    clock.advance(1799)
    assert r.current() == "openai"
    clock.advance(2)
    assert r.current() == "gemini"


def test_failing_again_right_after_the_retry_starts_a_new_cooldown(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, cooldown_s=1800.0, clock=clock)
    r.report_failure("gemini", "insufficient_quota")
    clock.advance(1801)
    assert r.current() == "gemini"
    r.report_failure("gemini", "insufficient_quota")
    assert r.current() == "openai"
    clock.advance(1799)
    assert r.current() == "openai"


def test_with_no_backup_it_stays_put(clock):
    r = ProviderRouter("gemini", None, clock=clock)
    assert r.report_failure("gemini", "insufficient_quota") == "gemini"
    assert r.current() == "gemini"


def test_when_both_are_broken_it_falls_back_to_the_primary(clock):
    # Something has to be tried. Predictable beats clever.
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    r.report_failure("gemini", "insufficient_quota")
    r.report_failure("openai", "insufficient_quota")
    assert r.current() == "gemini"


def test_a_failure_from_an_engine_that_is_not_running_is_ignored(clock):
    # A late error frame from the session we just left must not bounce us back.
    r = ProviderRouter("gemini", "openai", probe=_healthy, clock=clock)
    r.report_failure("gemini", "insufficient_quota")
    assert r.current() == "openai"
    assert r.report_failure("gemini", "insufficient_quota") == "openai"
    assert r.current() == "openai"


def test_status_says_what_a_dashboard_needs(clock):
    r = ProviderRouter("gemini", "openai", probe=_healthy, cooldown_s=1800.0, clock=clock)
    r.report_failure("gemini", "insufficient_quota")
    s = r.status()
    assert s["provider"] == "openai"
    assert "quota" in s["reason"]
    assert s["retry_primary_in_s"] == pytest.approx(1800.0, abs=1)


# --- three engines in order (0.26.2, Henrik 2026-10-04: gemini, xai, openai) ---

def _router3(clock, healthy=("xai", "openai")):
    return ProviderRouter("gemini", "xai", probe=lambda e: e in healthy, clock=clock, extra=["openai"])


def test_tre_motorer_i_ordning(clock):
    r = _router3(clock)
    assert r.chain == ["gemini", "xai", "openai"]
    assert r.report_failure("gemini", "insufficient_quota") == "xai"
    assert r.report_failure("xai", "insufficient_quota") == "openai"


def test_hoppar_over_en_sjuk_mellanniva(clock):
    r = _router3(clock, healthy=("openai",))
    assert r.report_failure("gemini", "insufficient_quota") == "openai"


def test_sista_nivan_faller_tillbaka_till_den_forsta(clock):
    r = _router3(clock)
    r.report_failure("gemini", "insufficient_quota")
    r.report_failure("xai", "insufficient_quota")
    assert r.report_failure("openai", "insufficient_quota") == "gemini"


def test_ingen_frisk_stannar_pa_den_forsta(clock):
    r = _router3(clock, healthy=())
    assert r.report_failure("gemini", "insufficient_quota") == "gemini"


def test_tredje_nivan_provar_forsta_igen_efter_nedkylningen(clock):
    r = _router3(clock)
    r.report_failure("gemini", "insufficient_quota")
    r.report_failure("xai", "insufficient_quota")
    clock.advance(r.cooldown_s)
    assert r.current() == "gemini"


def test_build_router_laser_voice_providers(monkeypatch):
    from app.main import build_router
    monkeypatch.setenv("VOICE_PROVIDERS", "gemini, xai ,openai,bogus,xai")
    monkeypatch.setenv("VOICE_PROVIDER", "openai")  # the list wins
    r = build_router()
    assert r.chain == ["gemini", "xai", "openai"]
    assert (r.primary, r.backup) == ("gemini", "xai")


def test_build_router_utan_lista_som_forut(monkeypatch):
    from app.main import build_router
    monkeypatch.delenv("VOICE_PROVIDERS", raising=False)
    monkeypatch.setenv("VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("VOICE_PROVIDER_BACKUP", "xai")
    assert build_router().chain == ["gemini", "xai"]
