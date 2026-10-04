"""Device WebSocket access control: tokens, allowlist, migration modes, /healthz."""
import asyncio
import json
import unittest

import httpx
import websockets

from app.device_auth import DeviceAuth
from tests.test_two_clients import LiveServerTestCase


class FakeURL:
    def __init__(self, query=""):
        self.query = query


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeWS:
    def __init__(self, headers=None, query="", host="192.0.2.5"):
        self.headers = headers or {}
        self.url = FakeURL(query)
        self.client = FakeClient(host)


class TestDeviceAuthPolicy(unittest.TestCase):
    def test_auto_without_token_is_off_and_logged(self):
        auth = DeviceAuth()
        self.assertEqual(auth.mode, "off")
        decision = auth.check(FakeWS())
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.authenticated)
        with self.assertLogs("app.device_auth", "WARNING"):
            auth.log_startup()

    def test_auto_with_token_enforces(self):
        auth = DeviceAuth(token="s3cret-token")
        self.assertEqual(auth.mode, "enforce")
        self.assertFalse(auth.check(FakeWS()).allowed)
        self.assertFalse(auth.check(FakeWS({"authorization": "Bearer wrong"})).allowed)
        ok = auth.check(FakeWS({"authorization": "Bearer s3cret-token"}))
        self.assertTrue(ok.allowed and ok.authenticated)
        self.assertTrue(auth.check(FakeWS(query="device_id=k&token=s3cret-token")).allowed)

    def test_permissive_accepts_but_marks_unauthenticated(self):
        auth = DeviceAuth(token="s3cret-token", mode="permissive")
        decision = auth.check(FakeWS())
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.authenticated)
        self.assertIn("permissive", decision.reason)

    def test_enforce_without_token_falls_back_to_off(self):
        self.assertEqual(DeviceAuth(mode="enforce").mode, "off")

    def test_allowlist_applies_in_every_mode(self):
        auth = DeviceAuth(allowlist=["192.0.2.0/30", "bogus"])
        self.assertTrue(auth.check(FakeWS(host="192.0.2.2")).allowed)
        self.assertFalse(auth.check(FakeWS(host="192.0.2.9")).allowed)
        self.assertFalse(auth.check(FakeWS(host=None)).allowed)

    def test_http_authorization_helper(self):
        auth = DeviceAuth(token="dev-token")
        self.assertTrue(auth.authorizes_request("Bearer dev-token"))
        self.assertTrue(auth.authorizes_request("Bearer ann-token", ["ann-token"]))
        self.assertFalse(auth.authorizes_request("Bearer nope", ["", "ann-token"]))
        self.assertFalse(auth.authorizes_request(""))


class TestEnforcedServer(LiveServerTestCase):
    handler_kwargs = {"device_auth": DeviceAuth(token="device-secret")}

    async def test_unauthenticated_client_is_rejected(self):
        with self.assertRaises(Exception):
            async with websockets.connect(self.url("?device_id=intruder")) as ws:
                await asyncio.wait_for(ws.recv(), 2)
        self.assertEqual(self.handler.devices.ids(), [])
        self.assertEqual(self.services, [], "no OpenAI session may be created for a rejected client")

    async def test_header_token_is_accepted(self):
        async with websockets.connect(
            self.url("?device_id=kitchen"),
            additional_headers={"Authorization": "Bearer device-secret"},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["proto"], 2)

    async def test_healthz_hides_ids_without_authorization(self):
        async with websockets.connect(
            self.url("?device_id=kitchen"),
            additional_headers={"Authorization": "Bearer device-secret"},
        ) as ws:
            await asyncio.wait_for(ws.recv(), 5)
            await self.wait_for_ids(["kitchen"])
            async with httpx.AsyncClient() as client:
                anonymous = (await client.get(f"http://127.0.0.1:{self.port}/healthz")).json()
                authorized = (await client.get(
                    f"http://127.0.0.1:{self.port}/healthz",
                    headers={"Authorization": "Bearer device-secret"},
                )).json()
        self.assertEqual(anonymous, {"status": "ok", "devices": 1})
        self.assertEqual(authorized["device_ids"], ["kitchen"])

    def configure_app(self, app):
        app.device_auth = self.handler.device_auth


if __name__ == "__main__":
    unittest.main()
