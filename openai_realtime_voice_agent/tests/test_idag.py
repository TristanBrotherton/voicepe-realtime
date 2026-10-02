"""The "Idag:" block (0.23.3): today's facts in the prompt, kept fresh.

The HA calendar scripts tell the model to compute dates "ur raden Idag: i
systemprompten" -- a line that did not exist. Now it does, with the weather
and the next events, so "vad blir det för väder" needs no tool round.
"""
import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.idag import TZ, Idag
from app.providers import GEMINI, build_service
from test_gemini_main_engine import NATIVE, _connect_config  # noqa: E402
from test_gemini_provider import OPENAI_SHAPE, _options as options  # noqa: E402

NOW = datetime(2026, 10, 2, 18, 14, tzinfo=TZ)  # a Friday

WEATHER = {"weather.smhi_home": {"forecast": [
    {"datetime": "2026-10-02T16:00:00+00:00", "condition": "partlycloudy", "temperature": 16.3,
     "templow": 12.8, "precipitation": 0.3, "wind_speed": 10.44},
    {"datetime": "2026-10-03T12:00:00+00:00", "condition": "rainy", "temperature": 17.1,
     "templow": 12.6, "precipitation": 2.1, "wind_speed": 18.36},
    {"datetime": "2026-10-04T12:00:00+00:00", "condition": "sunny", "temperature": 15.6,
     "templow": 9.1, "precipitation": 0.0, "wind_speed": 9.0},
    {"datetime": "2026-10-05T12:00:00+00:00", "condition": "cloudy", "temperature": 17.0,
     "templow": 11.2},
]}}
CALENDAR = {"antal": 4, "handelser": [
    {"id": "k9", "vad": "Redan i morse", "borjar": "2026-10-02T07:00:00+02:00"},
    {"id": "k1", "vad": "Tes Ridning", "borjar": "2026-10-03T10:30:00Z"},
    {"id": "k26", "vad": "Brynäs –\nMalmö", "borjar": "2026-10-03T15:15:00+02:00"},
    {"id": "k2", "vad": "Moa Gymnastik", "borjar": "2026-10-04T14:00:00Z"},
    {"id": "k3", "vad": "Elsa Ridning", "borjar": "2026-10-05T16:00:00Z"},
]}


def _idag(weather=WEATHER, calendar=CALENDAR):
    async def call(name, arguments=None):
        result = weather if name == "script__vaderprognos" else calendar
        if isinstance(result, Exception):
            raise result
        return result
    return Idag(call=call)


@pytest.mark.asyncio
async def test_block_format():
    idag = _idag()
    await idag.refresh(NOW)
    block = idag.block(NOW)
    lines = block.strip().split("\n")
    # The exact line the HA scripts point at.
    assert lines[0].startswith("Idag: fredag 2026-10-02, klockan 18:14")
    assert lines[1] == (
        "Väder (SMHI): idag halvklart 13–16°, 0.3 mm, vind 3 m/s; "
        "i morgon regn 13–17°, 2.1 mm, vind 5 m/s; söndag 4/10 sol 9–16°, vind 2 m/s."
    )
    # Past events dropped, UTC turned into Swedish time, titles on one line.
    assert lines[2] == (
        "Kalendern härnäst: i morgon 12:30 Tes Ridning; i morgon 15:15 Brynäs – Malmö; "
        "söndag 4/10 16:00 Moa Gymnastik."
    )
    assert len(block) < 1200  # well under ~300 tokens


@pytest.mark.asyncio
async def test_a_failed_fetch_keeps_the_last_good_block():
    idag = _idag()
    await idag.refresh(NOW)
    good = idag.block(NOW)
    idag._call = AsyncMock(side_effect=RuntimeError("comms down"))
    await idag.refresh(NOW)
    assert idag.block(NOW) == good


@pytest.mark.asyncio
async def test_one_part_failing_does_not_take_the_other():
    idag = _idag(weather=RuntimeError("SMHI"))
    await idag.refresh(NOW)
    block = idag.block(NOW)
    assert "Väder" not in block and "Tes Ridning" in block


@pytest.mark.asyncio
async def test_refresh_replaces_the_facts():
    idag = _idag()
    await idag.refresh(NOW)
    idag._call = AsyncMock(return_value={"handelser": [
        {"vad": "Tandläkare", "borjar": "2026-10-06T09:00:00+02:00"}]})
    await idag.refresh(NOW)
    assert "tisdag 6/10 09:00 Tandläkare" in idag.block(NOW)


