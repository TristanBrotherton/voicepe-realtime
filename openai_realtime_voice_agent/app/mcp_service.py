"""Home Assistant MCP integration on pipecat's persistent MCPClient.

pipecat 1.x's ``MCPClient`` keeps ONE session open in a dedicated owner task
(the MCP SDK's transports must be entered and exited in the same task) and
reuses it for tool calls from every device — which is what the 0.0.x-era
``PersistentMCPClient`` had to build itself. What stays ours is the policy
around each call, applied by ``guarded_handler`` on top of the handler the
schema from ``MCPClient.tools()`` carries:

* A session that has been idle longer than ``IDLE_PING_S`` is pinged first
  (an MCP ping on the live session, bounded by ``PING_TIMEOUT_S``); failures
  to open or probe a session reopen it and are retried once — nothing has
  been sent to Home Assistant yet at that point.
* A call is bounded by ``CALL_TIMEOUT_S``.
* Once the tool call itself has been sent, a failure is NOT retried: the
  action may already have happened, and a retry could do it twice. The model
  gets an honest "may or may not have completed" error instead of pipecat's
  "Error calling mcp tool …" line, which reads as a safe-to-retry failure.
* The one session is shared by every device (parallel tool calls, two rooms
  at once), so it is never closed under a call that is still in flight: a
  failed call retires it once the last call on it has finished (a call
  arriving before then pings it first), and ``MCP_PERSISTENT_SESSION=false``
  closes it whenever no call is in flight — pipecat 1.x has no per-call mode
  of its own, and calls that overlap share the session until the last one is
  done.
* The tool list a new device session needs (``cached_tools``) is bounded by
  ``OPEN_TIMEOUT_S`` and reused for ``TOOLS_CACHE_S``, so a reconnecting
  device neither waits on a slow Home Assistant nor holds pipecat's session
  lock — which every device's calls go through — while it opens the session.
"""
import asyncio
import logging
import os
import time
from typing import Optional

# _NotConnectedError is pipecat-internal (1.13): what MCPClient raises when a
# tool call reaches it with no session open — before anything was sent.
# Re-check on upgrade.
from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters, _NotConnectedError

