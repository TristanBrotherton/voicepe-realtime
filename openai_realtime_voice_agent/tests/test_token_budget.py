"""The session's fixed payload stays under a token budget (raawr D-73).

gpt-realtime-2 re-bills instructions + tools + conversation on every
response, and the account allows 40,000 tokens/min. Live 2026-10-02 a turn
cost 15,233 tokens and the second sentence hit `Rate limit reached`.

The tools here are what create_service really hands the engine: our own
tools plus Home Assistant's 45, captured read-only from the office's MCP
(tests/fixtures/ha_mcp_tools.json, names and ids anonymised). The persona is a
stand-in of the same size as the live one (tests/fixtures/persona_instructions.txt).

Counted with tiktoken o200k_base when installed, otherwise UTF-8 bytes / 3.5
(within ~1% on this payload). It is a JSON count: OpenAI renders tools more
compactly, live usage was ~0.78x of it.
"""
import json
from pathlib import Path

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from app.providers.openai_realtime import add_history_cap
from app.providers.tool_registration import cap_tool_result
from tests.test_provider_selection import _bare_app

FIXTURES = Path(__file__).parent / "fixtures"

# Before D-73: 10,514 (live instructions) / ~9,960 (this persona). After: ~8,630.
# Raise it only with a measurement that says why the session needs more.
BUDGET_TOKENS = 9000


def _tokens(text: str) -> int:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return round(len(text.encode()) / 3.5)


def _ha_schema() -> ToolsSchema:
    return ToolsSchema(standard_tools=[
        FunctionSchema(
            name=t["name"],
            description=t["description"],
            properties=t["inputSchema"].get("properties", {}),
            required=t["inputSchema"].get("required", []),
        )
        for t in json.loads((FIXTURES / "ha_mcp_tools.json").read_text())
    ])


class _McpClient:
    async def get_tools_schema(self):
        return _ha_schema()

    async def register_tools_schema(self, schema, service):
        pass


async def _session_payload(monkeypatch):
    from app import providers
    from app.device_registry import DeviceConnection
    from app.providers import OPENAI

    monkeypatch.setenv("OPENCLAW_URL", "http://openclaw.test:3400")
    seen = {}
    real_build = providers.build_service

    def capture(provider, options, tools):
        seen["instructions"], seen["tools"] = options.instructions, tools
        return real_build(provider, options, tools)

    monkeypatch.setattr(providers, "build_service", capture)
    app = _bare_app(OPENAI)
    app.enable_web_search = True
    app.web_search_model = "gpt-5.5"
    app.mcp_client = _McpClient()
    app.instructions = (FIXTURES / "persona_instructions.txt").read_text()
    connection = DeviceConnection(device_id="kontor", websocket=object())
    connection.provider = OPENAI
    await app.create_service(connection)
    return seen["instructions"], seen["tools"]


@pytest.mark.asyncio
async def test_instructions_and_tools_fit_the_budget(monkeypatch):
    instructions, tools = await _session_payload(monkeypatch)
    total = _tokens(instructions) + _tokens(json.dumps(tools, ensure_ascii=False))
    biggest = sorted(((_tokens(json.dumps(t, ensure_ascii=False)), t["name"]) for t in tools), reverse=True)[:5]
    assert total <= BUDGET_TOKENS, f"{total} tokens > {BUDGET_TOKENS}; biggest tools: {biggest}"


@pytest.mark.asyncio
async def test_house_keeps_what_it_uses(monkeypatch):
    _, tools = await _session_payload(monkeypatch)
    names = {t["name"] for t in tools}
    for needed in ("intent__HassTurnOn", "intent__HassTurnOff", "light__HassLightSet",
                   "homeassistant__GetLiveContext", "llm__GetDateTime", "play_media",
                   "media_player__HassMediaPause", "set_timer", "cancel_timer",
                   "script__kalender_sok", "script__kalenderaktivitet", "todo__get_items",
                   "todo__HassListAddItem", "script__vaderprognos",
                   "script__delegera_till_raawr", "ask_openclaw", "web_search"):
        assert needed in names, needed
    # Comms prefixes the names; the duplicates must go anyway.
    assert "media_player__HassMediaSearchAndPlay" not in names
    assert "intent__HassCancelAllTimers" not in names
    turn_on = next(t for t in tools if t["name"] == "intent__HassTurnOn")
    assert {"domain", "device_class", "area"} <= set(turn_on["parameters"]["properties"])
    pause = next(t for t in tools if t["name"] == "media_player__HassMediaPause")
    assert set(pause["parameters"]["properties"]) == {"name", "area", "floor"}


def test_a_whole_house_dump_is_cut_and_says_how_to_narrow_it():
    house = "\n".join(f"- names: Lampa {i}\n  domain: light\n  state: 'off'" for i in range(1000))
    capped = cap_tool_result(house, 6000)
    assert len(capped) < 6300 and "narrower filter" in capped
    assert cap_tool_result("short", 6000) == "short"
    assert cap_tool_result({"ok": True}, 6000) == {"ok": True}
    assert cap_tool_result(house, 0) == house


def test_session_update_caps_the_history(monkeypatch):
    payload = {"type": "session.update", "session": {"instructions": "x"}}
    add_history_cap(payload)
    assert payload["session"]["truncation"]["token_limits"]["post_instructions"] == 2500
    monkeypatch.setenv("REALTIME_HISTORY_TOKENS", "0")
    off = {"type": "session.update", "session": {}}
    add_history_cap(off)
    assert "truncation" not in off["session"]
    other = {"type": "response.create"}
    add_history_cap(other)
    assert other == {"type": "response.create"}


@pytest.mark.asyncio
async def test_every_registered_tool_result_goes_through_the_cap():
    from types import SimpleNamespace

    from app.providers import OPENAI, ProviderOptions, build_service

    service = build_service(OPENAI, ProviderOptions(
        api_key="sk-test", model="gpt-realtime-2", voice="cedar", instructions="x"), [])

    async def dump_the_house(params):
        await params.result_callback("x\n" * 20000)

    service.register_function("homeassistant__GetLiveContext", dump_the_house)
    got = []

    async def result_callback(result, **kwargs):
        got.append(result)

    await service._functions["homeassistant__GetLiveContext"].handler(
        SimpleNamespace(result_callback=result_callback))
    assert len(got[0]) < 6300 and "TRUNCATED" in got[0]
