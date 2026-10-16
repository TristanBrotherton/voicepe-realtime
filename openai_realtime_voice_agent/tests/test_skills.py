"""The speaker's tools through comms' skill catalogue (raawr US-098, E-07/D-27 point 5)."""
import types

import pytest

from app import skills
from tests.test_ha_tools_recovery import _connect, _tool_names
from tests.test_mcp_dorr import _Klient, _schema
from tests.test_provider_selection import _bare_app

KATALOG = [("hus", "läget i huset och att tända och släcka"), ("kalender", "läsa och ändra kalendern")]


def _app(monkeypatch, lage, katalog=KATALOG):
    if lage:
        monkeypatch.setenv("ROST_SKILLS", lage)
    else:
        monkeypatch.delenv("ROST_SKILLS", raising=False)
    svar = list(katalog) if isinstance(katalog, list) else katalog

    async def hamta(self):
        return svar() if callable(svar) else svar

    monkeypatch.setattr(skills.Katalog, "_hamta", hamta)
    app = _bare_app("openai")
    app.mcp_client = _Klient(_schema("HassTurnOn", "GetLiveContext"))
    app.dorr_client = _Klient(_schema("las_skill", "anvand_verktyg", "hus_lage", "styr", "lage_drift"))
    return app


@pytest.mark.asyncio
async def test_ac1_sessionen_har_bara_de_fasta_verktygen(monkeypatch):
    _, service = await _connect(_app(monkeypatch, "b"))
    namn = set(_tool_names(service))
    assert {"las_skill", "anvand_verktyg", "request_follow_up", "set_timer"} <= namn
    assert namn <= skills.EGNA | skills.META, namn - skills.EGNA - skills.META


@pytest.mark.asyncio
async def test_ac1_vag_c_har_ocksa_hus_lage_och_styr(monkeypatch):
    _, service = await _connect(_app(monkeypatch, "c"))
    namn = set(_tool_names(service))
    assert {"hus_lage", "styr"} <= namn
    assert namn <= skills.EGNA | skills.META | skills.HETA


@pytest.mark.asyncio
async def test_ac1_av_ger_dagens_verktyg(monkeypatch):
    _, service = await _connect(_app(monkeypatch, ""))
    namn = set(_tool_names(service))
    assert {"HassTurnOn", "search_home", "lage_drift"} <= namn
    assert "styr" not in namn and "las_skill" not in namn


@pytest.mark.asyncio
async def test_ac1_prompten_ar_karnan_plus_en_rad_per_skill(monkeypatch):
    app = _app(monkeypatch, "b")
    await _connect(app)
    text = app._instructions("office")
    assert text.startswith(app.instructions)
    assert "- hus: läget i huset" in text and "- kalender: läsa och ändra kalendern" in text
    assert "las_skill" in text and "anvand_verktyg" in text


@pytest.mark.asyncio
async def test_ac5_katalogen_nere_ger_en_session_och_en_kort_mening(monkeypatch):
    app = _app(monkeypatch, "b", katalog=None)
    _, service = await _connect(app)
    assert {"request_follow_up", "set_timer"} <= set(_tool_names(service))
    assert skills.NAS_INTE in app._instructions("office")


@pytest.mark.asyncio
async def test_ac5_senast_hamtade_katalogen_anvands_nar_comms_hickar(monkeypatch):
    svar = [KATALOG, None]
    app = _app(monkeypatch, "b", katalog=lambda: svar.pop(0))
    await _connect(app)
    await _connect(app)
    text = app._instructions("office")
    assert "- hus: läget i huset" in text and skills.NAS_INTE not in text


@pytest.mark.asyncio
async def test_ac4_ett_nej_fran_comms_nar_modellen_som_text(monkeypatch):
    """comms says no with isError: true and one short sentence; pipecat must hand the sentence on, not drop it."""
    from pipecat.services.mcp_service import MCPClient

    nej = "Det får jag inte göra härifrån."

    class _Session:
        async def call_tool(self, namn, arguments=None):
            return types.SimpleNamespace(isError=True, content=[types.SimpleNamespace(text=nej)])

    fick = []

    async def tillbaka(resultat):
        fick.append(resultat)

    klient = MCPClient.__new__(MCPClient)
    klient._tools_output_filters = {}
    await klient._call_tool(_Session(), "anvand_verktyg", {"namn": "lock_unlock", "argument": {}}, tillbaka)
    assert fick == [nej]


