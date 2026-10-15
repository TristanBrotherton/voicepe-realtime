"""/svar-prov (test only, EKO_PROV=1): the clip plays through the reply path while the mic is deaf to everything after
the canceller, so the clip's echo can not become a turn and wake the cloud engine (Kontoret 2026-10-15 20:53)."""
import asyncio
import base64

import httpx
import pytest

from app.main import Application
from app.raw_audio_serializer import RawAudioSerializer


@pytest.mark.asyncio
async def test_the_mic_is_deaf_while_the_clip_plays_and_hears_again_after(monkeypatch):
    monkeypatch.setenv("EKO_PROV", "1")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    ser = RawAudioSerializer("kontoret", input_sample_rate=16000)
    mic = b"\x10\x27" * 320
    heard = []

    class Task:
        async def queue_frame(self, _frame):
            heard.append(await ser.deserialize(mic))

    class Maskin:
        async def phase(self, *_a):
            pass

    conn = type("C", (), {"serializer": ser, "task": Task(), "maskin": Maskin()})()
    app = Application()
    app.websocket_handler = type("H", (), {"resolve_device": lambda self, _r: conn})()
    app.session_manager = None
    transport = httpx.ASGITransport(app=app.build_web_app(), client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/svar-prov", json={"rum": "kontoret", "pcm_b64": base64.b64encode(b"\0" * 4096).decode()})
    assert r.json()["ok"] and len(heard) == 4          # 4096 B in 1024 B frames
    assert heard == [None] * 4                        # nothing reached the pipeline while it played
    assert await ser.deserialize(mic) is not None     # and the mic is back afterwards