from app.tool_guards import intercept_result_callback

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
    TOOLS_CACHE_S = 300.0

    def __init__(self, server_params, *, persistent: bool = True, **kwargs):
        super().__init__(server_params, **kwargs)
        self._persistent = persistent
        self._open_lock: Optional[asyncio.Lock] = None
        self._last_used = 0.0
        self._opened = False
        # Calls between _acquire and _release; the session is never closed
        # by us while this is non-zero.
        self._in_flight = 0
        # A call failed after sending on this session: replace it once
        # nobody is on it any more.
        self._retire = False
        self._tools_cache = None
        self._tools_cached_at = 0.0
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

    def _live_session(self):
        """The ClientSession pipecat's owner task holds open, or None.

        ``_active_session`` is pipecat-internal (1.13): MCPClient offers no
        ping and no handle on its session, and ``tools()`` would open one.
        Re-check on upgrade.
        """
        return self._active_session

    async def _open(self) -> None:
        await asyncio.wait_for(self.start(), self.OPEN_TIMEOUT_S)
        self._note_opened()

    async def _close(self) -> None:
        self._opened = False
        try:
            await asyncio.wait_for(self.close(), 3)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"MCP session close: {e!r}")

    async def _probe(self, session) -> bool:
        """Whether the open session still answers an MCP ping.

        A cheap round trip on the session itself: it opens and reconnects
        nothing, and is bounded by PING_TIMEOUT_S.
        """
        try:
            await asyncio.wait_for(session.send_ping(), self.PING_TIMEOUT_S)
            return True
        except Exception as e:
            logger.info(f"🔌 MCP session failed its ping ({e!r})")
            return False

    async def _acquire(self) -> None:
        """Make sure a healthy session is open and count this call as in flight.

        Raises only before anything was sent. Every close runs under the
        lock, so none can slip between a call's acquire and its send.
        """
        async with self._lock():
            session = self._live_session()
            alive = self._opened and session is not None
            if alive and self._retire and self._in_flight == 0:
                # A failed call retired it and nobody is on it: replace it.
                alive = False
            elif alive and (self._retire or time.monotonic() - self._last_used > self.IDLE_PING_S):
                if not await self._probe(session):
                    logger.info("🔌 reopening the MCP session")
                    alive = False
            if not alive:
                await self._close()
                try:
                    await self._open()
                except Exception:
                    await self._close()
                    raise
            self._retire = False
            self._in_flight += 1
            self._last_used = time.monotonic()

    async def _release(self, *, failed: bool) -> None:
        """Count a call out; retire or close the session once nobody is on it."""
        self._in_flight -= 1
        if failed:
            self._retire = True
        if self._in_flight == 0 and (self._retire or not self._persistent):
            async with self._lock():
                # Re-checked under the lock: a call may have joined meanwhile.
                if self._in_flight == 0 and (self._retire or not self._persistent):
                    await self._close()
                    self._retire = False

    async def tools(self):
        """The tool schemas; opens the session when none is open."""
        schema = await super().tools()
        self._note_opened()
        return schema

    async def cached_tools(self):
        """The tool schemas for a new device session.

        A listing younger than TOOLS_CACHE_S is reused, so a device that
        reconnects (follow-up windows, keepalive drops) does not list again.
        A fresh listing is bounded by OPEN_TIMEOUT_S: ``tools()`` opens the
        session under pipecat's lifecycle lock, which every device's tool
        calls wait on, so a slow Home Assistant must not hold it for longer.
        """
        if self._tools_cache is not None and time.monotonic() - self._tools_cached_at < self.TOOLS_CACHE_S:
            return self._tools_cache
        schema = await asyncio.wait_for(self.tools(), self.OPEN_TIMEOUT_S)
        self._tools_cache, self._tools_cached_at = schema, time.monotonic()
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
        honest = (
            f"Error: Home Assistant did not confirm {name}; "
            "it may or may not have completed. Say so briefly; do not retry automatically."
        )
        failed_after_send = False

        def on_result(result):
            nonlocal failed_after_send
            if isinstance(result, str) and result.startswith(PIPECAT_CALL_ERROR_PREFIX):
                logger.warning(f"⚠️ MCP tool {name} failed after sending: {result}")
                failed_after_send = True
                return honest
            return result

        guarded_params = intercept_result_callback(params, on_result)

        for attempt in (1, 2):
            try:
                await self._acquire()
            except Exception as e:
                logger.warning(f"⚠️ MCP session unavailable (attempt {attempt}): {e!r}")
                continue
            try:
                await asyncio.wait_for(call_tool(guarded_params), self.CALL_TIMEOUT_S)
            except _NotConnectedError:
                # The call reached pipecat after its session was closed
                # (its own drop of a dead session; our closes wait for the
                # calls in flight). Nothing was sent: open again, retry once.
                logger.warning(f"⚠️ MCP session closed before {name} was sent (attempt {attempt})")
                await self._release(failed=False)
                continue
            except asyncio.CancelledError:
                self._in_flight -= 1
                raise
            except Exception as e:
                logger.warning(f"⚠️ MCP tool {name} failed after sending: {e!r}")
                failed_after_send = True
                await params.result_callback(honest)
            await self._release(failed=failed_after_send)
            return
        await params.result_callback(
            f"Error calling Home Assistant tool {name}: Home Assistant is not reachable."
        )

    async def aclose(self) -> None:
        async with self._lock():
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
                "✅ Home Assistant MCP Client initialized (one session shared by every device, "
                + ("kept open" if persistent else "closed whenever no call is in flight")
                + ")"
            )
            return self.mcp_client
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise

    def get_client(self) -> Optional[PersistentMCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client
