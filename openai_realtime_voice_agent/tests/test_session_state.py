"""One state machine per session (raawr US-032 AC-9)."""
import ast
import logging
import pathlib
import random

import pytest

from app.session_state import (CLOSING, EVENTS, IDLE, LISTENING, SPEAKING, STATES, TABLE, THINKING,
                               WAKE, SessionMaskin)


def test_varje_par_lage_och_handelse_har_ett_utfall():
    assert set(TABLE) == {(s, e) for s in STATES for e in EVENTS}
    assert all(v is None or v in STATES for v in TABLE.values())


@pytest.mark.parametrize("seed", range(20))
def test_slumpade_foljder_hamnar_aldrig_utanfor_tabellen_och_nar_idle(seed):
    rng = random.Random(seed)
    m = SessionMaskin("test")
    for _ in range(300):
        fore = m.state
        ok = m.handle(rng.choice(EVENTS), "slump")
        assert m.state in STATES
        if not ok:
            assert m.state == fore  # a refused event changes nothing
    # The events stop. The session cap (or the quiet rule) closes the engine:
    # from any state, close then closed ends in IDLE.
    if m.state != CLOSING:
        assert m.handle("close", "maxtid")
    assert m.handle("closed", "maxtid")
    assert m.state == IDLE


def test_den_hela_kedjan_med_orsaker_i_journalen(caplog):
    caplog.set_level(logging.INFO)
    m = SessionMaskin("kontoret")
    for event, reason in [("wake", "device wake"), ("listening", "user speech"),
                          ("thinking", "turn end"), ("replying", "bot audio"),
                          ("close", "quiet for 30s"), ("closed", "quiet for 30s")]:
        assert m.handle(event, reason)
    kedja = [r.getMessage() for r in caplog.records if "🧭" in r.getMessage()]
    assert kedja == [
        "🧭 kontoret: IDLE -> WAKE (wake: device wake)",
        "🧭 kontoret: WAKE -> LISTENING (listening: user speech)",
        "🧭 kontoret: LISTENING -> THINKING (thinking: turn end)",
        "🧭 kontoret: THINKING -> SPEAKING (replying: bot audio)",
        "🧭 kontoret: SPEAKING -> CLOSING (close: quiet for 30s)",
        "🧭 kontoret: CLOSING -> IDLE (closed: quiet for 30s)",
    ]


def test_otillaten_handelse_andrar_ingenting_och_skrivs_som_avvisad(caplog):
    caplog.set_level(logging.INFO)
    m = SessionMaskin("kontoret")
    assert m.handle("closed", "ingen stängning pågår") is False
    assert m.state == IDLE
    m.state = CLOSING
    assert m.handle("replying", "sen ljudram") is False
    assert m.handle("close", "dubbel") is False
    assert m.state == CLOSING
    assert [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()] == [
        "🧭 kontoret: rejected closed in IDLE (ingen stängning pågår)",
        "🧭 kontoret: rejected replying in CLOSING (sen ljudram)",
        "🧭 kontoret: rejected close in CLOSING (dubbel)",
    ]


def test_vaeckningen_vinner_over_en_nedstangning():
    m = SessionMaskin("kontoret")
    m.handle("close", "quiet")
    assert m.handle("wake", "ny väckning") and m.state == WAKE


@pytest.mark.asyncio
async def test_fas_till_enheten_skickas_bara_om_laget_tillater_den():
    skickat = []

    async def skicka(v):
        skickat.append(v)

    m = SessionMaskin("kontoret", skicka)
    await m.phase("listening", "wake")
    await m.phase("replying", "bot")
    m.state = CLOSING
    await m.phase("replying", "sen ljudram")  # refused: nothing reaches the device
    await m.phase("idle", "klar")
    assert skickat == ["listening", "replying", "idle"]


@pytest.mark.asyncio
async def test_sov_bokfor_och_oppnar_inte_en_stangning_som_redan_pagar():
    class Tjanst:
        anrop = 0

        async def sova(self, orsak):
            Tjanst.anrop += 1
            return True

    m = SessionMaskin("kontoret")
    assert await m.sov(Tjanst(), "quiet") is True
    assert m.state == IDLE and Tjanst.anrop == 1
    m.state = CLOSING
    assert await m.sov(Tjanst(), "dubbel") is False
    assert Tjanst.anrop == 1  # the engine was not asked twice


# --- the source guard: nobody else sends the signals -----------------------------

APP = pathlib.Path(__file__).parent.parent / "app"
FORBUDET = {"sova", "send_phase"}


