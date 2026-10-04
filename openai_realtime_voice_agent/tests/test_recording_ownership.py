"""A failed connection setup releases its recording claim."""
import unittest

from app.websocket_handler import WebSocketHandler


class FakeURL:
    query = "device_id=kitchen"


class FakeWebSocket:
    url = FakeURL()
    client = None
    headers = {}

    async def accept(self):
        return None

    async def close(self, code=1000, reason=None):
        return None


class FakeTransport:
    def event_handler(self, _name):
        def register(fn):
            return fn
        return register


class FakeRecordingService:
    def __init__(self):
        self.started = []
        self.stopped = 0

    def start_new_session(self, device_id):
        self.started.append(device_id)

    def stop_recording(self):
        self.stopped += 1


class TestRecordingOwnership(unittest.IsolatedAsyncioTestCase):
    async def test_failed_setup_releases_claim(self):
        recorder = FakeRecordingService()
        handler = WebSocketHandler(audio_recording_service=recorder)
        handler.create_transport = lambda *_args: FakeTransport()

        async def factory(_connection):
            return object()

        def fail_after_claim(connection, _activity_callback):
            connection.records_audio = handler._claim_recording(connection.device_id)
            raise RuntimeError("pipeline setup failed")

        handler.openai_service_factory = factory
        handler.build_pipeline = fail_after_claim
        await handler.serve_connection(FakeWebSocket())

        self.assertEqual(recorder.started, ["kitchen"])
        self.assertEqual(recorder.stopped, 1)
        self.assertIsNone(handler._recording_owner)

    async def test_second_device_does_not_steal_recording(self):
        recorder = FakeRecordingService()
        handler = WebSocketHandler(audio_recording_service=recorder)
        self.assertTrue(handler._claim_recording("kitchen"))
        self.assertFalse(handler._claim_recording("office"))
        handler._release_recording("office")
        self.assertEqual(handler._recording_owner, "kitchen")
        handler._release_recording("kitchen")
        self.assertIsNone(handler._recording_owner)


if __name__ == "__main__":
    unittest.main()
