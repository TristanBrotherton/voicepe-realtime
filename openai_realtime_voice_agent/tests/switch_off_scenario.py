"""A fixed scenario through the pipeline exactly as WebSocketHandler builds it,
recording every frame the device receives. Used to show that with the
speaker owner switched off the device gets byte-for-byte what it got before
the owner existed (tests/test_speaker_owner_wiring.py holds the digest that
the add-on produced before this work).

Written against the handler's public construction steps only, so the same
file runs unchanged on the code from before the owner existed.
"""
import asyncio
import hashlib
import json
from unittest.mock import patch

import app.live_service as live_service_module
from app.device_config import DeviceDirectory
from app.device_registry import DeviceConnection
from app.live_service import OpenAILiveLLMService
from app.phase_emitter import TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.turn_timeline import TurnTimeline
from app.voice_runtime import LiveConfig
from app.websocket_handler import WebSocketHandler

from tests import live_fixtures as fx
from tests.test_live_device_audio import DeviceSocket, ModelSocket


async def run_fixed_scenario(devices_json: str = "", lead_ms: int = 600, start=None,
                             interrupt=None, keep=None):
    """Model speech, a pause, more speech, then a device interrupt.

    Returns (sha256 of the PCM the device received, its byte count, the text
    frames it received in order with timing-dependent fields removed).
    """
    sockets = []

    async def fake_connect(uri, additional_headers=None, **_kwargs):
        socket = ModelSocket()
        sockets.append(socket)
        return socket

    with patch.object(live_service_module, "websocket_connect", fake_connect):
        handler = WebSocketHandler(output_lead_buffer_ms=lead_ms)
        handler.device_directory = DeviceDirectory.from_json(devices_json)
        device = DeviceSocket()
        serializer = RawAudioSerializer("testdevice")
        connection = DeviceConnection(device_id="testdevice", websocket=device,
                                      serializer=serializer,
                                      profile=handler.device_directory.get("testdevice"))
        connection.transport = handler.create_transport(device, serializer)
        connection.turn_timeline = TurnTimeline("testdevice")
        build_owner = getattr(handler, "_build_speaker_owner", None)
        if build_owner is not None:
            connection.speaker_owner = build_owner(connection)
        service = OpenAILiveLLMService(
            api_key="sk-test", config=LiveConfig(voice="marin"),
            instructions="Be brief.", backend_instructions="Use tools.",
            tools=[], fill_silence=False, admit_complete_replies=False,
        )
        service.turn_liveness = TurnLiveness()
        service.device_id = "testdevice"
        connection.openai_service = service
        pipeline, runner, task = handler.build_pipeline(connection)
        run = asyncio.create_task(runner.run(task))
        try:
            for _ in range(500):
                if sockets and sockets[-1].started.is_set():
                    break
                await asyncio.sleep(0.01)
            sockets[-1].push(fx.session_started())
            for _ in range(500):
                if service._session_started:
                    break
                await asyncio.sleep(0.01)
            hello = handler.hello_payload("testdevice")
            if start is not None:
                await serializer.deserialize(json.dumps(start))
            pcm = fx.reply_pcm(fx.SHORT_PLAN)
            for delta in fx.output_audio_deltas(pcm):
                sockets[-1].push(delta)
            await asyncio.sleep(1.5)
            for delta in fx.output_audio_deltas(fx.tone(600, freq=330.0)):
                sockets[-1].push(delta)
            await asyncio.sleep(1.0)
            await serializer.deserialize(json.dumps(interrupt or {"type": "interrupt"}))
            await asyncio.sleep(1.0)
        finally:
            await asyncio.wait_for(task.cancel(), 5)
            await asyncio.wait_for(run, 5)
            if getattr(connection, "speaker_owner", None) is not None:
                await connection.speaker_owner.shutdown()
    texts = [json.loads(t) for t in device.text]
    if keep is not None:
        keep.update(device=device, connection=connection, service=service)
    return (hashlib.sha256(device.pcm).hexdigest(), len(device.pcm), texts, hello)


def main():  # pragma: no cover - prints the digest for a code version
    digest, size, texts, hello = asyncio.run(run_fixed_scenario())
    print(json.dumps({"sha256": digest, "bytes": size, "texts": texts, "hello": hello}))


if __name__ == "__main__":  # pragma: no cover
    main()
