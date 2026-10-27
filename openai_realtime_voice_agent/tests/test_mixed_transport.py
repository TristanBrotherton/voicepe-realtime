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
    """An output_batch_end marker follows ALL of its batch's PCM.

    pipecat's media sender resamples reply audio through a stream resampler
    that keeps a filter tail back, and then cuts whole chunks, keeping the
    sub-chunk rest back too. The fence has to flush both, or the marker
    precedes the batch's last samples.
    """

    async def test_marker_follows_the_resampler_tail_and_the_partial_chunk(self):
        import math

        from pipecat.frames.frames import OutputTransportMessageFrame, TTSAudioRawFrame
        from pipecat.transports.base_output import BaseOutputTransport
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

        from app.multi_client_transport import MixedFastAPIWebsocketTransport
        from app.raw_audio_serializer import RawAudioSerializer

        transport = MixedFastAPIWebsocketTransport(
            FakeWebSocket([]),
            FastAPIWebsocketParams(serializer=RawAudioSerializer("kitchen"),
                                   audio_out_enabled=True, audio_out_sample_rate=24000),
        )
        out = transport.output()
        chunk_size = 960 * 2  # 40 ms at 24 kHz mono 16-bit
        sender = BaseOutputTransport.MediaSender(
            out, destination=None, sample_rate=24000, audio_chunk_size=chunk_size, params=out._params
        )
        queued = []

        class Queue:
            async def put(self, frame):
                queued.append(frame)

        sender._audio_queue = Queue()
        out._media_senders[None] = sender

        # 100 ms of a 16 kHz tone: resampled to 24 kHz it is 2400 samples,
        # i.e. 2.5 chunks, and the resampler holds its filter tail back.
        samples_16k = 1600
        pcm = b"".join(
            int(8000 * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(2, "little", signed=True)
            for i in range(samples_16k)
        )
        await sender.handle_audio_frame(TTSAudioRawFrame(pcm, sample_rate=16000, num_channels=1))
        marker = OutputTransportMessageFrame(message={"type": "output_batch_end", "output_batch_id": "b1"})
        await out._handle_frame(marker)

        self.assertIs(queued[-1], marker, "the marker goes out last")
        audio = b"".join(f.audio for f in queued if isinstance(f, TTSAudioRawFrame))
        self.assertAlmostEqual(len(audio) // 2, 2400, delta=2, msg="every resampled sample precedes the marker")
        self.assertEqual(sender._audio_runs, type(sender._audio_runs)(), "nothing held back")


if __name__ == "__main__":
    unittest.main()