@pytest.mark.asyncio
async def test_day_names_are_worked_out_when_rendered_not_when_fetched():
    idag = _idag()
    await idag.refresh(NOW)
    after_midnight = datetime(2026, 10, 3, 0, 5, tzinfo=TZ)
    block = idag.block(after_midnight)
    assert "Idag: lördag 2026-10-03, klockan 00:05" in block
    assert "Väder (SMHI): idag regn" in block
    assert "Kalendern härnäst: idag 12:30 Tes Ridning" in block


def test_without_any_facts_the_date_line_still_stands():
    block = Idag(call=AsyncMock()).block(NOW)
    assert "Idag: fredag 2026-10-02" in block and "Väder" not in block


# --- how the block reaches Gemini -----------------------------------------

def test_gemini_renders_the_instruction_on_every_connect():
    """A resumed session follows a NEW system instruction (measured live)."""
    service = build_service(GEMINI, options(model=NATIVE), OPENAI_SHAPE)
    calls = []
    service.instructions_provider = lambda: calls.append(1) or f"Du är Björn. Idag: {len(calls)}"
    assert _connect_config(service).system_instruction == "Du är Björn. Idag: 1"
    service._session = None
    assert _connect_config(service).system_instruction == "Du är Björn. Idag: 2"


def test_a_broken_provider_keeps_the_last_instruction():
    service = build_service(GEMINI, options(model=NATIVE), OPENAI_SHAPE)
    service.instructions_provider = lambda: 1 / 0
    config = _connect_config(service)
    assert config.system_instruction == service._system_instruction_from_init


@pytest.mark.asyncio
async def test_refresh_never_reconnects_mid_turn():
    service = build_service(GEMINI, options(model=NATIVE), OPENAI_SHAPE)
    service._reconnect = AsyncMock()
    service._session = object()
    for busy in ("_activity_open", "_reply_awaited_at"):
        setattr(service, busy, time.monotonic())
        assert await service.refresh_instructions() is False
        setattr(service, busy, None if busy == "_reply_awaited_at" else False)
    service._held = [b"audio"]
    assert await service.refresh_instructions() is False
    service._held = None
    service._reply_awaited_at = None
    assert await service.refresh_instructions() is True
    service._reconnect.assert_awaited_once()


# --- Google search grounding ----------------------------------------------

WITH_WEB = OPENAI_SHAPE + [{"type": "function", "name": "web_search", "description": "web",
                            "parameters": {"type": "object", "properties": {}}}]


def _tools(service):
    return service._tools_from_init


def test_google_search_is_off_by_default(monkeypatch):
    monkeypatch.delenv("GEMINI_GOOGLE_SEARCH", raising=False)
    tools = _tools(build_service(GEMINI, options(model=NATIVE), WITH_WEB))
    assert all("google_search" not in t for t in tools)
    assert "web_search" in [d["name"] for d in tools[0]["function_declarations"]]


def test_google_search_on_replaces_our_web_search(monkeypatch):
    monkeypatch.setenv("GEMINI_GOOGLE_SEARCH", "true")
    tools = _tools(build_service(GEMINI, options(model=NATIVE), WITH_WEB))
    assert {"google_search": {}} in tools
    names = [d["name"] for d in tools[0]["function_declarations"]]
    assert "web_search" not in names and names


def test_google_search_is_on_by_default_for_the_3x_models(monkeypatch):
    """2.5 native audio + google_search + function tools gives 1011; the 3.x
    live backend does not (0/15 probed), so grounding is its default."""
    monkeypatch.delenv("GEMINI_GOOGLE_SEARCH", raising=False)
    tools = _tools(build_service(GEMINI, options(model="models/gemini-3.8-live"), WITH_WEB))
    assert {"google_search": {}} in tools
    assert "web_search" not in [d["name"] for d in tools[0]["function_declarations"]]


def test_google_search_can_be_turned_off_on_3x(monkeypatch):
    monkeypatch.setenv("GEMINI_GOOGLE_SEARCH", "false")
    tools = _tools(build_service(GEMINI, options(model="models/gemini-3.8-live"), WITH_WEB))
    assert all("google_search" not in t for t in tools)
