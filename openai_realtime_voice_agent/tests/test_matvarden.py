"""The AC-10 measuring command (raawr US-032)."""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "matvarden", pathlib.Path(__file__).parent.parent / "tools" / "matvarden.py")
matvarden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(matvarden)


def rad(tid, text, mod="app.session_state"):
    return f"Oct 08 x env[1]: 2026-10-08 {tid} - {mod} - INFO - {text}"


LOGG = [
    rad("10:00:00,000", "🧭 kontoret: IDLE -> WAKE (wake: device wake)"),
    rad("10:00:01,000", "🧭 kontoret: WAKE -> LISTENING (listening: phase)"),
    rad("10:00:03,000", "🧭 kontoret: LISTENING -> THINKING (thinking: phase)"),
    rad("10:00:03,300", "bana0: hit 'tänd lampan hemligt ord' -> 'Tände'", "app.bana0"),
    rad("10:00:04,000", "🧭 kontoret: THINKING -> SPEAKING (replying: phase)"),
    rad("10:00:06,000", "🧭 kontoret: SPEAKING -> LISTENING (listening: phase)"),  # an interruption
    rad("10:00:08,000", "🧭 kontoret: LISTENING -> THINKING (thinking: phase)"),
    rad("10:00:11,000", "🧭 kontoret: THINKING -> SPEAKING (replying: phase)"),
    rad("10:00:20,000", "🧭 kontoret: SPEAKING -> CLOSING (close: quiet for 30s)"),
    rad("10:01:00,000", "🧭 kontoret: CLOSING -> IDLE (closed: quiet for 30s)"),
    rad("10:05:00,000", "🧭 koket: IDLE -> WAKE (wake: device wake)"),
    rad("10:05:05,000", "🧭 koket: WAKE -> CLOSING (close: wake without speech for 5s)"),
    rad("10:05:06,000", "🧭 koket: CLOSING -> IDLE (closed: wake without speech for 5s)"),
    rad("10:05:07,000", "bana0: heard 'hemligt ord'", "app.bana0"),
]


def test_per_rum_siffror_stammer_med_journalen():
    ut = matvarden.mat(LOGG)
    k = ut["kontoret"]
    assert k["sessioner"] == 1 and k["uppkopplade_minuter"] == 1.0
    assert k["avbrott"] == 1
    assert k["stangningsorsaker"] == {"quiet for 30s": 1}
    assert k["forsta_ljud_s"] == {"p50": 1.0, "p95": 3.0, "n": 2}  # 1 s and 3 s
    assert k["reflex_s"] == {"p50": 0.3, "p95": 0.3, "n": 1}
    q = ut["koket"]
    assert q["sessioner"] == 1 and q["stangningsorsaker"] == {"wake without speech for 5s": 1}
    assert q["forsta_ljud_s"]["n"] == 0 and q["forsta_ljud_s"]["p95"] is None


def test_forsta_ljud_en_gang_per_tankande():
    # Journal 2026-10-10 (attrapp 08:23): THINKING -> SPEAKING, then LISTENING -> SPEAKING again with no new THINKING.
    # The second was counted from the old THINKING, so P95 read 2.6 s where the first sounds said 1.7. A VAD flap
    # (THINKING -> LISTENING -> SPEAKING, koket 2026-10-09 10:07) is still a first sound.
    logg = [
        rad("10:00:00,000", "🧭 kontoret: LISTENING -> THINKING (thinking: phase)"),
        rad("10:00:01,000", "🧭 kontoret: THINKING -> SPEAKING (replying: phase)"),
        rad("10:00:02,000", "🧭 kontoret: SPEAKING -> LISTENING (listening: phase)"),
        rad("10:00:03,000", "🧭 kontoret: LISTENING -> SPEAKING (replying: phase)"),
        rad("10:00:10,000", "🧭 koket: LISTENING -> THINKING (thinking: phase)"),
        rad("10:00:11,000", "🧭 koket: THINKING -> LISTENING (listening: phase)"),
        rad("10:00:12,000", "🧭 koket: LISTENING -> SPEAKING (replying: phase)"),
    ]
    ut = matvarden.mat(logg)
    assert ut["kontoret"]["forsta_ljud_s"] == {"p50": 1.0, "p95": 1.0, "n": 1}
    assert ut["koket"]["forsta_ljud_s"] == {"p50": 2.0, "p95": 2.0, "n": 1}


def test_inget_av_det_som_sades_hamnar_i_utskriften():
    import json
    assert "hemligt" not in json.dumps(matvarden.mat(LOGG), ensure_ascii=False)
    assert "tänd lampan" not in json.dumps(matvarden.mat(LOGG), ensure_ascii=False)


