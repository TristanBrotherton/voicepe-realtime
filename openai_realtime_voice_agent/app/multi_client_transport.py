"""Per-connection FastAPI transport for binary audio and text control frames."""

import asyncio
import os
import time
import typing

from loguru import logger
from pipecat.frames.frames import EndFrame
from starlette.websockets import WebSocket, WebSocketState

from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketClient,
    FastAPIWebsocketInputTransport,
    FastAPIWebsocketOutputTransport,
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)


class MixedFastAPIWebsocketClient(FastAPIWebsocketClient):
    """A FastAPI WebSocket client that reads and writes both frame types.

    `receive()` yields whichever payload each frame actually carried, and
    `send()` dispatches on the Python type of the outgoing data rather than on
    a mode fixed at construction time.
    """

    def __init__(self, websocket: WebSocket, callbacks):
        """Initialize the client.

        Args:
            websocket: The FastAPI/starlette WebSocket connection.
            callbacks: The transport's FastAPIWebsocketCallbacks.
        """
        # `is_binary` is inherited but never consulted — both receive() and
        # send() are overridden to decide per message. It is set to True only
        # so any base-class code that reads it assumes the audio path.
        super().__init__(websocket, True, callbacks)
        # The pipeline's output transport writes audio bytes while the phase
        # emitter writes JSON text, from different tasks, to the same socket.
        # starlette does not serialize concurrent sends, so interleaved frames
        # could corrupt the stream; this lock makes each send atomic.
        self._send_lock = asyncio.Lock()

    def receive(self) -> typing.AsyncIterator[bytes | str]:
        """Yield every inbound frame, binary or text.

        Returns:
            An async iterator over raw payloads, ending on disconnect.
        """
        return self._iter_messages()

    async def _iter_messages(self) -> typing.AsyncIterator[bytes | str]:
        while True:
            try:
                message = await self._websocket.receive()
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                # starlette raises RuntimeError if receive() is called once the
                # socket has already gone away. Treat it as end-of-stream so the
                # input transport runs its normal disconnect path.
                break

            if message.get("type") == "websocket.disconnect":
                break

            # A frame carries exactly one of these. Binary is checked first:
            # it is the audio path and by far the hotter of the two.
            payload = message.get("bytes")
            if payload is None:
                payload = message.get("text")
            if payload is not None:
                yield payload

    async def send(self, data: str | bytes):
        """Send one frame, choosing binary or text from the payload type.

        Args:
            data: Audio bytes or a JSON control string.
        """
        try:
            if self._can_send():
                async with self._send_lock:
                    if isinstance(data, (bytes, bytearray, memoryview)):
                        await self._websocket.send_bytes(data)
                    else:
                        await self._websocket.send_text(data)
        except Exception as e:
            logger.error(
                f"{self} exception sending data: {e.__class__.__name__} ({e}), "
                f"application_state: {self._websocket.application_state}"
            )
            # Mirrors the base class: a send failure on an already-dead socket
            # means the transport should start closing rather than retry.
            if (
                self._websocket.application_state == WebSocketState.DISCONNECTED
                and not self.is_closing
            ):
                logger.warning("Closing already disconnected websocket!")
                self._closing = True


class MixedFastAPIWebsocketInputTransport(FastAPIWebsocketInputTransport):
    """End the pipeline when this connection's receive loop ends."""

    async def _receive_messages(self):
        await super()._receive_messages()
        await self.push_frame(EndFrame())


def stadig_takt() -> bool:
    return os.environ.get("STADIG_TAKT", "0") == "1"


STADIG_LEAD_S = float(os.environ.get("STADIG_LEAD_MS", "120")) / 1000    # sent at once at the start of a reply: the device's cushion
STADIG_OMSTART_S = 0.25                                                 # this far behind: the device ran dry anyway, start over


class StadigUtTransport(FastAPIWebsocketOutputTransport):
    """Reply audio at exactly real time, with a small cushion in the device (STADIG_TAKT=1; off = pipecat's own pacer).

    Pipecat's pacer starts its clock over every time a frame is late, so the time lost is never made up and the device
    gets no cushion: every late frame is a hole in what the speaker plays, a few to 60 ms long (measured on the live
    recordings 2026-10-12/13: 70 holes in 29 s of reply, the echo wandering 119-155 ms against what was sent). A voice
    with holes at moments only the device knows cannot be cancelled. Here the schedule is kept (a late frame is followed
    by early ones) and the first STADIG_LEAD_MS go out at once, so the speaker plays one unbroken stream."""

    async def _write_audio_sleep(self):
        if not stadig_takt():
            return await super()._write_audio_sleep()
        now = time.monotonic()
        if self._next_send_time == 0 or now - self._next_send_time > STADIG_OMSTART_S:
            self._next_send_time = now - STADIG_LEAD_S
        self._next_send_time += self._send_interval
        await asyncio.sleep(max(0.0, self._next_send_time - now))


class MixedFastAPIWebsocketTransport(FastAPIWebsocketTransport):
    """A per-connection transport whose client speaks both frame types.

    Construct one of these per connected device. Everything else — event
    handlers, `input()`, `output()` — behaves exactly like the pipecat base.
    """

    def __init__(
        self,
        websocket: WebSocket,
        params: FastAPIWebsocketParams,
        input_name: typing.Optional[str] = None,
        output_name: typing.Optional[str] = None,
    ):
        """Initialize the transport.

        Args:
            websocket: The FastAPI/starlette WebSocket connection.
            params: Transport configuration, including the serializer.
            input_name: Optional name for the input processor.
            output_name: Optional name for the output processor.
        """
        super().__init__(websocket, params, input_name=input_name, output_name=output_name)

        # The base constructor already built a single-mode client and handed it
        # to the input/output processors, so both have to be rebuilt around the
        # mixed client. The discarded objects hold no resources — their
        # constructors only assign fields — so this costs nothing at runtime.
        self._client = MixedFastAPIWebsocketClient(websocket, self._callbacks)
        self._input = MixedFastAPIWebsocketInputTransport(
            self, self._client, self._params, name=self._input_name
        )
        self._output = StadigUtTransport(
            self, self._client, self._params, name=self._output_name
        )

    @property
    def client(self) -> MixedFastAPIWebsocketClient:
        """The underlying WebSocket client, for out-of-band control frames."""
        return self._client