def test_behall_tar_comms_prefix_och_bara_det_som_ska_med(monkeypatch):
    monkeypatch.setenv("ROST_SKILLS", "b")
    assert skills.behall("rost__anvand_verktyg") and skills.behall("set_timer")
    assert not skills.behall("hus_lage") and not skills.behall("HassTurnOn")
    monkeypatch.setenv("ROST_SKILLS", "c")
    assert skills.behall("hus_lage")
    monkeypatch.setenv("ROST_SKILLS", "ja")
    assert skills.lage() == ""


def test_katalogens_adress_ligger_bredvid_api(monkeypatch):
    monkeypatch.setenv("HA_API_URL", "http://c:3500/kanal/rost/kontoret/api")
    assert skills.katalog_url() == "http://c:3500/kanal/rost/kontoret/skills"
    monkeypatch.setenv("HA_API_URL", "http://c:3500/annat")
    assert skills.katalog_url() == ""


@pytest.mark.asyncio
async def test_ac1_omhamtningen_vid_vakning_haller_ocksa_urvalet(monkeypatch):
    """HA was down when the session was built; the next wake fetches the list again, and only the fixed few go on."""
    from tests.test_fynd8_ha_borta import _Queue, _sleep_untouched

    app = _app(monkeypatch, "b")
    app.mcp_client = _Queue([RuntimeError("404 Not Found"), _schema("HassTurnOn")])
    connection, service = await _connect(app, phase="idle")
    _sleep_untouched(service)
    await app.hamta_verktyg_vid_vakning(connection)
    namn = set(_tool_names(service))
    assert "HassTurnOn" not in namn
    assert namn <= skills.EGNA | skills.META, namn - skills.EGNA - skills.META


def test_matningen_laser_verktyg_och_ratt_svar():
    """tools/skills_matning.py: the tool called (anvand_verktyg shown by the comms tool it ran) and the key."""
    import sys
    sys.path.insert(0, "tools")
    from skills_matning import anropade, ratt

    rader = [
        "x | DEBUG | ... Calling function [las_skill:call_1] with arguments {'namn': 'hus'}",
        "x | DEBUG | ... Calling function [anvand_verktyg:call_2] with arguments {'namn': 'styr', 'argument': {}}",
        "x | DEBUG | ... Calling function [homeassistant__GetLiveContext:call_3] with arguments {'domain': 'light'}",
    ]
    assert anropade(rader) == ["las_skill", "anvand_verktyg:styr", "GetLiveContext"]
    assert ratt({"vantat": ["styr"]}, ["las_skill", "anvand_verktyg:styr"], "Klart.")
    assert not ratt({"vantat": ["styr"]}, ["las_skill"], "Klart.")
    assert ratt({"vantat": ["-"], "ord": ["34"]}, [], "Det blir 34.")
    assert not ratt({"vantat": ["-"], "ord": ["34"]}, [], "Det blir 35.")
    assert not ratt({"vantat": ["-"]}, ["HassTurnOn"], "Tänt.")
    from skills_matning import verktygstider
    tid = ["x - INFO - ⏱ tool las_skill 40 ok", "x - INFO - ⏱ tool rost__anvand_verktyg 610 ok",
           "x - INFO - ⏱ tool homeassistant__GetLiveContext 300 fel"]
    assert verktygstider(tid, anropade(rader)) == [["las_skill", 40, "ok"], ["anvand_verktyg:styr", 610, "ok"],
                                                    ["GetLiveContext", 300, "fel"]]


@pytest.mark.asyncio
async def test_av_far_dorrens_nya_verktyg_inte_ovanpa_has(monkeypatch):
    """The door grows with Grok's tools; with ROST_SKILLS off the speaker keeps only what the door listed before."""
    app = _app(monkeypatch, "")
    app.dorr_client = _Klient(_schema("lage_drift", "hamta_svar", "styr", "hus_lage", "las_skill", "kostnad"))
    _, service = await _connect(app)
    namn = set(_tool_names(service))
    assert {"lage_drift", "hamta_svar", "HassTurnOn"} <= namn
    assert not namn & {"styr", "hus_lage", "las_skill", "kostnad"}


def test_ljuslaget_aterstalls_som_det_var():
    """tools/ljus_lage.py (B's review of #90): each light whose on/off changed goes back; the rest is left alone."""
    import sys
    sys.path.insert(0, "tools")
    from ljus_lage import andringar

    fore = {"light.kontor_fonster": "off", "light.hall": "on", "light.ute": "on", "light.elsa": "unavailable"}
    efter = {"light.kontor_fonster": "on", "light.hall": "off", "light.ute": "on", "light.elsa": "on"}
    assert sorted(andringar(fore, efter)) == [("light.hall", "turn_on"), ("light.kontor_fonster", "turn_off")]
