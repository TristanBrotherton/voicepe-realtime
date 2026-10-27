"""Per-connection FastAPI transport for binary audio and text control frames."""

import asyncio
import typing
from typing import Callable, Optional

from loguru import logger
from pipecat.frames.frames import EndFrame, OutputTransportMessageFrame, TTSAudioRawFrame
from starlette.websockets import WebSocket, WebSocketState

from pipecat.transports.websocket.fastapi import (
    _WS_CLOSE_TIMEOUT_DEFAULT,
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

    def __init__(self, websocket: WebSocket, callbacks, ws_close_timeout: float = _WS_CLOSE_TIMEOUT_DEFAULT):
        """Initialize the client.

        Args:
            websocket: The FastAPI/starlette WebSocket connection.
            callbacks: The transport's FastAPIWebsocketCallbacks.
            ws_close_timeout: How long disconnect() waits for the close handshake.
        """
        # The stock client reads text and binary alike since pipecat 1.x, but
        # its receive() iterator ends on the first frame of the other kind
        # that carries no payload, and its send() takes no lock; both
        # overrides below stay.
        super().__init__(websocket, callbacks, ws_close_timeout=ws_close_timeout)
        # The pipeline's output transport writes audio bytes while the phase
        # emitter writes JSON text, from different tasks, to the same socket.
        # starlette does not serialize concurrent sends, so interleaved frames
        # could corrupt the stream; this lock makes each send atomic.
        self._send_lock = asyncio.Lock()
        # Told about every binary frame that actually went out, whichever path
        # sent it (the pipeline's output transport, or an out-of-band prompt,
        # announcement or acknowledgement clip). The output recording hangs off
        # this so it holds what the device was sent, not what one pipeline
        # stage saw.
        self.on_audio_sent: Optional[Callable[[bytes], None]] = None
        self._audio_tap_failed = False

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
                        self._audio_sent(data)
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

    def _audio_sent(self, data) -> None:
        tap = self.on_audio_sent
        if tap is None:
            return
        try:
            tap(bytes(data))
        except Exception as e:
            # A recording problem must never cost the device its audio. Logged
            # once: this runs for every 40 ms of reply audio.
            if not self._audio_tap_failed:
                self._audio_tap_failed = True
                logger.warning(f"{self} output audio tap failed: {e!r}")


class MixedFastAPIWebsocketInputTransport(FastAPIWebsocketInputTransport):
    """End the pipeline when this connection's receive loop ends."""

    async def _receive_messages(self):
        await super()._receive_messages()
        await self.push_frame(EndFrame())


class BatchFastAPIWebsocketOutputTransport(FastAPIWebsocketOutputTransport):
    """Flush partial PCM before an ordered Live batch fence.

    pipecat's media sender only queues whole ``audio_chunk_size`` chunks and
    keeps the sub-chunk tail back until the next audio or a TTSStoppedFrame,
    while an ordinary message frame is queued at once. Without this adapter
    an ``output_batch_end`` marker could precede its final samples. The tail
    goes out as it is: no padding, no PCM modification; Realtime frames are
    unaffected.

    The sender's stream resampler is left alone. At equal rates — the Live
    service's 24 kHz on the 24 kHz transport the handler configures — it is
    a pass-through holding nothing, so the drained runs are the whole batch.
    At other rates the filter would hold a tail back, but flushing it here
    would reset the stream at every batch seam of one reply and click (pipecat
    flushes it only at the end of speech); batches are byte-exact at equal
    rates only, which is the one configuration the serializer speaks.

    pipecat 1.13 has no public hook for "flush the tail before this message"
    (``_enqueue_flushed_audio_buffer`` pads to a full chunk and is private
    too), so this reaches into ``MediaSender``'s buffer: ``_audio_runs``,
    ``_take_audio_chunk`` and ``_audio_queue``. Re-check on every pipecat
    upgrade.
    """

    async def _handle_frame(self, frame):
        if (isinstance(frame, OutputTransportMessageFrame)
                and isinstance(frame.message, dict)
                and frame.message.get('type') == 'output_batch_end'):
            sender = self._media_senders.get(frame.transport_destination)
            if sender is not None and sender._audio_runs:
                while sender._audio_runs:
                    audio, uninterruptible = sender._take_audio_chunk()
                    tail = TTSAudioRawFrame(
                        audio, sample_rate=sender.sample_rate,
                        num_channels=self._params.audio_out_channels,
                    )
                    tail.transport_destination = frame.transport_destination
                    tail.interruptible = not uninterruptible
                    await sender._audio_queue.put(tail)
        await super()._handle_frame(frame)


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
        self._client = MixedFastAPIWebsocketClient(
            websocket, self._callbacks, ws_close_timeout=self._params.ws_close_timeout
        )
        self._input = MixedFastAPIWebsocketInputTransport(
            self, self._client, self._params, name=self._input_name
        )
        self._output = BatchFastAPIWebsocketOutputTransport(
            self, self._client, self._params, name=self._output_name
        )

    @property
    def client(self) -> MixedFastAPIWebsocketClient:
        """The underlying WebSocket client, for out-of-band control frames."""
        return self._client
