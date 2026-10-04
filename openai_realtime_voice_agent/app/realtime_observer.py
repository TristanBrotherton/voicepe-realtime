"""Observe the OpenAI Realtime socket without changing what pipecat parses.

``ObservedSocket`` wraps the websocket pipecat reads from. For every inbound
message it

* stamps ``last_event_mono`` (positive liveness: the server is talking),
* reports the event type to an observer callback (``response.created`` and
  ``error`` are not otherwise visible to our code), and
* drops event types pipecat 0.0.97 cannot parse. Its ``parse_server_event``
  raises on any unknown type, which kills the receive loop and forces a
  reconnect — so a single new server event type would otherwise bounce every
  session.

Attribute access falls through to the real socket, so ``send``, ``close`` and
``ping`` behave exactly as before.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"]+)"')


def extract_event_type(message: Any) -> str:
    """Cheaply read the event type without decoding (large) audio payloads."""
    if isinstance(message, (bytes, bytearray)):
        head = bytes(message[:200]).decode("utf-8", "ignore")
    else:
        head = str(message)[:200]
    match = _TYPE_RE.search(head)
    if match:
        return match.group(1)
    # "type" is normally the first key, but stay correct if it is not.
    match = _TYPE_RE.search(message if isinstance(message, str) else head)
    return match.group(1) if match else ""


class ObservedSocket:
    def __init__(
        self,
        socket: Any,
        on_event: Optional[Callable[[str, Any], None]] = None,
        known_types: Optional[Iterable[str]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._socket = socket
        self._on_event = on_event
        self._known = set(known_types) if known_types is not None else None
        self._clock = clock
        self._warned: set = set()
        self.last_event_mono = clock()
        self.events_seen = 0

    @property
    def wrapped(self) -> Any:
        return self._socket

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        async for message in self._socket:
            self.last_event_mono = self._clock()
            self.events_seen += 1
            event_type = extract_event_type(message)
            if self._on_event is not None:
                try:
                    self._on_event(event_type, message)
                except Exception as e:  # observers must never break the read loop
                    logger.debug(f"realtime observer failed on {event_type}: {e!r}")
            if self._known is not None and event_type not in self._known:
                if event_type not in self._warned:
                    self._warned.add(event_type)
                    logger.info(f"ℹ️ ignoring unsupported realtime event type {event_type!r}")
                continue
            yield message
