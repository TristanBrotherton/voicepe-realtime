"""Home Assistant MCP integration on pipecat's persistent MCPClient.

pipecat 1.x's ``MCPClient`` keeps ONE session open in a dedicated owner task
(the MCP SDK's transports must be entered and exited in the same task),
reuses it for tool calls from every device, and — since 1.11 — drops a
session whose transport died and connects again on the next call. What
stays ours is the policy around each call, applied by ``guarded_handler``
on top of the handler the schema from ``MCPClient.tools()`` carries:

* A session that has been idle longer than ``IDLE_PING_S`` is pinged first
  (an MCP ping on the live session, bounded by ``PING_TIMEOUT_S``). A dead
  idle session is reopened before anything is sent — but only while no
  call is on it; otherwise pipecat's own reconnect takes over, and the
  shared session is never closed under another device's call.
* A call is bounded by ``CALL_TIMEOUT_S``; a result reaches the model
  exactly once.
* Once the tool call itself has been sent, a failure is NOT retried: the
  action may already have happened, and a retry could do it twice. The model
  gets an honest "may or may not have completed" error instead of pipecat's
  "Error calling mcp tool …" line, which reads as a safe-to-retry failure.
* The tool list a new device session needs (``cached_tools``) is bounded by
  ``OPEN_TIMEOUT_S`` and reused for ``TOOLS_CACHE_S``; when a refresh fails
  the last listing is kept, so a reconnecting device never loses its tools
  to a slow Home Assistant.

``MCP_PERSISTENT_SESSION=false`` gives each call its own client and session
(``PerCallMCPClient``), as pipecat 0.0.x did: nothing is shared, so nothing
can be closed under another call.
"""
import asyncio
import logging
import os
import time
from typing import Optional, Union

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

CALL_TIMEOUT_S = 45.0
OPEN_TIMEOUT_S = 15.0
TOOLS_CACHE_S = 300.0


def not_reachable(name: str) -> str:
    return f"Error calling Home Assistant tool {name}: Home Assistant is not reachable."


def not_confirmed(name: str) -> str:
    return (
        f"Error: Home Assistant did not confirm {name}; "
        "it may or may not have completed. Say so briefly; do not retry automatically."
    )


class _GuardedCall:
    """One tool call: its result reaches the model exactly once.

    pipecat's wrapper delivers through the intercepted callback; a failure
    it reports as a result string becomes the honest wording. Our own
    deliveries (not reachable, not confirmed) are skipped once anything
    has been delivered.
    """

    def __init__(self, params):
        self.name = params.function_name
        self._original = params.result_callback
        self.delivered = False
        self.params = intercept_result_callback(params, self._on_result)

    def _on_result(self, result):
        self.delivered = True
        if isinstance(result, str) and result.startswith(PIPECAT_CALL_ERROR_PREFIX):
            logger.warning(f"⚠️ MCP tool {self.name} failed after sending: {result}")
            return not_confirmed(self.name)
        return result

    async def deliver(self, result) -> None:
        if self.delivered:
            return
        self.delivered = True
        await self._original(result)


async def _close_quietly(client: MCPClient) -> None:
    try:
        await asyncio.wait_for(client.close(), 3)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"MCP session close: {e!r}")


def _handler_for(schema, name):
    for function_schema in schema.standard_tools:
        if function_schema.name == name:
            return function_schema.handler
    return None


class _ToolsCache:
    """A tool listing reused for TOOLS_CACHE_S; kept when a refresh fails."""

    TOOLS_CACHE_S = TOOLS_CACHE_S

    def __init__(self):
        self._tools_cache = None
        self._tools_cached_at = 0.0

    async def cached_tools(self):
        """The tool schemas for a new device session.

        A listing younger than TOOLS_CACHE_S is reused, so a device that
        reconnects (follow-up windows, keepalive drops) does not list again.
        A refresh that fails or times out keeps the last listing.
        """
        if self._tools_cache is not None and time.monotonic() - self._tools_cached_at < self.TOOLS_CACHE_S:
            return self._tools_cache
        try:
            schema = await self._list_tools()
        except Exception as e:
            if self._tools_cache is None:
                raise
            logger.warning(f"⚠️ MCP tool listing failed ({e!r}); keeping the previous list")
            return self._tools_cache
        self._tools_cache, self._tools_cached_at = schema, time.monotonic()
        return schema

    async def _list_tools(self):
        raise NotImplementedError


