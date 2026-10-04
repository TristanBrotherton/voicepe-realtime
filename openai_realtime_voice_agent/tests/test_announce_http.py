"""Announce endpoint: auth, targeting, duplicate suppression and retries."""
import asyncio
import unittest

from aiohttp import ClientSession

from app import announce_http


TOKEN = "test-token"


class AnnounceServerTestCase(unittest.IsolatedAsyncioTestCase):
    """Starts a real announce server on an ephemeral port per test."""

    connected = {"kitchen", "office"}

    async def asyncSetUp(self):
        announce_http._recent.clear()
        announce_http._pending.clear()
        self.attempts = []
        self.results = []
        self.runner = await announce_http.start_announce_server(
            0, TOKEN, self._announce, lambda device_id: (
                device_id in self.connected if device_id else bool(self.connected)
            )
        )
        site = next(iter(self.runner.sites))
        self.port = site._server.sockets[0].getsockname()[1]
        self.session = ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def _announce(self, message, device_id):
        self.attempts.append((message, device_id))
        result = self.results.pop(0) if self.results else True
        if callable(result):
            return await result()
        return result

    def post(self, payload, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.session.post(
            f"http://127.0.0.1:{self.port}/announce", json=payload, headers=headers
        )


class TestAnnounceHttp(AnnounceServerTestCase):
    async def test_rejects_missing_or_wrong_token(self):
        async with self.post({"message": "hi"}, token=None) as response:
            self.assertEqual(response.status, 401)
        async with self.post({"message": "hi"}, token="wrong") as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(self.attempts, [])

    async def test_rejects_empty_message(self):
        async with self.post({"message": "   "}) as response:
            self.assertEqual(response.status, 400)

    async def test_unknown_explicit_device_is_503_without_fallback(self):
        async with self.post({"message": "Dinner is ready", "device_id": "bedroom"}) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["device_id"], "bedroom")
        self.assertEqual(self.attempts, [])

    async def test_failed_announcement_does_not_suppress_retry(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_success():
            started.set()
            await release.wait()
            return True

        self.results = [False, slow_success]
        payload = {"message": "Dinner is ready", "device_id": "kitchen"}
        async with self.post(payload) as response:
            self.assertEqual(response.status, 503)

        first = asyncio.create_task(self.post(payload).__aenter__())
        await started.wait()
        # A concurrent duplicate while the first delivery is in flight is
        # accepted but not spoken twice.
        async with self.post(payload) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "duplicate_suppressed")
        release.set()
        response = await first
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "announced")
        response.release()

        # The same text to a DIFFERENT room is not a duplicate.
        async with self.post({"message": "Dinner is ready", "device_id": "office"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "announced")

        self.assertEqual(
            self.attempts,
            [
                ("Dinner is ready", "kitchen"),
                ("Dinner is ready", "kitchen"),
                ("Dinner is ready", "office"),
            ],
        )

    async def test_message_is_truncated_to_limit(self):
        long = "x" * (announce_http.MAX_MESSAGE_CHARS + 50)
        async with self.post({"message": long, "device_id": "kitchen"}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.attempts[0][0]), announce_http.MAX_MESSAGE_CHARS)


if __name__ == "__main__":
    unittest.main()
