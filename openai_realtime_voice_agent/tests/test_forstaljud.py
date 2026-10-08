"""tools/forstaljud.py splits the wait with journal lines (raawr US-032)."""
import datetime as dt
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "forstaljud", pathlib.Path(__file__).parent.parent / "tools" / "forstaljud.py")
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)

BAS = dt.datetime(2026, 10, 7, 23, 8, 0)


def _r(sek, text):
    return f"{(BAS + dt.timedelta(seconds=sek)).isoformat(timespec='milliseconds')}+0000 core env[1]: {text}"


def test_dela_en_tur_med_utfyllnad():
    rader = [
        _r(48.211, "👋 device wake received"),
        _r(52.405, "📞 phase -> thinking"),
        _r(52.742, "HTTP Request: POST http://x/conversation/process"),
        _r(54.826, "⏱ early ack: Ett ögonblick."),
        _r(54.924, "⏱ tool script__delegera 270 ok"),
        _r(55.931, "📞 phase -> replying"),
    ]
    d = fg.dela({"fragan_slut": 2.946, "forsta_svarsljud": 6.619}, rader)
    assert d == {"totalt": 3.67, "svar": 4.77, "tystnad": 1.25, "lokal": 0.34, "motor": 2.09,
                 "verktyg": 0.0, "uppspelning": None, "filler": True}


def test_dela_ren_fraga_ger_uppspelning_och_raknar_verktyg_fore_ljudet():
    rader = [
        _r(56.724, "👋 device wake received"),
        _r(60.114, "📞 phase -> thinking"),
        _r(60.699, "HTTP Request: POST http://x/conversation/process"),
        _r(61.100, "⏱ tool homeassistant__GetLiveContext 432 ok"),
        _r(62.620, "📞 phase -> replying"),
        _r(63.0, "⏱ tool search_home 206 ok"),
    ]
    d = fg.dela({"fragan_slut": 2.187, "forsta_svarsljud": 5.896}, rader)
    assert d["filler"] is False and d["uppspelning"] == 0.0 and d["tystnad"] == 1.2
    assert d["verktyg"] == 0.43  # only the tool that finished before the first sound
