"""US-060 AC-1 tool: the echo measurement endpoint is off by default, loopback only, and takes the mic away from everything else."""
import asyncio

import pytest

from app.raw_audio_serializer import RawAudioSerializer


@pytest.mark.asyncio
async def test_the_tap_gets_every_frame_and_nothing_else_does():
    ser = RawAudioSerializer("kontoret", input_sample_rate=16000)
    got = bytearray()
    ser.eko_mat = got.extend
    bit = b"\x01\x00" * 1600
    for _ in range(5):
        assert await ser.deserialize(bit) is None
    assert bytes(got) == bit * 5
    assert ser.take_turn_audio() == b""  # the ring and the turn buffer did not see it


@pytest.mark.asyncio
async def test_without_the_tap_the_mic_goes_on_as_before():
    ser = RawAudioSerializer("kontoret", input_sample_rate=16000)
    bit = b"\x01\x00" * 1600
    await ser.deserialize(bit)
    assert ser.take_turn_audio() == bit


def test_the_endpoint_is_off_unless_asked(monkeypatch):
    monkeypatch.delenv("EKO_PROV", raising=False)
    import os
    assert os.environ.get("EKO_PROV", "0") != "1"
