"""Home Assistant MCP integration with a reusable tool session.

One session, opened on first use in a dedicated owner task (the MCP SDK's
transports must be entered and exited in the same task) and reused for tool
calls from every device. This talks to the MCP SDK directly (``mcp.client``);
pipecat's own MCP client is not involved, so none of its internals are
reached. Tools reach pipecat through its public schema classes: the listing
becomes a ``ToolsSchema`` of plain ``FunctionSchema``s, and main.py registers
our handler per tool (``guarded_handler``) with ``register_function``.

Safety rules:
* A session that has been idle longer than ``IDLE_PING_S`` is pinged first;
  failures to acquire or ping a session reopen it and are retried once —
  nothing has been sent to Home Assistant yet at that point.
* Once the tool call itself has been sent, a failure is NOT retried: the
  action may already have happened, and a retry could do it twice. The model
  gets an honest "may or may not have completed" error instead, and the
  session is closed so the next call starts on a fresh one (after a Home
  Assistant restart the old session answers "Session terminated").
* The tool list a new device session needs is bounded by ``OPEN_TIMEOUT_S``
  and reused for ``TOOLS_CACHE_S``; a failed listing keeps the previous list
  (when there is one) and is not tried again for ``TOOLS_RETRY_S``.
* ``MCP_PERSISTENT_SESSION=false`` restores the per-call behaviour.
"""
import asyncio
import contextlib
import logging
import os
import time
from typing import Optional

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

logger = logging.getLogger(__name__)

CALL_TIMEOUT_S = 45.0
OPEN_TIMEOUT_S = 15.0
TOOLS_CACHE_S = 300.0
TOOLS_RETRY_S = 30.0


def not_reachable(name: str) -> str:
    return f"Error calling Home Assistant tool {name}: Home Assistant is not reachable."


def not_confirmed(name: str) -> str:
    return (
        f"Error: Home Assistant did not confirm {name}; "
        "it may or may not have completed. Say so briefly; do not retry automatically."
    )


@contextlib.asynccontextmanager
async def open_session(url: str, headers: dict):
    """A streamable-HTTP transport and an initialised ClientSession.

    Entered and exited in the calling task, as the SDK requires. The HTTP
    client carries the headers and the SDK's recommended timeouts (a server
    may hold a response stream open).
    """
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def tools_schema(listed) -> ToolsSchema:
    """pipecat's public tool schema for the tools a listing returned."""
    schemas = []
    for tool in listed.tools:
        input_schema = tool.input_schema or {}
        schemas.append(FunctionSchema(
            name=tool.name,
            description=tool.description or "",
            properties=input_schema.get("properties", {}),
            required=input_schema.get("required", []),
        ))
    return ToolsSchema(standard_tools=schemas)


def result_text(result) -> str:
    """The text of a tool result, as pipecat's MCP client reads it."""
    text = "".join(c.text for c in (result.content or []) if getattr(c, "text", None))
    return text or "Sorry, could not call the mcp tool"


class ToolListCache:
    """The tool listing a new device session needs, reused and bounded."""

    OPEN_TIMEOUT_S = OPEN_TIMEOUT_S
    TOOLS_CACHE_S = TOOLS_CACHE_S
    TOOLS_RETRY_S = TOOLS_RETRY_S

    def __init__(self):
        self._tools_cache: Optional[ToolsSchema] = None
        self._tools_cached_at = 0.0
        self._tools_failed_at = 0.0

    async def cached_tools(self) -> ToolsSchema:
        """A listing younger than TOOLS_CACHE_S, else a fresh one bounded by
        OPEN_TIMEOUT_S. A failed listing keeps the previous list (when there
        is one) and is not tried again for TOOLS_RETRY_S."""
        now = time.monotonic()
        if self._tools_cache is not None and now - self._tools_cached_at < self.TOOLS_CACHE_S:
            return self._tools_cache
        if now - self._tools_failed_at < self.TOOLS_RETRY_S:
            if self._tools_cache is not None:
                return self._tools_cache
            raise ConnectionError("Home Assistant MCP tool listing failed recently; not retried yet")
        try:
            schema = await asyncio.wait_for(self._list_tools(), self.OPEN_TIMEOUT_S)
        except Exception as e:
            self._tools_failed_at = time.monotonic()
            if self._tools_cache is None:
                raise
            logger.warning(f"⚠️ MCP tool listing failed ({e!r}); keeping the previous list")
            return self._tools_cache
        self._tools_cache, self._tools_cached_at = schema, time.monotonic()
        return schema

    async def _list_tools(self) -> ToolsSchema:
        raise NotImplementedError


