"""MCP service integration using Pipecat's MCPClient with StreamableHTTP."""
import logging
import os
from typing import Optional
from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

from app import ha_api

logger = logging.getLogger(__name__)


class LasandeMCPClient(MCPClient):
    """pipecat's MCPClient, plus each tool's MCP `readOnlyHint` from tools/list (raawr US-103): `las` maps a tool name
    to True (only reads) or False (changes something). A tool that declares nothing is not in it."""

    las: dict = {}

    async def _list_tools_helper(self, session):
        lista = await session.list_tools()
        self.las = {t.name: bool(t.annotations.readOnlyHint) for t in lista.tools
                    if getattr(t, "annotations", None) is not None and t.annotations.readOnlyHint is not None}

        class _Igen:  # hand pipecat the same list instead of asking the door twice
            async def list_tools(self_):
                return lista
        return await super()._list_tools_helper(_Igen())


class HomeAssistantMCPService:
    """Home Assistant MCP service using Pipecat's MCPClient."""
    
    def __init__(self):
        """Home Assistant's MCP server, reached through raawr-comms.

        HA's `/api/mcp` is stateless Streamable HTTP: one JSON-RPC POST, one
        JSON answer. Comms holds the HA key and lets through only the MCP
        tools it has handed out; this side sends comms' key (app/ha_api.py).
        """
        self.url = ha_api.url("/mcp")
        self.mcp_client: Optional[MCPClient] = None
        
    async def initialize(self) -> MCPClient:
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            
            # Create StreamableHTTP parameters with authentication
            server_params = StreamableHttpParameters(
                url=self.url,
                headers=ha_api.headers(),
            )
            
            # Create MCP client
            self.mcp_client = MCPClient(server_params=server_params)
            
            logger.info("✅ Home Assistant MCP Client initialized")
            return self.mcp_client
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise
    
    def get_client(self) -> Optional[MCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client








def dorr_url() -> Optional[str]:
    """comms' own MCP door for this room, or None when it is off (COMMS_MCP_DORR) or cannot be worked out.

    `/kanal/rost/kontoret/mcp` sits beside `HA_API_URL` (`.../kanal/rost/kontoret/api`), not under it: `/api/mcp`
    goes to Home Assistant, the door hands out comms' own registry (lage_drift, fraga_huset, folj_upp, kasta_in
    ...) -- the same tools Grok has (raawr US-021). COMMS_MCP_URL overrides the derived address.
    """
    if (os.environ.get("COMMS_MCP_DORR") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    explicit = (os.environ.get("COMMS_MCP_URL") or "").strip()
    if explicit:
        return explicit
    bas = ha_api.base()
    return bas[: -len("/api")] + "/mcp" if bas.endswith("/api") else None


class CommsDoorMCPService:
    """comms' MCP door as a second tool source. Nothing is listed here by hand: `tools/list` is asked of the door
    every time the agent builds a session (main.py `_fetch_ha_tools_schema`), so a tool added or changed in comms
    shows up on the next wake with no change to the agent."""

    def __init__(self):
        self.url = dorr_url()
        self.mcp_client: Optional[MCPClient] = None

    async def initialize(self) -> Optional[MCPClient]:
        if not self.url:
            return None
        logger.info(f"🔗 Initializing comms MCP door client at {self.url}")
        self.mcp_client = LasandeMCPClient(server_params=StreamableHttpParameters(url=self.url, headers=ha_api.headers()))
        return self.mcp_client
