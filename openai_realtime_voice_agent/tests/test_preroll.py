"""D-97: the first word of a turn must survive. Speech that began 1.2 s before the turn was called is kept."""
from app.providers.local_turns import LocalTurnsMixin
from app.raw_audio_serializer import TURN_PCM_PREROLL, RawAudioSerializer


def test_pre_rolls_are_longer_than_before_and_the_ring_is_the_longer():
    assert LocalTurnsMixin.PREROLL_S >= 1.0
    assert TURN_PCM_PREROLL >= 16000 * 2 * 3 // 2
    assert TURN_PCM_PREROLL > int(LocalTurnsMixin.PREROLL_S * 16000) * 2  # bana 0 holds more than the engine


def test_the_engine_keeps_a_second_of_audio_before_the_start_event():
    class Frame:
        def __init__(self, n):
            self.audio, self.sample_rate = bytes([n % 250]) * 3200, 16000  # 0.1 s

    class Eng(LocalTurnsMixin):
        pass
    e = Eng()
    for n in range(30):  # 3 s of mic audio, nothing said yet
        e._keep_preroll(Frame(n))
    kept = e._take_preroll()
    assert len(kept) == int(LocalTurnsMixin.PREROLL_S * 16000) * 2
    assert kept[:3200] == bytes([20]) * 3200  # frame 20 = 1.0 s back from the end, the oldest one kept


def test_a_misspelt_env_value_does_not_stop_the_agent_starting(monkeypatch):
    import importlib

    import app.providers.local_turns as lt
    import app.raw_audio_serializer as rs
    monkeypatch.setenv("ENGINE_PREROLL_MS", "1s")
    monkeypatch.setenv("TURN_PREROLL_MS", "lång")
    try:
        importlib.reload(lt)
        importlib.reload(rs)
        assert lt.LocalTurnsMixin.PREROLL_S == 1.0 and rs.TURN_PCM_PREROLL == 16000 * 2 * 1500 // 1000
    finally:
        monkeypatch.undo()
        importlib.reload(lt)
        importlib.reload(rs)