def test_percentiler_med_narmaste_rang():
    assert matvarden.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert matvarden.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert matvarden.pct([], 50) is None


def test_tider_raderna_ger_p50_p95_efter_talets_slut_och_verktygsturer_for_sig():
    logg = [
        rad("10:00:00,000", "⏱ tider kontoret turslut=800 stt=990 enhet=1500", "app.turn_tider"),
        rad("10:00:10,000", "⏱ tider kontoret turslut=800 stt=990 enhet=2500", "app.turn_tider"),
        rad("10:00:20,000", "⏱ tider kontoret turslut=800 verktyg=1300 enhet=5000", "app.turn_tider"),
        rad("10:00:30,000", "⏱ tider kontoret turslut=800 utan_ljud", "app.turn_tider"),
    ]
    e = matvarden.mat(logg)["kontoret"]["efter_talets_slut_s"]
    assert e["n"] == 2 and e["p50"] == 1.5 and e["p95"] == 2.5
    assert e["med_verktyg_n"] == 1 and e["med_verktyg_p50"] == 5.0


def _tank(tid, rum):
    return rad(tid, f"🧭 {rum}: LISTENING -> THINKING (thinking: phase)")


def _traff(tid, text="zebralampa", svar="klart"):
    return rad(tid, f"bana0: hit {text!r} -> {svar!r}", "app.bana0")


def _tider(tid, rum, resten):
    return rad(tid, f"⏱ tider {rum} {resten}", "app.turn_tider")


def test_tva_personliga_traffar_och_en_kort_ger_n_och_p50():
    logg = [
        _tank("12:00:00,000", "kontoret"),
        _traff("12:00:00,200"),
        _tider("12:00:02,000", "kontoret", "turslut=800 stt=802 comms=1058 modell=1000 enhet=2892"),
        _tank("12:00:10,000", "kontoret"),
        _traff("12:00:10,200"),
        _tider("12:00:12,000", "kontoret", "turslut=800 stt=802 comms=1058 modell=3000 enhet=4000"),
        _tank("12:00:20,000", "kontoret"),
        _traff("12:00:20,200"),
        _tider("12:00:21,000", "kontoret", "turslut=800 stt=802 comms=1058 enhet=1050"),
    ]
    import json
    k = matvarden.mat(logg)["kontoret"]
    assert k["traff_personligt"]["n"] == 2 and k["traff_personligt"]["p50"] == 1000  # nearest rank of 1000, 3000
    assert k["traff_kort"]["n"] == 1 and k["traff_kort"]["p50"] == 1050
    assert "zebralampa" not in json.dumps(k, ensure_ascii=False)


def test_tur_utan_traff_med_modell_raknas_inte():
    logg = [
        _tank("12:00:00,000", "kontoret"),
        _tider("12:00:02,000", "kontoret", "turslut=800 stt=802 comms=1058 modell=2889 enhet=2892"),
    ]
    k = matvarden.mat(logg)["kontoret"]
    assert k["traff_personligt"]["n"] == 0 and k["traff_kort"]["n"] == 0
    assert k["efter_talets_slut_s"]["n"] == 1 and k["efter_talets_slut_s"]["p50"] == 2.892


def test_traff_for_ett_rum_raknas_inte_till_ett_annat():
    logg = [
        _tank("12:00:00,000", "kontoret"),
        _traff("12:00:00,200"),
        _tank("12:00:01,000", "koket"),
        _tider("12:00:02,000", "koket", "turslut=800 modell=1111 enhet=1222"),
        _tider("12:00:03,000", "kontoret", "turslut=800 modell=2889 enhet=3000"),
    ]
    ut = matvarden.mat(logg)
    assert ut["kontoret"]["traff_personligt"] == {"n": 1, "p50": 2889, "p95": 2889}
    assert ut["koket"]["traff_personligt"]["n"] == 0 and ut["koket"]["traff_kort"]["n"] == 0
    assert ut["koket"]["efter_talets_slut_s"]["n"] == 1  # the turn still counts, just not as a hit


def test_json_har_traff_personligt_och_traff_kort():
    import json
    logg = [
        _tank("12:00:00,000", "kontoret"),
        _traff("12:00:00,200", text="hemligt ord"),
        _tider("12:00:02,000", "kontoret", "turslut=800 modell=1000 enhet=1100"),
    ]
    ut = json.loads(json.dumps(matvarden.mat(logg), ensure_ascii=False))
    assert set(ut["kontoret"]["traff_personligt"]) == {"n", "p50", "p95"}
    assert set(ut["kontoret"]["traff_kort"]) == {"n", "p50", "p95"}
    assert ut["kontoret"]["traff_personligt"]["n"] == 1
    assert "hemligt" not in json.dumps(ut, ensure_ascii=False)
