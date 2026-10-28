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
  gets an honest "may or may not have completed" error instead. A call whose
  session was already closed never left this process and is retried once.
* A session that failed is retired: no new call uses it, and it closes once
  the calls still running on it finish, never under another device's call.
* ``MCP_PERSISTENT_SESSION=false`` restores the per-call behaviour.
"""
import asyncio
import logging
import os
import time
from typing import Optional

import anyio

from app.tool_results import classify_result
from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

logger = logging.getLogger(__name__)


class _Session:
    """One MCP session, its owner task and the calls running on it."""

    def __init__(self):
        self.session = None
        self.owner: Optional[asyncio.Task] = None
        self.stop = asyncio.Event()
        self.in_flight = 0
        self.retired = False

    def alive(self) -> bool:
        return self.session is not None and self.owner is not None and not self.owner.done()


class _WatchedSession:
    """The session as pipecat's ``_call_tool`` sees it, keeping the SDK's own
    exception: ``_call_tool`` swallows it and then fails on its unbound result."""

    def __init__(self, session):
        self._inner = session
        self.error: Optional[BaseException] = None

    async def call_tool(self, *args, **kwargs):
        try:
            return await self._inner.call_tool(*args, **kwargs)
        except BaseException as e:
            self.error = e
            raise


def mark_error(result) -> None:
    """pipecat passes on only the text of an MCP result, so a result flagged
    ``isError`` ("Input validation error: ...") read as a success. Its first
    text is prefixed "Error: " unless it is already judged a failure
    (app/tool_results.py). Kitchen 2026-10-28: "Garden lights are off" for a
    call Home Assistant had rejected."""
    if not getattr(result, "isError", False):
        return
    for content in getattr(result, "content", None) or []:
        text = getattr(content, "text", None)
        if isinstance(text, str):
            if classify_result(text).ok:
                content.text = "Error: " + text
            return


class _ErrorMarkingSession:
    """A session whose results have ``isError`` written into their text."""

    def __init__(self, session):
        self._inner = session

    async def call_tool(self, *args, **kwargs):
        result = await self._inner.call_tool(*args, **kwargs)
        mark_error(result)
        return result


class ErrorMarkingMCPClient(MCPClient):
    """pipecat's MCP client with ``isError`` kept (see :func:`mark_error`).
    Both the persistent client and the per-call one
    (``MCP_PERSISTENT_SESSION=false``) are built on it."""

    async def _call_tool(self, session, function_name, arguments, result_callback):
        return await super()._call_tool(_ErrorMarkingSession(session), function_name, arguments,
                                        result_callback)


def _never_sent(error: Optional[BaseException]) -> bool:
    """The request never entered the transport: its stream was already closed."""
    return isinstance(error, (anyio.ClosedResourceError, anyio.BrokenResourceError))


class PersistentMCPClient(ErrorMarkingMCPClient):
    CALL_TIMEOUT_S = 45.0
    OPEN_TIMEOUT_S = 15.0
    IDLE_PING_S = 60.0
    PING_TIMEOUT_S = 3.0

    def __init__(self, server_params, **kwargs):
        super().__init__(server_params, **kwargs)
        self._current: Optional[_Session] = None
        self._open_lock: Optional[asyncio.Lock] = None
        self._last_used = 0.0
        self.session_opens = 0

    @property
    def _persistent_session(self):
        return self._current.session if self._current is not None else None

    def _lock(self) -> asyncio.Lock:
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        return self._open_lock

    async def _own_session(self, slot: _Session, ready: asyncio.Future) -> None:
        try:
            async with self._client(**self._server_params.model_dump()) as (read, write, _):
                async with self._session(read, write) as session:
                    await session.initialize()
                    self.session_opens += 1
                    slot.session = session
                    if not ready.done():
                        ready.set_result(session)
                    await slot.stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                logger.info(f"🔌 Home Assistant MCP session closed: {e!r}")
        finally:
            slot.session = None

    async def _open(self) -> _Session:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        slot = _Session()
        slot.owner = asyncio.create_task(self._own_session(slot, ready))
        try:
            await asyncio.wait_for(ready, self.OPEN_TIMEOUT_S)
        except BaseException:
            await self._shut(slot)
            raise
        self._current = slot
        self._last_used = time.monotonic()
        logger.info(f"🔗 Home Assistant MCP session opened (#{self.session_opens})")
        return slot

    @staticmethod
    async def _shut(slot: _Session) -> None:
        slot.stop.set()
        owner, slot.owner = slot.owner, None
        if owner is not None and not owner.done():
            try:
                await asyncio.wait_for(owner, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                owner.cancel()
        slot.session = None

    async def _retire(self, slot: Optional[_Session]) -> None:
        """No new call uses this session; it closes once no call is running on
        it, so a failure never cuts off another device's call in flight."""
        if slot is None:
            return
        slot.retired = True
        if self._current is slot:
            self._current = None
        if slot.in_flight == 0:
            await self._shut(slot)

    async def _close(self) -> None:
        await self._retire(self._current)

    async def _acquire(self) -> _Session:
        """A healthy session, counted as in use; raises only before anything was sent."""
        async with self._lock():
            slot = self._current
            alive = slot is not None and slot.alive()
            if alive and time.monotonic() - self._last_used > self.IDLE_PING_S:
                try:
                    await asyncio.wait_for(slot.session.send_ping(), self.PING_TIMEOUT_S)
                except Exception as e:
                    logger.info(f"🔌 idle MCP session failed its ping ({e!r}); reopening")
                    alive = False
            if not alive:
                await self._retire(slot)
                slot = await self._open()
            self._last_used = time.monotonic()
            slot.in_flight += 1
            return slot

    async def _release(self, slot: _Session) -> None:
        slot.in_flight -= 1
        if slot.retired and slot.in_flight == 0:
            await self._shut(slot)

    async def _streamable_http_tool_wrapper(self, params) -> None:  # type: ignore[override]
        for attempt in (1, 2):
            slot = watched = None
            try:
                slot = await self._acquire()
            except Exception as e:
                logger.warning(f"⚠️ MCP session unavailable (attempt {attempt}): {e!r}")
                await self._close()
                continue
            try:
                if not slot.alive():
                    # Closed between acquire and send: nothing went out.
                    await self._retire(slot)
                    continue
                watched = _WatchedSession(slot.session)
                await asyncio.wait_for(
                    self._call_tool(watched, params.function_name, params.arguments,
                                    params.result_callback),
                    self.CALL_TIMEOUT_S,
                )
                self._last_used = time.monotonic()
                return
            except Exception as e:
                await self._retire(slot)
                if _never_sent(watched.error if watched is not None else None):
                    logger.warning(f"⚠️ MCP tool {params.function_name} not sent, session was closed "
                                   f"(attempt {attempt}): {e!r}")
                    continue
                logger.warning(f"⚠️ MCP tool {params.function_name} failed after sending: {e!r}")
                await params.result_callback(
                    f"Error: Home Assistant did not confirm {params.function_name}; "
                    "it may or may not have completed. Say so briefly; do not retry automatically."
                )
                return
            finally:
                await self._release(slot)
        await params.result_callback(
            f"Error calling Home Assistant tool {params.function_name}: Home Assistant is not reachable."
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
            client_class = PersistentMCPClient if persistent else ErrorMarkingMCPClient
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
