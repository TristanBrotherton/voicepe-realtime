"""GEMINI_EGEN_TUR=1 (raawr US-060): the engine's own activity detection runs the turns; ours is the gate and the guard."""
import pytest
from pipecat.frames.frames import InterruptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame

from app.providers import build_service
from test_gemini_provider import OPENAI_SHAPE, _ScriptedTurns, _frame, _options, _wire


def _bygg(monkeypatch, events):
    monkeypatch.setenv("GEMINI_EGEN_TUR", "1")
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    sent = _wire(service, None)
    service._fas_vad = _ScriptedTurns(events)
    service.sover = False
    pushed = []

    async def push(frame, direction=None):
        pushed.append(frame)
    service.push_frame = push
    return service, sent, pushed


def test_the_engine_gets_its_own_activity_detection(monkeypatch):
    monkeypatch.setenv("GEMINI_EGEN_TUR", "1")
    service = build_service("gemini", _options(), OPENAI_SHAPE)
    assert service._turns is None and service._settings["vad"].disabled is not True
    monkeypatch.setenv("GEMINI_EGEN_TUR", "0")
    assert build_service("gemini", _options(), OPENAI_SHAPE)._settings["vad"].disabled is True
    monkeypatch.setenv("GEMINI_EGEN_TUR", "kontoret")   # one room: the others keep local turns and bana 0
    o = _options(); o.rum = "kontoret"
    assert build_service("gemini", o, OPENAI_SHAPE)._turns is None
    o = _options(); o.rum = "koket"
    assert build_service("gemini", o, OPENAI_SHAPE)._turns is not None


@pytest.mark.asyncio
async def test_every_frame_is_streamed_with_no_activity_markers(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, [None, "start", None, "end", None])
    for b in range(5):
        await service._send_user_audio(_frame(b + 1))
    assert len(sent) == 5 and all("activity_start" not in kw and "activity_end" not in kw for kw in sent)
    assert [type(f) for f in pushed] == [UserStartedSpeakingFrame, UserStoppedSpeakingFrame]  # only for the device's phases


