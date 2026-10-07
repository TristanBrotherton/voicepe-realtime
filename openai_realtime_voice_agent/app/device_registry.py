"""Per-device connection state and default-target lookup.

Explicit targets never fall back. Without a target, the most recently active
connection is selected.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

# Device ids appear in logs and in tool arguments, so keep them short and
# printable rather than trusting whatever a device sends.
MAX_DEVICE_ID_LEN = 64
_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
)


def sanitize_device_id(raw: str) -> str:
    """Reduce an untrusted device id to a short printable token.

    Args:
        raw: The candidate id, typically from a query string.

    Returns:
        The sanitized id, or "" if nothing usable remained.
    """
    cleaned = "".join(c for c in (raw or "").strip() if c in _SAFE_CHARS)
    return cleaned[:MAX_DEVICE_ID_LEN]


def device_id_from_websocket(websocket: Any) -> str:
    """Derive a best-effort device identifier for a connection.

    Prefers an explicit `?device_id=` from the connection URL, because the
    fallback — the client IP — is not stable: it changes on DHCP renewal
    (orphaning that device's cached conversation context) and collapses two
    devices behind the same NAT into one identity.

    Args:
        websocket: The FastAPI/starlette WebSocket.

    Returns:
        A non-empty device id.
    """
    # Query string first: the firmware can set it in its WebSocket path.
    try:
        query = websocket.url.query if getattr(websocket, "url", None) else ""
        if query:
            values = parse_qs(query).get("device_id") or []
            if values:
                explicit = sanitize_device_id(values[0])
                if explicit:
                    return explicit
    except Exception as e:  # pragma: no cover - defensive, never fatal
        logger.debug(f"could not read device_id from query string: {e!r}")

    client = getattr(websocket, "client", None)
    host = getattr(client, "host", None) if client else None
    if host:
        return sanitize_device_id(host) or "unknown"

    logger.warning("⚠️ no device_id and no client host; using 'unknown'")
    return "unknown"


@dataclass
class DeviceConnection:
    """Everything owned by one connected device.

    Each field that used to be a singleton on WebSocketHandler or Application
    lives here instead, so two devices never share one.
    """

    device_id: str
    websocket: Any
    serializer: Any = None
    transport: Any = None
    maskin: Any = None  # app/session_state.SessionMaskin, set by build_pipeline
    openai_service: Any = None
    # Which engine this connection's session actually runs ("openai" or
    # "gemini"), decided once by WebSocketHandler.serve_connection before the
    # transport is even built, and only ever read back afterwards (by
    # Application.create_service, and by build_pipeline) -- never re-decided.
    # The source of truth for which engine is live -- not the router's
    # current answer, which can move on before this is read.
    provider: str = ""
    pipeline: Any = None
    task: Any = None
    runner: Any = None
    speaker_probe: Any = None
    turn_liveness: Any = None
    connected_at: float = field(default_factory=time.monotonic)
    # A reconnect must not become the implicit target until the user interacts
    # with it; `connected_at` remains available for connection diagnostics.
    last_active: float = 0.0
    recovery: Any = None
    phase_emitter: Any = None
    records_audio: bool = False
    # The background task publishing this connection's provider sensor to
    # Home Assistant (see WebSocketHandler.serve_connection). Held here, on
    # the per-connection object, so asyncio's weak reference to a running
    # task can't let it be garbage-collected mid-publish, and so a second
    # connection's task can never clobber this one's. Cleared to None by
    # WebSocketHandler._publish_provider_status itself once the publish is
    # actually done, so nothing keeps a finished task referenced forever.
    provider_status_task: Any = None
    # Set when the HA tool fetch failed. The next wake refetches
    # (Application.hamta_verktyg_vid_vakning). A list HA returned is kept,
    # even when it is shorter. The speaker is not closed.
    ha_verktyg_saknas: bool = False
    # Kept so a teardown from an older session shape can still cancel it.
    ha_tools_task: Any = None
    # Announcements playing on this device right now, and when the last one
    # ended (Application._guarded_say). They have no phase and no wake, so
    # without these the HA-recovery recycle could cut one off (raawr D-72).
    says_playing: int = 0
    last_busy: float = 0.0

    def touch(self) -> None:
        """Mark this device as the most recently used one."""
        self.last_active = time.monotonic()

    async def send_json(self, obj: dict) -> bool:
        """Send one JSON control frame to this device.

        Uses the transport client when available so control frames share its
        send lock. Firmware phase parsing requires compact JSON.

        Args:
            obj: The object to serialize.

        Returns:
            True if the frame was handed to the socket.
        """
        payload = json.dumps(obj, separators=(",", ":"))
        try:
            transport = self.transport
            client = getattr(transport, "client", None) if transport else None
            if client is not None:
                await client.send(payload)
            else:
                await self.websocket.send_text(payload)
            return True
        except Exception as e:
            logger.warning(f"⚠️ send to {self.device_id} failed: {e!r}")
            return False

    async def send_phase(self, value: str) -> bool:
        """Send a va_client phase message to this device only.

        Args:
            value: One of listening/thinking/replying/idle.

        Returns:
            True if the frame was handed to the socket.
        """
        return await self.send_json({"type": "phase", "value": value})


class DeviceRegistry:
    """The set of currently connected devices, and how to pick one."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._devices: Dict[str, DeviceConnection] = {}
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        """Number of connected devices."""
        return len(self._devices)

    def __iter__(self) -> Iterator[DeviceConnection]:
        """Iterate over a snapshot of the connections."""
        return iter(list(self._devices.values()))

    async def add(self, connection: DeviceConnection) -> Optional[DeviceConnection]:
        """Register a connection, replacing any stale one with the same id.

        A device that reconnects without its previous socket having been
        reaped would otherwise leave an orphan entry that `resolve()` could
        hand out. The displaced connection is returned so the caller can tear
        down its pipeline and session.

        Args:
            connection: The new connection.

        Returns:
            The previous connection for that device id, if any.
        """
        async with self._lock:
            previous = self._devices.get(connection.device_id)
            self._devices[connection.device_id] = connection
        if previous is not None:
            logger.info(
                f"↩️ device {connection.device_id} reconnected; replacing its previous session"
            )
        return previous

    async def remove(self, connection: DeviceConnection) -> bool:
        """Deregister a connection.

        Removes by identity, not just by id: a slow disconnect callback for an
        old socket must not evict the fresh connection that replaced it.

        Args:
            connection: The connection to remove.

        Returns:
            True if this exact connection was still registered.
        """
        async with self._lock:
            current = self._devices.get(connection.device_id)
            if current is not connection:
                return False
            del self._devices[connection.device_id]
            return True

    def get(self, device_id: str) -> Optional[DeviceConnection]:
        """Look up one device by exact id.

        Args:
            device_id: The id to find.

        Returns:
            The connection, or None if that device is not connected.
        """
        return self._devices.get(device_id)

    def resolve(self, device_id: Optional[str] = None) -> Optional[DeviceConnection]:
        """Pick the device a single-device feature should act on.

        Args:
            device_id: An explicit target, or None for the most recently
                active device.

        Returns:
            The chosen connection, or None if there is no match. An explicit
            id that is not connected deliberately returns None instead of
            silently falling back to another room.
        """
        if device_id:
            return self._devices.get(sanitize_device_id(device_id))
        if not self._devices:
            return None
        # A single-device instance has no ambiguity: it must be addressable
        # immediately after reconnect, before the first wake/audio frame has
        # had a chance to call touch(). This is required for timer and HTTP
        # announcements delivered just after a device reboot.
        if len(self._devices) == 1:
            return next(iter(self._devices.values()))
        connection = max(self._devices.values(), key=lambda c: c.last_active)
        return connection if connection.last_active > 0 else None

    def ids(self) -> list[str]:
        """The ids of every connected device, for tool descriptions and logs."""
        return sorted(self._devices.keys())

    async def broadcast_json(self, obj: dict) -> int:
        """Send one JSON frame to every connected device.

        Args:
            obj: The object to serialize.

        Returns:
            How many devices accepted it.
        """
        sent = 0
        for connection in self:
            if await connection.send_json(obj):
                sent += 1
        return sent
