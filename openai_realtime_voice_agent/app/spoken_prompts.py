"""Short spoken prompts outside the Realtime conversation.

Two jobs that should not depend on the language model:

* **Slow-tool acknowledgements.** When a lookup that is known to be slow
  (web search, the external agent, or any tool whose measured median exceeds
  ``ACK_THRESHOLD_S``) is still running after ``ack_delay_s``, the device plays
  one short phrase ("One moment.") so the user is not left in dead air. Fast
  Home Assistant commands never trigger it, and at most one acknowledgement
  plays per turn. The model is told not to narrate tool use, so there is
  exactly one consistent behaviour.
* **Audible errors.** A rate limit or failed response during a turn the user
  started is explained out loud instead of silently going idle.

Prompts are synthesized once with the OpenAI speech endpoint and cached on the
add-on's persistent volume. If a prompt cannot be produced, callers fall back
to the device's error chime (errors) or stay silent (acknowledgements).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Awaitable, Callable, Dict, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

PROMPTS: Dict[str, str] = {
    "ack": "One moment.",
    "error_rate_limit": "Sorry, I'm getting too many requests right now. Please try again in a minute.",
    "error_generic": "Sorry, something went wrong on my end. Please try again.",
}

# Voices accepted by the speech endpoint. The Realtime voice is reused when it
# is one of these, so prompts sound like the assistant.
TTS_VOICES = {
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
    "sage", "shimmer", "verse", "marin", "cedar",
}
DEFAULT_STYLE = "Warm, clear and concise."
FALLBACK_VOICE = "alloy"
ACK_THRESHOLD_S = 1.2
DEFAULT_SLOW_TOOLS = ("web_search", "ask_openclaw")


def pick_voice(*candidates: str) -> str:
    for voice in candidates:
        if voice and voice.strip().lower() in TTS_VOICES:
            return voice.strip().lower()
    return "alloy"


class TTSCache:
    """Synthesize 24 kHz mono PCM16 once per (voice, style, text) and cache it."""

    def __init__(self, api_key: str, voice: str, style: str = DEFAULT_STYLE,
                 cache_dir: str = "/data/spoken_prompts",
                 client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
                 model: str = "gpt-4o-mini-tts"):
        self.api_key = api_key
        self.voice = voice
        self.style = style or DEFAULT_STYLE
        self.cache_dir = cache_dir
        self.model = model
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=20))
        self._memory: Dict[str, bytes] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}|{self.voice}|{self.style}|{text}".encode()).hexdigest()[:32]

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.pcm")

    def cached(self, text: str) -> Optional[bytes]:
        key = self._key(text)
        if key in self._memory:
            return self._memory[key]
        path = self._path(key)
        try:
            if os.path.getsize(path) > 0:
                with open(path, "rb") as f:
                    self._memory[key] = f.read()
                return self._memory[key]
        except OSError:
            pass
        return None

    async def get(self, text: str) -> Optional[bytes]:
        pcm = self.cached(text)
        if pcm is not None or not self.api_key:
            return pcm
        key = self._key(text)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            pcm = self.cached(text)
            if pcm is not None:
                return pcm
            pcm = None
            for voice in dict.fromkeys((self.voice, FALLBACK_VOICE)):
                try:
                    async with self._client_factory() as client:
                        response = await client.post(
                            "https://api.openai.com/v1/audio/speech",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json={"model": self.model, "voice": voice, "input": text,
                                  "response_format": "pcm", "instructions": self.style},
                        )
                        if getattr(response, "status_code", 200) == 400 and voice != FALLBACK_VOICE:
                            # Some Realtime voices are not offered by the speech
                            # endpoint; use the fallback voice from now on.
                            logger.info(f"ℹ️ speech endpoint rejected voice {voice!r}; using {FALLBACK_VOICE!r}")
                            self.voice = FALLBACK_VOICE
                            key = self._key(text)
                            continue
                        response.raise_for_status()
                        pcm = response.content
                        break
                except Exception as e:
                    logger.warning(f"⚠️ prompt synthesis failed ({text[:30]!r}): {e!r}")
                    return None
            if pcm is None:
                return None
            self._memory[key] = pcm
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                tmp = self._path(key) + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(pcm)
                os.replace(tmp, self._path(key))
            except OSError as e:
                logger.debug(f"prompt cache write failed: {e!r}")
            return pcm


class SpokenPrompts:
    """Plays cached prompts on one device through the guarded announcement lane."""

    def __init__(self, tts: TTSCache, play: Callable[[bytes, str], Awaitable[bool]],
                 slow_tools: Iterable[str] = DEFAULT_SLOW_TOOLS, ack_delay_s: float = 1.0,
                 enabled: bool = True):
        self.tts = tts
        self._play = play
        self.slow_tools = {t.strip() for t in slow_tools if t and t.strip()}
        self.ack_delay_s = ack_delay_s
        self.enabled = enabled

    async def warm(self) -> None:
        """Pre-synthesize every prompt in the background at startup."""
        for text in PROMPTS.values():
            await self.tts.get(text)

    def is_slow(self, tool_name: str, p50_s: Optional[float]) -> bool:
        if tool_name in self.slow_tools:
            return True
        return p50_s is not None and p50_s > ACK_THRESHOLD_S

    async def say(self, key: str, device_id: str) -> bool:
        if not self.enabled:
            return False
        text = PROMPTS.get(key)
        if not text:
            return False
        pcm = self.tts.cached(text) or await self.tts.get(text)
        if not pcm:
            return False
        return await self._play(pcm, device_id)


def classify_error(reason: str) -> str:
    text = (reason or "").lower()
    if "rate limit" in text or "rate_limit" in text or "429" in text:
        return "error_rate_limit"
    return "error_generic"