@pytest.mark.asyncio
async def test_nothing_is_sent_when_nobody_woke_him(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start", None])
    service.slapp_ljud = lambda: False
    await service._send_user_audio(_frame(1))
    await service._send_user_audio(_frame(2))
    assert sent == [] and pushed == []


@pytest.mark.asyncio
async def test_over_his_reply_only_the_engine_says_that_somebody_spoke(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, ["start", "end"])
    service.svarar = lambda: True
    await service._send_user_audio(_frame(1))
    await service._send_user_audio(_frame(2))
    assert len(sent) == 2 and pushed == []   # the engine hears it all; the local detector cuts nothing
    avbrott = []
    service.on_motor_avbrott = lambda: avbrott.append(1)
    await service._motor_avbrott()
    assert avbrott == [1]
    assert [type(f) for f in pushed if isinstance(f, (InterruptionFrame, UserStartedSpeakingFrame))] == [InterruptionFrame, UserStartedSpeakingFrame]


@pytest.mark.asyncio
async def test_interrupted_from_the_engine_reaches_the_handler(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, [])
    hort = []

    async def avbrott():
        hort.append(1)
    service._motor_avbrott = avbrott

    class M:
        def __init__(self, i):
            self.server_content = type("S", (), {"interrupted": i})()

    class Session:
        def receive(self):
            async def g():
                for m in (M(None), M(True), M(False)):
                    yield m
            return g()

    async def ingen(self, session):   # pipecat's own part needs a live socket
        return None
    monkeypatch.setattr("pipecat.services.google.gemini_live.llm.GeminiLiveLLMService._handle_session_ready", ingen)
    s = Session()
    await service._handle_session_ready(s)
    assert len([m async for m in s.receive()]) == 3 and hort == [1]


@pytest.mark.asyncio
async def test_a_sleeping_engine_is_woken_by_speech_and_gets_the_start_of_it(monkeypatch):
    service, sent, pushed = _bygg(monkeypatch, [None, "start", None])
    service.sover = True

    async def vakna():
        service.sover = False
        return True
    service.vakna = vakna
    for b in range(3):
        await service._send_user_audio(_frame(b + 1))
    assert not service.sover and len(sent) == 2
    assert len(sent[0]["audio"].data) == 2 * 640   # the frame before the speech and the first of it, in one piece


# --- B's review of #74 (2026-10-13): the mutations that survived, and F1/F2 ---

@pytest.mark.asyncio
async def test_cut_by_the_engine_on_a_sound_that_is_no_speech_the_device_does_not_stay_listening(monkeypatch):
    """F1: the engine says "interrupted" on a thump; the local detector never says start or end. The device was told
    "listening" and must be told that it is over."""
    from app.providers import gemini_live
    service, sent, pushed = _bygg(monkeypatch, [None, None, None])
    service.svarar = lambda: True
    await service._motor_avbrott()
    assert [type(f) for f in pushed].count(UserStoppedSpeakingFrame) == 0
    await service._send_user_audio(_frame(1))
    assert [type(f) for f in pushed].count(UserStoppedSpeakingFrame) == 0      # not at once: speech may still come
    monkeypatch.setattr(gemini_live, "AVBROTT_UTAN_TAL_S", 0.0)
    await service._send_user_audio(_frame(2))
    await service._send_user_audio(_frame(3))
    assert [type(f) for f in pushed].count(UserStoppedSpeakingFrame) == 1      # once, not per frame


@pytest.mark.asyncio
async def test_cut_by_the_engine_on_real_speech_ends_when_the_speech_does(monkeypatch):
    from app.providers import gemini_live
    monkeypatch.setattr(gemini_live, "AVBROTT_UTAN_TAL_S", 0.0)
    service, sent, pushed = _bygg(monkeypatch, ["start", None, "end", None])
    service.svarar = lambda: True
    await service._motor_avbrott()
    await service._send_user_audio(_frame(1))     # the local detector hears it too
    await service._send_user_audio(_frame(2))
    assert UserStoppedSpeakingFrame not in [type(f) for f in pushed]           # still speaking: the timer is off
    await service._send_user_audio(_frame(3))
    assert [type(f) for f in pushed].count(UserStoppedSpeakingFrame) == 1


class _Fas:
    phase = "listening"
    idle_since = 0.0


class _Ser:
    def __init__(self, sedan=1e9):
        self._s = sedan

    def sedan_svar(self):
        return self._s


class _Tjanst:
    def __init__(self, text="", hans="Vilken dörr vill du låsa? Är det hallen du vill att jag ska titta på?"):
        self._text = text
        self.svar = type("S", (), {"text": lambda s: hans})()

    def tur_text(self):
        return self._text


def _koppla(tjanst=None, vakning_for_s=5.0, idle_for_s=5.0, sedan_svar=1e9):
    import time
    from app.websocket_handler import koppla_egen_tur
    tjanst = tjanst or _Tjanst()
    fas = _Fas()
    fas.idle_since = time.monotonic() - idle_for_s
    conn = type("C", (), {})()
    conn.recovery = type("R", (), {"_last_wake": time.monotonic() - vakning_for_s})()
    conn.bana0_heard = "tänd lampan i köket"     # bana 0 does not run here: this must NOT be what the guard reads
    koppla_egen_tur(tjanst, conn, fas, _Ser(sedan_svar))
    return tjanst, conn, fas


def test_the_room_is_sent_only_while_somebody_woke_him_or_he_just_spoke():
    assert _koppla(vakning_for_s=5, idle_for_s=5)[0].slapp_ljud() is True
    assert _koppla(vakning_for_s=500, idle_for_s=500)[0].slapp_ljud() is False      # nobody woke him: the room
    assert _koppla(vakning_for_s=500, idle_for_s=500, sedan_svar=1.0)[0].slapp_ljud() is True   # he is replying now
    assert _koppla(vakning_for_s=3, idle_for_s=500)[0].slapp_ljud() is True         # just woken


@pytest.mark.asyncio
async def test_the_tool_guard_knows_the_engine_cut_him_and_waits_for_the_engines_own_text(monkeypatch):
    """F2: a real order that cuts him off, whose transcript comes a moment after the tool call, is not refused."""
    import asyncio
    from app import websocket_handler as wh
    monkeypatch.setattr(wh, "TUR_TEXT_VANTA_S", 0.5)
    tjanst, conn, fas = _koppla(_Tjanst(text=""))
    assert await tjanst.verktygsvakt() is None            # no interruption: nothing to guard
    tjanst.on_motor_avbrott()
    assert conn.barge_tur is True and conn.barge_in_t > 0

    async def sen_text():
        await asyncio.sleep(0.15)
        tjanst._text = "släck lampan i köket tack"
    t = asyncio.create_task(sen_text())
    assert await tjanst.verktygsvakt() is None            # the text came in time: a person, the tool runs
    await t
    # No text at all: unknown speech right after his own voice does not open a door (and bana0_heard is not read).
    tjanst2, conn2, _ = _koppla(_Tjanst(text=""))
    tjanst2.on_motor_avbrott()
    assert "unclear speech" in (await tjanst2.verktygsvakt() or "")
    # His own words back: refused, at once.
    tjanst3, _, _ = _koppla(_Tjanst(text="Vilken dörr vill du låsa?"))
    tjanst3.on_motor_avbrott()
    assert "your own last reply" in (await tjanst3.verktygsvakt() or "")


def test_the_handler_calls_the_wiring_for_a_room_where_the_engine_runs_the_turns():
    import inspect
    from app import websocket_handler as wh
    src = inspect.getsource(wh)
    assert src.count("koppla_egen_tur(openai_service, connection, phase_emitter, serializer)") == 1


@pytest.mark.asyncio
async def test_a_tool_waits_for_a_guard_that_is_a_coroutine():
    from tests.test_ha_misslyckades import _call, _service
    service = _service()
    ran = []

    async def tool(params):
        ran.append(params.function_name)
        await params.result_callback("ok")
    service.register_function("intent__HassTurnOn", tool)

    async def nej():
        return "ignored: echo"
    service.verktygsvakt = nej
    got = await _call(service, "intent__HassTurnOn", {"area": "hall"})
    assert ran == [] and "ignored" in str(got)

    async def ja():
        return None
    service.verktygsvakt = ja
    await _call(service, "intent__HassTurnOn", {"area": "hall"})
    assert ran == ["intent__HassTurnOn"]


def test_the_mic_is_not_amplified_in_such_a_room_also_without_the_canceller_library(monkeypatch):
    """The same as in test_mjuk_aec.py, which is skipped where aec_audio_processing is not installed."""
    from app.mjuk_aec import MjukAec
    monkeypatch.setenv("SOFT_AEC", "0")
    monkeypatch.setenv("AEC_SPARA", "1")       # the object exists without the library
    monkeypatch.setenv("AEC_SPARA_KATALOG", "/nonexistent-egen-tur")
    monkeypatch.setenv("GEMINI_EGEN_TUR", "kontoret")
    MjukAec._per_enhet.clear()
    try:
        assert MjukAec.for_enhet("kontoret").agc_max_db == 0.0
        assert MjukAec.for_enhet("koket").agc_max_db > 0.0
    finally:
        MjukAec._per_enhet.clear()
