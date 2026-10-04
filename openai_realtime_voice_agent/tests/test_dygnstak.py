"""The daily cloud budget must survive every way a session ends, and a bad ledger.

Two holes from the 2026-10-04 review. Each test fails on 0.27.0.
"""
import logging

import pytest

from app.device_registry import DeviceConnection
from app.providers.sovlage import Budget, SovlageMixin
from app.websocket_handler import WebSocketHandler


@pytest.fixture(autouse=True)
def egen_budget(tmp_path, monkeypatch):
    """Never the real ledger on the box the tests run on."""
    b = Budget(path=str(tmp_path / "moln.json"), today=lambda: "2026-10-04")
    monkeypatch.setattr(SovlageMixin, "budget", b)
    return b


class Motor(SovlageMixin):
    """A cloud session with a clock and a disconnect, and nothing else."""

    def __init__(self):
        self.sover = True
        self.calls = []

    async def _ateranslut(self, forut):
        self.calls.append(("upp", forut))

    async def _disconnect(self):
        self.calls.append("ner")


@pytest.mark.asyncio
async def test_rivning_bokfor_oppen_tid_en_gang(egen_budget):
    """Teardown, the HA recycle (socket close 1000) and a displacing
    reconnect all end in WebSocketHandler._teardown. That has to record the
    open seconds once. A later sova() must not add them again."""
    s = Motor()
    await s.vakna()
    s._uppkopplad_sedan -= 90

    await WebSocketHandler()._teardown(DeviceConnection(device_id="kontoret", websocket=None, openai_service=s))

    assert 89 < egen_budget.anvant() < 95
    await s.sova("redan bokford")
    assert 89 < egen_budget.anvant() < 95


@pytest.mark.asyncio
async def test_trasig_ledger_och_alla_oppna_motorer(egen_budget, tmp_path, caplog, monkeypatch):
    """A torn ledger must not read as zero, and two awake engines count together."""
    monkeypatch.setenv("MOLN_MAX_MINUTER_PER_DAG", "60")
    a, b = Motor(), Motor()
    await a.vakna()
    await b.vakna()
    a._uppkopplad_sedan -= 50 * 60
    b._uppkopplad_sedan -= 20 * 60
    # 50 + 20 minutes open, nothing saved yet. Either engine alone is under 60.
    assert a.over_budget() is True
    assert b.over_budget() is True

    path = tmp_path / "egen.json"
    ledger = Budget(path=str(path), today=lambda: "2026-10-04")
    ledger.lagg_till(120)
    path.write_text("{trasig")
    with caplog.at_level(logging.ERROR, logger="app.providers.sovlage"):
        assert ledger.anvant() == 120
    assert any("budget" in r.message for r in caplog.records)
    ledger.lagg_till(30)
    assert ledger.anvant() == 150
    assert Budget(path=str(path), today=lambda: "2026-10-04").anvant() == 150
