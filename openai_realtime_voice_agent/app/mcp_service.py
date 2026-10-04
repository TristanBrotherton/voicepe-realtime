"""Home Assistant MCP integration with a reusable tool session.

pipecat's MCPClient opens a new HTTP client, MCP session and initialize
handshake for EVERY tool call. ``PersistentMCPClient`` keeps one session open
in a dedicated owner task (the MCP SDK's transports must be entered and exited
in the same task) and reuses it for tool calls from every device.

Safety rules:
* A session that has been idle longer than ``IDLE_PING_S`` is pinged first;
  failures to acquire or ping a session reopen it and are retried once —
  nothing has been sent to Home Assistant yet at that point.
* Once the tool call itself has been sent, a failure is NOT retried: the
  action may already have happened, and a retry could do it twice. The model
  gets an honest "may or may not have completed" error instead.
* ``MCP_PERSISTENT_SESSION=false`` restores the per-call behaviour.
"""
import asyncio
import logging
import os
import time
from typing import Optional

from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

logger = logging.getLogger(__name__)


class PersistentMCPClient(MCPClient):
    CALL_TIMEOUT_S = 45.0
    OPEN_TIMEOUT_S = 15.0
    IDLE_PING_S = 60.0
    PING_TIMEOUT_S = 3.0

    def __init__(self, server_params, **kwargs):
        super().__init__(server_params, **kwargs)
        self._persistent_session = None
        self._owner: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._open_lock: Optional[asyncio.Lock] = None
        self._last_used = 0.0
        self.session_opens = 0

    def _lock(self) -> asyncio.Lock:
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        return self._open_lock

    async def _own_session(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with self._client(**self._server_params.model_dump()) as (read, write, _):
                async with self._session(read, write) as session:
                    await session.initialize()
                    self.session_opens += 1
                    self._persistent_session = session
                    if not ready.done():
                        ready.set_result(session)
                    await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                logger.info(f"🔌 Home Assistant MCP session closed: {e!r}")
        finally:
            self._persistent_session = None

    async def _open(self):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self._stop = asyncio.Event()
        self._owner = asyncio.create_task(self._own_session(ready, self._stop))
        session = await asyncio.wait_for(ready, self.OPEN_TIMEOUT_S)
        self._last_used = time.monotonic()
        logger.info(f"🔗 Home Assistant MCP session opened (#{self.session_opens})")
        return session

    async def _close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        owner, self._owner = self._owner, None
        if owner is not None and not owner.done():
            try:
                await asyncio.wait_for(owner, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                owner.cancel()
        self._persistent_session = None

    async def _acquire(self):
        """Return a healthy session; raises only before anything was sent."""
        async with self._lock():
            session = self._persistent_session
            alive = session is not None and self._owner is not None and not self._owner.done()
            if alive and time.monotonic() - self._last_used > self.IDLE_PING_S:
                try:
                    await asyncio.wait_for(session.send_ping(), self.PING_TIMEOUT_S)
                except Exception as e:
                    logger.info(f"🔌 idle MCP session failed its ping ({e!r}); reopening")
                    alive = False
            if not alive:
                await self._close()
                session = await self._open()
            self._last_used = time.monotonic()
            return session

    async def _streamable_http_tool_wrapper(self, params) -> None:  # type: ignore[override]
        session = None
        for attempt in (1, 2):
            try:
                session = await self._acquire()
                break
            except Exception as e:
                logger.warning(f"⚠️ MCP session unavailable (attempt {attempt}): {e!r}")
                await self._close()
        if session is None:
            await params.result_callback(
                f"Error calling Home Assistant tool {params.function_name}: Home Assistant is not reachable."
            )
            return
        try:
            await asyncio.wait_for(
                self._call_tool(session, params.function_name, params.arguments, params.result_callback),
                self.CALL_TIMEOUT_S,
            )
            self._last_used = time.monotonic()
        except Exception as e:
            logger.warning(f"⚠️ MCP tool {params.function_name} failed after sending: {e!r}")
            await self._close()
            await params.result_callback(
                f"Error: Home Assistant did not confirm {params.function_name}; "
                "it may or may not have completed. Say so briefly; do not retry automatically."
            )

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
        self.mcp_client: Optional[MCPClient] = None

    async def initialize(self) -> MCPClient:
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            server_params = StreamableHttpParameters(
                url=self.url,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            persistent = os.environ.get("MCP_PERSISTENT_SESSION", "true").strip().lower() != "false"
            client_class = PersistentMCPClient if persistent else MCPClient
            self.mcp_client = client_class(server_params=server_params)
            logger.info(
                f"✅ Home Assistant MCP Client initialized "
                f"({'persistent session' if persistent else 'session per call'})"
            )
            return self.mcp_client
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise

    def get_client(self) -> Optional[MCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client