def _overtradelser():
    """Uses in app/ of the engine's wake/sleep or a device's phase sender, outside
    the state machine: a call, or the method taken as a value (G's review of
    PR #19: `g = svc.sova; await g(r)` got past a call-only guard). Allowed:
    the machine's own file; `.vakna` on `self`, the recovery wrapper or the machine (it
    calls the machine); the one wiring, `SessionMaskin(id, connection.send_phase)`.
    A `def` is not a use. (The music-ducking signal, AC-5, goes through the
    machine too; this guard grows with it.)"""
    funna = []
    for fil in sorted(APP.rglob("*.py")):
        if fil.name == "session_state.py":
            continue
        traed = ast.parse(fil.read_text())
        tillatna = {id(a) for n in ast.walk(traed)
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SessionMaskin"
                    for a in n.args}
        for nod in ast.walk(traed):
            if isinstance(nod, ast.Attribute) and id(nod) not in tillatna:
                if nod.attr in FORBUDET:
                    funna.append(f"{fil.name}:{nod.lineno} {ast.unparse(nod)}")
                elif nod.attr == "vakna" and ast.unparse(nod.value) not in ("self", "connection.recovery", "self._maskin"):
                    funna.append(f"{fil.name}:{nod.lineno} {ast.unparse(nod)}")
            elif (isinstance(nod, ast.Call) and getattr(nod.func, "id", "") == "getattr" and len(nod.args) > 1
                  and isinstance(nod.args[1], ast.Constant) and nod.args[1].value in FORBUDET | {"vakna"}):
                funna.append(f"{fil.name}:{nod.lineno} getattr(..., {nod.args[1].value!r})")
            elif isinstance(nod, ast.Call) and isinstance(nod.func, ast.Name) and nod.func.id in FORBUDET | {"vakna"}:
                funna.append(f"{fil.name}:{nod.lineno} {nod.func.id}()")
    return funna


def test_bara_tillstandsmaskinen_skickar_fas_vakna_och_sova():
    """Fails on 0.27.0-0.27.9, where phases, wake and sleep are called from the
    websocket handler, the xAI service and the phase emitter."""
    assert _overtradelser() == []


# --- tools/kedjekoll.py ----------------------------------------------------------

def _kedjekoll():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kedjekoll", APP.parent / "tools" / "kedjekoll.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rader(m, handelser, device="attrapp"):
    import io
    ut = io.StringIO()
    h = logging.StreamHandler(ut)
    logging.getLogger("app.session_state").addHandler(h)
    logging.getLogger("app.session_state").setLevel(logging.INFO)
    try:
        for e in handelser:
            m.handle(*e)
    finally:
        logging.getLogger("app.session_state").removeHandler(h)
    return ut.getvalue().splitlines()


def test_kedjekollen_godkanner_en_obruten_kedja_och_fangar_en_bruten():
    k = _kedjekoll()
    ok_rader = _rader(SessionMaskin("attrapp"), [
        ("wake", "w"), ("listening", "l"), ("close", "quiet for 30s"), ("closed", "quiet for 30s")])
    assert k.kolla(ok_rader, "attrapp")[0] is True
    bruten = ["🧭 attrapp: IDLE -> WAKE (wake: w)", "🧭 attrapp: THINKING -> IDLE (idle: x)"]
    ok, rader = k.kolla(bruten, "attrapp")
    assert ok is False and any("bruten kedja" in r for r in rader)
    ok, rader = k.kolla(["🧭 attrapp: IDLE -> WAKE (wake: w)"], "attrapp")
    assert ok is False and "slutar i WAKE" in rader[-1]
    ok, rader = k.kolla(["🧭 attrapp: rejected closed in IDLE (x)"], "attrapp")
    assert ok is False and rader[0].startswith("avvisad")


@pytest.mark.asyncio
async def test_tappad_lank_stanger_kedjan_i_idle():
    """Case D of the stand-in run on core (2026-10-07): the link dropped in
    THINKING, nothing closed the chain."""
    from app.device_registry import DeviceConnection
    from app.websocket_handler import WebSocketHandler

    class Tjanst:
        sover = False

        def bokfor(self):
            pass

        async def disconnect(self):
            pass

    handler = WebSocketHandler()
    c = DeviceConnection(device_id="attrapp", websocket=object())
    c.maskin = SessionMaskin("attrapp")
    for e in ("wake", "listening", "thinking"):
        c.maskin.handle(e, "t")
    c.openai_service = Tjanst()
    await handler._teardown(c)
    assert c.maskin.state == IDLE
