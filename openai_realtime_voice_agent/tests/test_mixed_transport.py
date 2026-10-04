"""MixedFastAPIWebsocketClient carries binary AND text, unlike the base client."""
import unittest

from starlette.websockets import WebSocketState
from pipecat.transports.websocket.fastapi import FastAPIWebsocketClient

from app.multi_client_transport import MixedFastAPIWebsocketClient


class FakeWebSocket:
    """Minimal stand-in for a starlette WebSocket."""

    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED

    async def receive(self):
        if not self._inbound:
            return {"type": "websocket.disconnect"}
        return self._inbound.pop(0)

    async def send_bytes(self, data):
        self.sent.append(("bytes", data))

    async def send_text(self, data):
        self.sent.append(("text", data))

    async def _iter(self, key):
        for message in list(self._inbound):
            if message.get(key) is not None:
                yield message[key]

    def iter_bytes(self):
        return self._iter("bytes")

    def iter_text(self):
        return self._iter("text")


# A realistic va_client frame sequence: control text interleaved with PCM.
INBOUND = [
    {"type": "websocket.receive", "text": '{"type":"start"}'},
    {"type": "websocket.receive", "bytes": b"\x01\x02" * 160},
    {"type": "websocket.receive", "text": '{"type":"wake"}'},
    {"type": "websocket.receive", "bytes": b"\x03\x04" * 160},
    {"type": "websocket.receive", "text": '{"type":"interrupt"}'},
]


async def drain(client):
    return [message async for message in client.receive()]


class TestMixedTransport(unittest.IsolatedAsyncioTestCase):
    async def test_receive_yields_text_and_binary_in_order(self):
        got = await drain(MixedFastAPIWebsocketClient(FakeWebSocket(INBOUND), object()))
        self.assertEqual(len(got), 5)
        self.assertEqual(
            [m for m in got if isinstance(m, str)],
            ['{"type":"start"}', '{"type":"wake"}', '{"type":"interrupt"}'],
        )
        self.assertEqual(len([m for m in got if isinstance(m, (bytes, bytearray))]), 2)

    async def test_send_dispatches_on_payload_type(self):
        ws = FakeWebSocket([])
        mixed = MixedFastAPIWebsocketClient(ws, object())
        await mixed.send(b"\xaa\xbb")
        await mixed.send('{"type":"phase","value":"listening"}')
        self.assertEqual(
            ws.sent,
            [("bytes", b"\xaa\xbb"), ("text", '{"type":"phase","value":"listening"}')],
        )

    async def test_stock_client_drops_control_frames(self):
        # Documents why the mixed client exists: the stock binary-mode client
        # silently drops every text control frame.
        base = FastAPIWebsocketClient(FakeWebSocket(INBOUND), True, object())
        got = await drain(base)
        self.assertTrue(all(isinstance(m, (bytes, bytearray)) for m in got))
        self.assertEqual(len(got), 2)

    async def test_disconnect_terminates_iteration(self):
        self.assertEqual(await drain(MixedFastAPIWebsocketClient(FakeWebSocket([]), object())), [])


if __name__ == "__main__":
    unittest.main()
