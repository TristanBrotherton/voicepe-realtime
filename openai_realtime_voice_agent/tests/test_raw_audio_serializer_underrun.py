"""The device says when its speaker ran dry mid-reply; the canceller is told (US-060)."""
import json

import pytest

from app.mjuk_aec import MjukAec
from app.raw_audio_serializer import RawAudioSerializer


@pytest.mark.asyncio
async def test_underrun_and_the_capability_reach_the_canceller():
    s = RawAudioSerializer("kontoret")
    s.aec = MjukAec(klocka=lambda: 50.0)
    assert s.aec.underrun_stod is False
    await s.deserialize(json.dumps({"type": "play_stamp", "rx_ms": 10, "play_ms": 12, "first_bytes": 1024, "ur": 1}))
    assert s.aec.underrun_stod is True
    await s.deserialize(json.dumps({"type": "underrun", "ms": 1234}))
    assert s.aec.svar_start == 51.0          # 3 s from now before the level may cut him (2 s is the rule for a reply start)