class PersistentMCPClient(_ToolsCache, MCPClient):
    CALL_TIMEOUT_S = CALL_TIMEOUT_S
    OPEN_TIMEOUT_S = OPEN_TIMEOUT_S
    IDLE_PING_S = 60.0
    PING_TIMEOUT_S = 3.0

    def __init__(self, server_params, **kwargs):
        _ToolsCache.__init__(self)
        MCPClient.__init__(self, server_params, **kwargs)
        self._open_lock: Optional[asyncio.Lock] = None
        self._last_used = 0.0
        # Calls and listings between _acquire and their end: the session is
        # never closed by us while this is non-zero.
        self._in_flight = 0
        self.session_opens = 0

    def _lock(self) -> asyncio.Lock:
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        return self._open_lock

    def _live_session(self):
        """The ClientSession pipecat's owner task holds open, or None.

        ``_active_session`` is pipecat-internal (1.13): MCPClient offers no
        ping and no handle on its session, and ``tools()`` would open one.
        None also after pipecat dropped a dead session; ``start()`` then
        connects again. Re-check on upgrade.
        """
        return self._active_session

    async def _open(self) -> None:
        await asyncio.wait_for(self.start(), self.OPEN_TIMEOUT_S)
        self.session_opens += 1
        logger.info(f"🔗 Home Assistant MCP session opened (#{self.session_opens})")

    async def _probe(self, session) -> bool:
        """Whether the open session still answers an MCP ping.

        A cheap round trip on the session itself: it opens and reconnects
        nothing, and is bounded by PING_TIMEOUT_S.
        """
        try:
            await asyncio.wait_for(session.send_ping(), self.PING_TIMEOUT_S)
            return True
        except Exception as e:
            logger.info(f"🔌 idle MCP session failed its ping ({e!r})")
            return False

    async def _acquire(self) -> None:
        """Make sure a session is open and count the caller as in flight.

        Raises only before anything was sent. The one close in here runs
        under the lock and only while nothing is in flight.
        """
        async with self._lock():
            session = self._live_session()
            if session is None:
                await self._open()
            elif time.monotonic() - self._last_used > self.IDLE_PING_S and not await self._probe(session):
                if self._in_flight == 0:
                    logger.info("🔌 reopening the idle MCP session")
                    await _close_quietly(self)
                    await self._open()
                else:
                    logger.info(
                        f"🔌 {self._in_flight} call(s) still on the MCP session; "
                        "left to pipecat's reconnect"
                    )
            self._in_flight += 1
            self._last_used = time.monotonic()

    async def _list_tools(self):
        await self._acquire()
        try:
            return await asyncio.wait_for(self.tools(), self.OPEN_TIMEOUT_S)
        finally:
            self._in_flight -= 1

    def guarded_handler(self, function_schema):
        """The call handler for one tool, with the session policy around it.

        Args:
            function_schema: One of ``(await self.cached_tools()).standard_tools``;
                its ``handler`` is pipecat's own tool wrapper.
        """
        call_tool = function_schema.handler
        if call_tool is None:
            raise ValueError(f"MCP tool {function_schema.name} carries no handler")

        async def handler(params) -> None:
            await self._guarded_call(call_tool, params)

        return handler

    async def _guarded_call(self, call_tool, params) -> None:
        call = _GuardedCall(params)
        for attempt in (1, 2):
            try:
                await self._acquire()
            except Exception as e:
                logger.warning(f"⚠️ MCP session unavailable (attempt {attempt}): {e!r}")
                continue
            try:
                await asyncio.wait_for(call_tool(call.params), self.CALL_TIMEOUT_S)
                self._last_used = time.monotonic()
                return
            except _NotConnectedError:
                # The call reached pipecat after its session was closed
                # (its own drop of a dead session; we never close under a
                # call). Nothing was sent: open again, retry once.
                logger.warning(f"⚠️ MCP session closed before {call.name} was sent (attempt {attempt})")
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ MCP tool {call.name} failed after sending: {e!r}")
                await call.deliver(not_confirmed(call.name))
                return
            finally:
                self._in_flight -= 1
        await call.deliver(not_reachable(call.name))

    async def aclose(self) -> None:
        async with self._lock():
            await _close_quietly(self)


class PerCallMCPClient(_ToolsCache):
    """A client and session per call (``MCP_PERSISTENT_SESSION=false``).

    Opening and listing are pre-send: a failure there reports Home
    Assistant as not reachable. The call runs through the handler that
    session's own listing carries — the one public way to call a tool on a
    pipecat MCPClient — and the session is closed afterwards whatever
    happened.
    """

    CALL_TIMEOUT_S = CALL_TIMEOUT_S
    OPEN_TIMEOUT_S = OPEN_TIMEOUT_S

    def __init__(self, server_params):
        super().__init__()
        self._server_params = server_params

    async def _list_tools(self):
        client = MCPClient(self._server_params)
        try:
            return await asyncio.wait_for(client.tools(), self.OPEN_TIMEOUT_S)
        finally:
            await _close_quietly(client)

    def guarded_handler(self, function_schema):
        name = function_schema.name

        async def handler(params) -> None:
            call = _GuardedCall(params)
            client = MCPClient(self._server_params)
            try:
                try:
                    schema = await asyncio.wait_for(client.tools(), self.OPEN_TIMEOUT_S)
                    call_tool = _handler_for(schema, name)
                    if call_tool is None:
                        raise LookupError(f"Home Assistant does not offer {name}")
                except Exception as e:
                    logger.warning(f"⚠️ MCP session unavailable for {name}: {e!r}")
                    await call.deliver(not_reachable(name))
                    return
                try:
                    await asyncio.wait_for(call_tool(call.params), self.CALL_TIMEOUT_S)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"⚠️ MCP tool {name} failed after sending: {e!r}")
                    await call.deliver(not_confirmed(name))
            finally:
                await _close_quietly(client)

        return handler

    async def aclose(self) -> None:
        return None


HomeAssistantMCPClient = Union[PersistentMCPClient, PerCallMCPClient]


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
        self.mcp_client: Optional[HomeAssistantMCPClient] = None

    async def initialize(self) -> HomeAssistantMCPClient:
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            server_params = StreamableHttpParameters(
                url=self.url,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            persistent = os.environ.get("MCP_PERSISTENT_SESSION", "true").strip().lower() != "false"
            if persistent:
                self.mcp_client = PersistentMCPClient(server_params=server_params)
                mode = "one session shared by every device, kept open"
            else:
                self.mcp_client = PerCallMCPClient(server_params)
                mode = "a session per call, opened and closed around it"
            logger.info(f"✅ Home Assistant MCP Client initialized ({mode})")
            return self.mcp_client
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise

    def get_client(self) -> Optional[HomeAssistantMCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client
