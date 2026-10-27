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

    async def test_stock_client_reads_both_kinds_since_pipecat_1(self):
        # pipecat 0.0.x's binary-mode client silently dropped every text
        # control frame, which is why the mixed client exists. Since 1.x the
        # stock client reads both kinds in order too; what the mixed client
        # still adds is the per-type send, the send lock and the audio tap.
        base = FastAPIWebsocketClient(FakeWebSocket(INBOUND), object())
        got = await drain(base)
        self.assertEqual(got, await drain(MixedFastAPIWebsocketClient(FakeWebSocket(INBOUND), object())))
        self.assertEqual(len(got), 5)

    async def test_disconnect_terminates_iteration(self):
        self.assertEqual(await drain(MixedFastAPIWebsocketClient(FakeWebSocket([]), object())), [])


class TestBatchFenceFlush(unittest.IsolatedAsyncioTestCase):
    """An output_batch_end marker follows all of its batch's PCM, and a reply
    split into batches sounds like one that was not.

    pipecat's media sender feeds reply audio through a stream resampler
    (a pass-through at equal rates) and then cuts whole chunks, keeping the
    sub-chunk rest back. The fence drains that rest; it never flushes the
    resampler, which would reset the filter at every seam.
    """

    def setUp(self):
        from pipecat.transports.base_output import BaseOutputTransport
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

        from app.multi_client_transport import MixedFastAPIWebsocketTransport
        from app.raw_audio_serializer import RawAudioSerializer

        transport = MixedFastAPIWebsocketTransport(
            FakeWebSocket([]),
            FastAPIWebsocketParams(serializer=RawAudioSerializer("kitchen"),
                                   audio_out_enabled=True, audio_out_sample_rate=24000),
        )
        self.out = transport.output()
        self.sender = BaseOutputTransport.MediaSender(
            self.out, destination=None, sample_rate=24000, audio_chunk_size=960 * 2,  # 40 ms
            params=self.out._params,
        )
        self.queued = []
        queued = self.queued

        class Queue:
            async def put(self, frame):
                queued.append(frame)

        self.sender._audio_queue = Queue()
        self.out._media_senders[None] = self.sender

    @staticmethod
    def tone(samples, rate, start=0):
        import math
        return b"".join(
            int(8000 * math.sin(2 * math.pi * 440 * (start + i) / rate)).to_bytes(2, "little", signed=True)
            for i in range(samples)
        )

    async def feed(self, pcm, rate):
        from pipecat.frames.frames import TTSAudioRawFrame
        await self.sender.handle_audio_frame(TTSAudioRawFrame(pcm, sample_rate=rate, num_channels=1))

    async def fence(self, batch):
        from pipecat.frames.frames import OutputTransportMessageFrame
        marker = OutputTransportMessageFrame(message={"type": "output_batch_end", "output_batch_id": batch})
        await self.out._handle_frame(marker)
        return marker

    def audio(self):
        from pipecat.frames.frames import TTSAudioRawFrame
        return b"".join(f.audio for f in self.queued if isinstance(f, TTSAudioRawFrame))

    async def test_marker_follows_every_sample_at_the_transport_rate(self):
        pcm = self.tone(2400, 24000)  # 100 ms: 2.5 chunks, the half chunk held back
        await self.feed(pcm, 24000)
        marker = await self.fence("b1")
        self.assertIs(self.queued[-1], marker, "the marker goes out last")
        self.assertEqual(self.audio(), pcm, "every sample precedes the marker, unmodified")
        self.assertEqual(len(self.sender._audio_runs), 0, "nothing held back")

    async def test_a_reply_split_into_batches_resamples_like_an_unsplit_one(self):
        # At another rate the resampler holds a filter tail; a fence must not
        # flush and restart it mid-reply, or the seam clicks.
        first, second = self.tone(1600, 16000), self.tone(1600, 16000, start=1600)
        await self.feed(first, 16000)
        await self.fence("b1")
        await self.feed(second, 16000)
        await self.fence("b2")
        split = self.audio()

        self.setUp()
        await self.feed(first, 16000)
        await self.feed(second, 16000)
        await self.fence("b3")
        self.assertEqual(split, self.audio(), "the seam changed the audio")


if __name__ == "__main__":
    unittest.main()
