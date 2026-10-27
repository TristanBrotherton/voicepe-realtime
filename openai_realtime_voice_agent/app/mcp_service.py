"""Home Assistant MCP integration on pipecat's persistent MCPClient.

pipecat 1.x's ``MCPClient`` keeps ONE session open in a dedicated owner task
(the MCP SDK's transports must be entered and exited in the same task) and
reuses it for tool calls from every device — which is what the 0.0.x-era
``PersistentMCPClient`` had to build itself. What stays ours is the policy
around each call, applied by ``guarded_handler`` on top of the handler the
schema from ``MCPClient.tools()`` carries:

* A session that has been idle longer than ``IDLE_PING_S`` is probed first
  (``tools()``: pipecat exposes no ping, a tool listing is the cheapest
  documented round trip); failures to open or probe a session reopen it and
  are retried once — nothing has been sent to Home Assistant yet at that
  point.
* A call is bounded by ``CALL_TIMEOUT_S``.
* Once the tool call itself has been sent, a failure is NOT retried: the
  action may already have happened, and a retry could do it twice. The model
  gets an honest "may or may not have completed" error instead of pipecat's
  "Error calling mcp tool …" line, which reads as a safe-to-retry failure.
* ``MCP_PERSISTENT_SESSION=false`` closes the session after every call
  (pipecat 1.x has no per-call mode of its own).
"""
import asyncio
import dataclasses
import logging
import os
import time
from typing import Optional

from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

logger = logging.getLogger(__name__)

# How pipecat's tool wrapper reports a call that raised (transport gone,
# HTTP error, timeout inside the SDK) — as a result string, not an exception.
# A tool error Home Assistant itself returns is ordinary content and does not
# start like this.
PIPECAT_CALL_ERROR_PREFIX = "Error calling mcp tool "


class PersistentMCPClient(MCPClient):
    CALL_TIMEOUT_S = 45.0
    OPEN_TIMEOUT_S = 15.0
    IDLE_PING_S = 60.0
    PING_TIMEOUT_S = 3.0

    def __init__(self, server_params, *, persistent: bool = True, **kwargs):
        super().__init__(server_params, **kwargs)
        self._persistent = persistent
        self._open_lock: Optional[asyncio.Lock] = None
        self._last_used = 0.0
        self._opened = False
        self.session_opens = 0

    def _lock(self) -> asyncio.Lock:
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        return self._open_lock

    def _note_opened(self) -> None:
        if not self._opened:
            self._opened = True
            self.session_opens += 1
            logger.info(f"🔗 Home Assistant MCP session opened (#{self.session_opens})")
        self._last_used = time.monotonic()

    async def _open(self) -> None:
        await asyncio.wait_for(self.start(), self.OPEN_TIMEOUT_S)
        self._note_opened()

    async def _close(self) -> None:
        self._opened = False
        try:
            await asyncio.wait_for(self.close(), 3)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"MCP session close: {e!r}")

    async def _acquire(self) -> None:
        """Make sure a healthy session is open; raises only before anything was sent."""
        async with self._lock():
            alive = self._opened
            if alive and time.monotonic() - self._last_used > self.IDLE_PING_S:
                try:
                    await asyncio.wait_for(self.tools(), self.PING_TIMEOUT_S)
                except Exception as e:
                    logger.info(f"🔌 idle MCP session failed its probe ({e!r}); reopening")
                    alive = False
            if not alive:
                await self._close()
                await self._open()
            self._last_used = time.monotonic()

    async def tools(self):
        """The tool schemas; opens the session when none is open."""
        schema = await super().tools()
        self._note_opened()
        return schema

    def guarded_handler(self, function_schema):
        """The call handler for one tool, with the session policy around it.

        Args:
            function_schema: One of ``(await self.tools()).standard_tools``;
                its ``handler`` is pipecat's own tool wrapper.
        """
        call_tool = function_schema.handler
        if call_tool is None:
            raise ValueError(f"MCP tool {function_schema.name} carries no handler")

        async def handler(params) -> None:
            await self._guarded_call(call_tool, params)

        return handler

    async def _guarded_call(self, call_tool, params) -> None:
        name = params.function_name
        acquired = False
        for attempt in (1, 2):
            try:
                await self._acquire()
                acquired = True
                break
            except Exception as e:
                logger.warning(f"⚠️ MCP session unavailable (attempt {attempt}): {e!r}")
                await self._close()
        if not acquired:
            await params.result_callback(
                f"Error calling Home Assistant tool {name}: Home Assistant is not reachable."
            )
            return

        honest = (
            f"Error: Home Assistant did not confirm {name}; "
            "it may or may not have completed. Say so briefly; do not retry automatically."
        )
        original_callback = params.result_callback
        failed_after_send = False

        async def result_callback(result, *, properties=None):
            nonlocal failed_after_send
            if isinstance(result, str) and result.startswith(PIPECAT_CALL_ERROR_PREFIX):
                logger.warning(f"⚠️ MCP tool {name} failed after sending: {result}")
                failed_after_send = True
                result = honest
            if properties is None:
                await original_callback(result)
            else:
                await original_callback(result, properties=properties)

        guarded_params = dataclasses.replace(params, result_callback=result_callback)
        try:
            await asyncio.wait_for(call_tool(guarded_params), self.CALL_TIMEOUT_S)
            if not failed_after_send:
                self._last_used = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ MCP tool {name} failed after sending: {e!r}")
            failed_after_send = True
            await original_callback(honest)
        if failed_after_send or not self._persistent:
            await self._close()

    async def aclose(self) -> None:
        await self._close()


class HomeAssistantMCPService:
    """Home Assistant MCP service using Pipecat's MCPClient."""

    def __init__(self, url: str, access_token: str):
        """
        Args:
            url: Home Assistant MCP Server URL (e.g., http://supervisor/core/api/mcp)
            access_token: Long-lived access token for Home Assistant
        """
        self.url = url
        self.access_token = access_token
        self.mcp_client: Optional[PersistentMCPClient] = None

    async def initialize(self) -> PersistentMCPClient:
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            server_params = StreamableHttpParameters(
                url=self.url,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            persistent = os.environ.get("MCP_PERSISTENT_SESSION", "true").strip().lower() != "false"
            self.mcp_client = PersistentMCPClient(server_params=server_params, persistent=persistent)
            logger.info(
                f"✅ Home Assistant MCP Client initialized "
                f"({'persistent session' if persistent else 'session per call'})"
            )
            return self.mcp_client
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise

    def get_client(self) -> Optional[PersistentMCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client
