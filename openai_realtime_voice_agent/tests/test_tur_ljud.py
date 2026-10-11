"""D-97: per-turn audio is kept only when the flag is on, and never more than the cap."""
import os
import wave

from app import tur_ljud

PCM = b"\x00\x10" * 16000  # 1 s


def _env(monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("TUR_LJUD_KATALOG", str(tmp_path))
    monkeypatch.setattr(tur_ljud, "_full_sagd", False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_off_by_default_saves_nothing(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.delenv("TUR_LJUD_SPARA", raising=False)
    assert tur_ljud.spara(PCM, "kontoret") is None
    assert os.listdir(tmp_path) == []


def test_on_saves_a_readable_16k_wav(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, TUR_LJUD_SPARA="1")
    p = tur_ljud.spara(PCM, "kontoret")
    with wave.open(p) as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 16000)


def test_cap_stops_saving(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, TUR_LJUD_SPARA="1", TUR_LJUD_MAX="2")
    got = [tur_ljud.spara(PCM, "kontoret") for _ in range(4)]
    assert [g is not None for g in got] == [True, True, False, False]
    assert len(os.listdir(tmp_path)) == 2


def test_unwritable_folder_does_not_raise(monkeypatch, tmp_path):
    f = tmp_path / "fil"
    f.write_text("x")
    _env(monkeypatch, f, TUR_LJUD_SPARA="1")  # a file where the folder should be
    assert tur_ljud.spara(PCM, "kontoret") is None
