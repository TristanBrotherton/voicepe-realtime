"""tools/minutkoll.py pairs the journal's connects and closes (raawr US-032 AC-4)."""
import datetime as dt
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "minutkoll", pathlib.Path(__file__).parent.parent / "tools" / "minutkoll.py")
minutkoll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(minutkoll)


def _t(s):
    return dt.datetime(2026, 10, 7, 16, 0, 0) + dt.timedelta(seconds=s)


def test_tva_hogtalare_samtidigt_och_kill_9():
    rader = [
        (_t(0), "app.providers.sovlage - INFO - ☁️ connected to the cloud engine on wake (0.6s) [moln aaa111]"),
        (_t(10), "app.providers.sovlage - INFO - ☁️ connected to the cloud engine on wake (0.5s) [moln bbb222]"),
        (_t(40), "app.providers.sovlage - INFO - 🧾 cloud session closed after 40s [moln aaa111]"),
        (_t(70), "app.providers.sovlage - INFO - ☁️ connected to the cloud engine on wake (0.5s) [moln ccc333]"),
        # kill -9: neither bbb222 nor ccc333 logs a close.
        (_t(100), "systemd[1]: raawr-rostagent.service: Main process exited, code=killed, status=9/KILL"),
        (_t(200), "app.providers.sovlage - INFO - ☁️ connected to the cloud engine on wake (0.6s) [moln ddd444]"),
    ]
    # 40 + (100 - 10) + (100 - 70) + still open (230 - 200)
    assert minutkoll.summera(rader, _t(230)) == 40 + 90 + 30 + 30


def test_en_stangning_utan_uppkoppling_raknas_inte():
    rader = [(_t(5), "🧾 cloud session closed after 5s [moln eee555]")]
    assert minutkoll.summera(rader, _t(10)) == 0


def test_session_som_var_oppen_nar_spannet_borjade_raknas_fran_starten():
    """23:55-00:05: run from midnight, only the 5 min after it count."""
    rader = [(_t(300), "🧾 cloud session closed after 600s [moln fff666]")]
    assert minutkoll.summera(rader, _t(400), fran=_t(0)) == 300
