"""Direct ask_openclaw tool: escalation to an external agent, bypassing HA MCP.

Why this exists: when ask_openclaw is exposed as an HA script through HA's MCP
server, every call is capped by HA core's hardcoded MCP request timeout
(homeassistant/components/mcp_server/http.py: TIMEOUT = 60). Deep tasks —
memory recall, contact lookups, multi-step agent turns — routinely take
longer, so the tool call dies while the real answer is still in flight
(observed live: "Buddy's number" answered by the agent at ~75s, discarded).

With OPENCLAW_URL set, the backend registers ask_openclaw natively and POSTs
{"question": ...} straight to the bridge endpoint ({"answer": ...} back),
with a timeout that actually matches agent latency. The same-named HA MCP
tool is skipped during tool assembly so the model sees exactly one. Unset,
everything falls back to the MCP-script path unchanged.

The speaker gate still applies: registration goes through
SafeRealtimeLLMService.register_function, so male_only_tools enforcement is
identical to the MCP path.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Bridge agent turns are killed at 150s on the far side; stay just under so
# the model gets the bridge's own "took too long" message, not a dead socket.
ASK_TIMEOUT_S = 145


def openclaw_url() -> str:
    return os.environ.get("OPENCLAW_URL", "").strip()


def get_openclaw_tool_definition() -> dict:
    return {
        "type": "function",
        "name": "ask_openclaw",
        "description": (
            "Ask the owner's OpenClaw assistant a question or give it a task, and "
            "get its answer. It also holds the household's DEEP LONG-TERM MEMORY - "
            "people, contacts, plans, history, past conversations, preferences going "
            "back months - so use it for personal or historical recall questions you "
            "cannot answer. ONLY for things Home Assistant cannot do itself - "
            "calendar, messaging, phone calls, web knowledge, memory recall, "
            "cross-app or computer tasks. NEVER use this for smart-home control or "
            "anything with a Home Assistant tool - lights, switches, climate, "
            "timers, and especially adding or removing items on shopping or to-do "
            "lists. This can take up to a couple of minutes, so tell the user you "
            "are checking before calling it. One request at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or task, with any needed context",
                }
            },
            "required": ["question"],
        },
    }


def get_recall_tool_definition() -> dict:
    return {
        "type": "function",
        "name": "recall_memory",
        "description": (
            "INSTANT (under a second) search of the household's long-term memory: "
            "people, contact phone numbers, birthdays, preferences, history, past "
            "decisions. ALWAYS try this FIRST for any personal or household recall "
            "question ('what is X's number', 'when is Y's birthday', 'what did we "
            "decide about Z'). Answer from the returned memory lines. Only if "
            "nothing relevant comes back, or the request needs action (messages, "
            "calls, research), fall back to ask_openclaw."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Two to four key words, e.g. 'Buddy phone'",
                }
            },
            "required": ["query"],
        },
    }


def register_openclaw_tool(llm) -> None:
    async def _ask(params) -> None:
        question = ((params.arguments or {}).get("question") or "").strip()
        if not question:
            await params.result_callback({"error": "empty question"})
            return
        try:
            async with httpx.AsyncClient(timeout=ASK_TIMEOUT_S) as client:
                r = await client.post(openclaw_url(), json={"question": question})
                r.raise_for_status()
                answer = (r.json() or {}).get("answer", "").strip()
        except Exception as e:
            logger.warning(f"⚠️ ask_openclaw direct call failed: {e!r}")
            await params.result_callback({
                "error": "The assistant could not be reached; try again shortly."})
            return
        logger.info(f"🦞 ask_openclaw answered ({len(answer)} chars)")
        await params.result_callback({"answer": answer or "(no answer)"})

    async def _recall(params) -> None:
        query = ((params.arguments or {}).get("query") or "").strip()
        if not query:
            await params.result_callback({"matches": [], "error": "empty query"})
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(openclaw_url(), json={"recall": query})
                r.raise_for_status()
                matches = (r.json() or {}).get("matches", [])
        except Exception as e:
            logger.warning(f"⚠️ recall_memory failed: {e!r}")
            await params.result_callback({
                "matches": [], "note": "recall unavailable — use ask_openclaw"})
            return
        logger.info(f"🔎 recall_memory '{query}' -> {len(matches)} lines")
        await params.result_callback({
            "matches": matches,
            "note": "" if matches else "nothing found — ask_openclaw may know more",
        })

    llm.register_function("ask_openclaw", _ask)
    llm.register_function("recall_memory", _recall)
