"""Web search function-tool.

Lets the Realtime assistant look things up online (weather, news, facts, opening
hours, prices, recent events). The Realtime API has NO native web search, and
pipecat 0.0.97 only supports custom *function* tools — so this is wired exactly
like the disconnect tool: a `web_search` function tool whose handler runs a
SECOND, server-side OpenAI call (the Responses API `web_search` built-in tool),
then returns a short, spoken-friendly answer the Realtime model reads aloud.

Uses the add-on's existing OPENAI_API_KEY — no extra account/vendor. Default
model gpt-5.4-mini is the cheapest model verified to support the web_search tool
(~$0.016/search); it's configurable via WEB_SEARCH_MODEL so a different
price/quality (or a renamed model) can be swapped in without a code change.
"""
import logging
from typing import Dict, Any, Callable, Awaitable, TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)


def get_web_search_tool_definition() -> Dict[str, Any]:
    """OpenAI Realtime function-tool definition for web search."""
    return {
        "type": "function",
        "name": "web_search",
        "description": (
            "Search the public internet for current, real-time, or factual "
            "information the assistant does not already know — for example the "
            "weather, news, sports scores, opening hours, prices, travel info, or "
            "recent events. Do NOT use this for controlling the smart home — use "
            "the Hass* tools for lights, switches, climate, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query, phrased as a clear natural-language "
                        "question in the user's language."
                    ),
                }
            },
            "required": ["query"],
        },
    }


def create_web_search_tool_handler(
    api_key: str, model: str
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    """Create a web_search handler for pipecat's OpenAIRealtimeLLMService.

    The handler calls the OpenAI Responses API with the built-in `web_search`
    tool and returns a short answer via ``params.result_callback`` (which becomes
    the function_call_output the Realtime model speaks).
    """
    client = AsyncOpenAI(api_key=api_key)

    async def web_search_tool_handler(params: "FunctionCallParams") -> None:
        query = (params.arguments or {}).get("query", "").strip()
        logger.info(f"🔎 web_search called: {query!r} (model={model})")

        if not query:
            await params.result_callback("Geen zoekopdracht ontvangen.")
            return

        try:
            response = await client.responses.create(
                model=model,
                tools=[{"type": "web_search"}],
                input=(
                    "Answer in at most 2 short sentences suitable for being read "
                    "aloud, in the same language as the question. Do not include "
                    "URLs, citations, or markdown. Question: " + query
                ),
            )
            answer = (getattr(response, "output_text", None) or "").strip()
            logger.info(f"🔎 web_search answer: {answer[:200]}")
            await params.result_callback(answer or "Ik kon hier online niets over vinden.")
        except Exception as e:
            logger.error(f"❌ web_search failed: {e}", exc_info=True)
            await params.result_callback("Het zoeken op internet lukte even niet.")

    return web_search_tool_handler