class PersistentMCPClient(ToolListCache):
    CALL_TIMEOUT_S = CALL_TIMEOUT_S
    IDLE_PING_S = 60.0
    PING_TIMEOUT_S = 3.0

    def __init__(self, url: str, headers: dict):
        super().__init__()
        self._url = url
        self._headers = headers
        self._persistent_session: Optional[ClientSession] = None
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
            async with open_session(self._url, self._headers) as session:
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
            if not ready.done():
                ready.set_exception(ConnectionError("MCP session ended before it was ready"))

    async def _open(self) -> ClientSession:
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

    async def _acquire(self) -> ClientSession:
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

    async def _list_tools(self) -> ToolsSchema:
        session = await self._acquire()
        try:
            listed = await session.list_tools()
        except Exception:
            await self._close()
            raise
        self._last_used = time.monotonic()
        return tools_schema(listed)

    def guarded_handler(self, function_schema: FunctionSchema):
        """The call handler for one tool, with the session policy around it."""
        name = function_schema.name

        async def handler(params) -> None:
            await self._call(name, params)

        return handler

    async def _call(self, name: str, params) -> None:
        session = None
        for attempt in (1, 2):
            try:
                session = await self._acquire()
                break
            except Exception as e:
                logger.warning(f"⚠️ MCP session unavailable (attempt {attempt}): {e!r}")
                await self._close()
        if session is None:
            await params.result_callback(not_reachable(name))
            return
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments=params.arguments), self.CALL_TIMEOUT_S
            )
            self._last_used = time.monotonic()
            response = result_text(result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ MCP tool {name} failed after sending: {e!r}")
            await self._close()
            response = not_confirmed(name)
        await params.result_callback(response)

    async def aclose(self) -> None:
        await self._close()


class PerCallMCPClient(ToolListCache):
    """A session per call (``MCP_PERSISTENT_SESSION=false``)."""

    CALL_TIMEOUT_S = CALL_TIMEOUT_S

    def __init__(self, url: str, headers: dict):
        super().__init__()
        self._url = url
        self._headers = headers

    async def _list_tools(self) -> ToolsSchema:
        async with open_session(self._url, self._headers) as session:
            return tools_schema(await session.list_tools())

    def guarded_handler(self, function_schema: FunctionSchema):
        name = function_schema.name

        async def handler(params) -> None:
            sent = False

            async def run() -> str:
                nonlocal sent
                async with open_session(self._url, self._headers) as session:
                    sent = True
                    result = await asyncio.wait_for(
                        session.call_tool(name, arguments=params.arguments), self.CALL_TIMEOUT_S
                    )
                    return result_text(result)

            try:
                response = await asyncio.wait_for(run(), self.OPEN_TIMEOUT_S + self.CALL_TIMEOUT_S + 3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if sent:
                    logger.warning(f"⚠️ MCP tool {name} failed after sending: {e!r}")
                    response = not_confirmed(name)
                else:
                    logger.warning(f"⚠️ MCP session unavailable for {name}: {e!r}")
                    response = not_reachable(name)
            await params.result_callback(response)

        return handler

    async def aclose(self) -> None:
        return None


class HomeAssistantMCPService:
    """Home Assistant MCP service on the add-on's own MCP session."""

    def __init__(self, url: str, access_token: str):
        """
        Args:
            url: Home Assistant MCP Server URL (e.g., http://supervisor/core/api/mcp)
            access_token: Long-lived access token for Home Assistant
        """
        self.url = url
        self.access_token = access_token
        self.mcp_client = None

    async def initialize(self):
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            headers = {"Authorization": f"Bearer {self.access_token}"}
            persistent = os.environ.get("MCP_PERSISTENT_SESSION", "true").strip().lower() != "false"
            client_class = PersistentMCPClient if persistent else PerCallMCPClient
            self.mcp_client = client_class(self.url, headers)
            logger.info(
                f"✅ Home Assistant MCP Client initialized "
                f"({'persistent session' if persistent else 'session per call'})"
            )
            return self.mcp_client
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise

    def get_client(self):
        """Get the MCP client instance."""
        return self.mcp_client
