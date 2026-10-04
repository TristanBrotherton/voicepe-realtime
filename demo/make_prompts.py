#!/usr/bin/env python3
"""Synthesize the demo's spoken prompts with OpenAI text-to-speech.

Writes demo/prompts/<step id>.wav (16 kHz mono 16-bit PCM, the Voice PE's
microphone format). The voices are synthetic, so no household recording is
ever involved; the prompts folder is git-ignored.

Usage:
  OPENAI_API_KEY=sk-... python3 demo/make_prompts.py [--voice alloy] [--out demo/prompts]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SPEECH_URL = "https://api.openai.com/v1/audio/speech"


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """Windowed-sinc low-pass at 7.2 kHz, then 3:2 interpolation."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    taps = 63
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * (7200 / 24000) * n) * np.hamming(taps)
    y = np.convolve(x, h / h.sum(), mode="same")
    positions = np.arange(0, len(y) * 2 // 3) * 1.5
    z = np.interp(positions, np.arange(len(y)), y)
    return np.clip(np.round(z), -32768, 32767).astype("<i2").tobytes()


def synthesize(text: str, voice: str, api_key: str, model: str) -> bytes:
    body = json.dumps({"model": model, "voice": voice, "input": text, "response_format": "pcm"}).encode()
    request = urllib.request.Request(SPEECH_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()          # 24 kHz mono 16-bit PCM


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.json")
    parser.add_argument("--out", type=Path, default=HERE / "prompts")
    parser.add_argument("--voice", default="alloy")
    parser.add_argument("--model", default="gpt-4o-mini-tts")
    args = parser.parse_args(argv)
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        parser.error("set OPENAI_API_KEY")
    steps = json.loads(args.scenarios.read_text(encoding="utf-8"))["steps"]
    args.out.mkdir(parents=True, exist_ok=True)
    for step in steps:
        try:
            pcm = resample_24k_to_16k(synthesize(step["text"], args.voice, openai_key, args.model))
        except urllib.error.HTTPError as e:
            print(f"{step['id']}: speech API returned {e.code}", file=sys.stderr)
            return 1
        path = args.out / f"{step['id']}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        print(f"{path}  {len(pcm) / 32000:.1f} s  \"{step['text']}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
