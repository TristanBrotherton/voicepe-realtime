"""MCP service integration using Pipecat's MCPClient with StreamableHTTP."""
import logging
from typing import Optional
from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

from app import ha_api

logger = logging.getLogger(__name__)


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






