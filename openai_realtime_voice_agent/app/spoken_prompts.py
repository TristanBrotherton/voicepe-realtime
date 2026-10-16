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

Prompts are synthesized once and cached on the add-on's persistent volume.
When the assistant speaks in a GPT-Live voice they are rendered in that same
voice by a Live session (``app/live_voice_prompts.py``), so a filler and the
reply it precedes are one voice; otherwise the OpenAI speech endpoint renders
them in the assistant's voice. The speech endpoint is used for a Live-voice
install only as a named fallback when the Live voice cannot be reached, and
only for an error or refusal the user must hear; a filler is never spoken in
another voice. If a prompt cannot be produced, callers fall back to the
device's error chime (errors) or stay silent (acknowledgements).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Awaitable, Callable, Dict, Iterable, Optional

import httpx

from app.live_voice_prompts import LiveVoiceUnavailable
from app.progress_ack import GENERIC_CUES, generic_progress_phrase, progress_phrase

logger = logging.getLogger(__name__)

PROMPTS: Dict[str, str] = {
    "ack": "One moment.",
    # Fallback acknowledgements (GPT-Live router mode): played only when the
    # voice itself has said nothing 1.2 s after the user stopped speaking.
    "ack_on_it": "On it.",
    "ack_sure": "Sure, one moment.",
    "progress_check": "Let me check, one second.",
    # Person-location progress cue. Deliberately names nobody and carries no
    # gender: it is selected from the *shape* of a "where is …" request, so one
    # pre-warmed phrase serves every person (app/progress_ack.py).
    "progress_find_person": "I’ll find them, let me look.",
    "error_rate_limit": "Sorry, I'm getting too many requests right now. Please try again in a minute.",
    "error_generic": "Sorry, something went wrong on my end. Please try again.",
    # The model claimed success for a household action twice with no tool
    # behind it and the backend rescue produced nothing. Said instead of the
    # claim: "Done." with nothing done is the one thing the device must
    # never say.
    "error_not_done": "Sorry, I couldn't do that.",
    # The GPT-Live spend cutoff refused to open a session. Said out loud
    # because a refused turn is otherwise indistinguishable from a broken
    # assistant, and the user needs to know it was deliberate.
    "error_budget": (
        "I've reached today's voice usage limit, so I can't answer right now. "
        "Raise the limit in the add-on settings if you need more."
    ),
}
# Every contextual progress cue the runtime can play in a Live voice, so the
# warm-up renders them all and no filler waits on synthesis.
PROMPTS.update(GENERIC_CUES)

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

# Tools that must NEVER produce a spoken acknowledgement, whatever their
# measured median says.
#
# These are local reads that answer from Home Assistant's own state in
# milliseconds. A measured p50 can still cross ACK_THRESHOLD_S for them — one
# slow MCP round trip, a cold session, a busy recorder — and then the ack is
# both pointless and actively harmful: on 2026-10-07 the Workshop device
# fetched the time correctly twice, the "One moment." played out of its
# speaker, its local wake word fired on that audio, and the resulting
# new-wake/interrupt cancelled the answer. The echo guard in
# app/raw_audio_serializer.py (is_self_wake) stops the cancel; this list stops
# the cause, because for a GetDateTime there is no dead air to fill.
#
# Matching ignores the MCP namespace prefix, so `llm__GetDateTime`,
# `homeassistant__GetLiveContext` and the bare names all qualify.
DEFAULT_SILENT_ACK_TOOLS = (
    "GetDateTime",
    "GetLiveContext",
    "get_items",
    "HassCancelAllTimers",
)


def bare_tool_name(tool_name: str) -> str:
    """A tool name without its MCP ``namespace__`` prefix."""
    return (tool_name or "").rsplit("__", 1)[-1]


def pick_voice(*candidates: str) -> str:
    for voice in candidates:
        if voice and voice.strip().lower() in TTS_VOICES:
            return voice.strip().lower()
    return "alloy"


class TTSCache:
    """Synthesize 24 kHz mono PCM16 once per (engine, voice, style, text) and cache it.

    With ``live_voice`` and ``live_renderer`` set, ``cached`` and ``get`` speak
    of the Live voice only: an endpoint rendering of the same text is never
    returned as "cached" for a Live-voice install, so a prompt either comes
    out in the assistant's voice or goes through the named fallback.
    """

    def __init__(self, api_key: str, voice: str, style: str = DEFAULT_STYLE,
                 cache_dir: str = "/data/spoken_prompts",
                 client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
                 model: str = "gpt-4o-mini-tts",
                 live_voice: Optional[str] = None, live_renderer=None):
        self.api_key = api_key
        self.voice = voice
        self.style = style or DEFAULT_STYLE
        self.cache_dir = cache_dir
        self.model = model
        self.live_voice = (live_voice or "").strip().lower() or None
        self.live_renderer = live_renderer
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=20))
        self._memory: Dict[str, bytes] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    @property
    def live(self) -> bool:
        """Prompts are rendered in the assistant's own Live voice."""
        return bool(self.live_voice) and self.live_renderer is not None

    def _key(self, text: str, *, live: bool = False) -> str:
        # "live4": Live renders before the onset fix in cut_line could miss
        # their first word; the new key renders every Live line again.
        engine, voice = ("live4", self.live_voice) if live else (self.model, self.voice)
        return hashlib.sha256(f"{engine}|{voice}|{self.style}|{text}".encode()).hexdigest()[:32]

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.pcm")

    def _lookup(self, key: str) -> Optional[bytes]:
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

    def _store(self, key: str, pcm: bytes, persist: bool) -> None:
        self._memory[key] = pcm
        if not persist:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self._path(key) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(pcm)
            os.replace(tmp, self._path(key))
        except OSError as e:
            logger.debug(f"prompt cache write failed: {e!r}")

    def cached(self, text: str) -> Optional[bytes]:
        return self._lookup(self._key(text, live=self.live))

    async def render_live(self, texts: Iterable[str], *, persist: bool = True) -> Dict[str, bytes]:
        """Render ``texts`` in the Live voice and cache them. Never falls back.

        Lines the Live voice did not render verbatim, or could not render at
        all, are simply absent from the result; both outcomes are logged by
        name and the caller decides what silence or a fallback means for it.
        """
        results: Dict[str, bytes] = {}
        wanted = []
        for text in dict.fromkeys(texts):
            pcm = self._lookup(self._key(text, live=True))
            if pcm is not None:
                results[text] = pcm
            else:
                wanted.append(text)
        if not wanted or not self.live:
            return results
        try:
            rendered = await self.live_renderer.render(wanted)
        except LiveVoiceUnavailable as e:
            logger.warning(
                "⚠️ prompt voice unavailable: the Live voice %r could not render "
                "%d line(s) (%s)", self.live_voice, len(wanted), e.reason,
            )
            return results
        for text in wanted:
            pcm = rendered.get(text)
            if pcm:
                self._store(self._key(text, live=True), pcm, persist)
                results[text] = pcm
        missing = [text for text in wanted if text not in rendered]
        if missing:
            logger.warning(
                "⚠️ prompt voice unverified: the Live voice %r did not render "
                "%d of %d line(s) verbatim; they stay unrendered",
                self.live_voice, len(missing), len(wanted),
            )
        return results

    async def get(self, text: str, *, persist: bool = True,
                  fallback: bool = True) -> Optional[bytes]:
        """PCM for ``text`` in the assistant's voice.

        On a Live-voice install the Live renderer is tried first. ``fallback``
        then decides whether the speech endpoint may speak the line in its
        nearest voice instead: an error the user must hear says yes, a
        filler says no (silence beats a second voice).
        """
        pcm = self.cached(text)
        if pcm is not None or not self.api_key:
            return pcm
        if self.live:
            pcm = (await self.render_live([text], persist=persist)).get(text)
            if pcm is not None:
                return pcm
            if not fallback:
                return None
            logger.warning(
                "⚠️ prompt voice fallback: speaking one line with the speech "
                "endpoint voice %r instead of the Live voice %r",
                self.voice, self.live_voice,
            )
        return await self._synthesize(text, persist=persist)

    async def _synthesize(self, text: str, *, persist: bool) -> Optional[bytes]:
        """The speech endpoint, in the nearest voice it offers."""
        key = self._key(text)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            pcm = self._lookup(key)
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
            self._store(key, pcm, persist)
            return pcm


class SpokenPrompts:
    """Plays cached prompts on one device through the guarded announcement lane."""

    # The contextual filler is armed when the device enters "thinking", which
    # is USER_TURN_GAP_S (0.8 s) after the user's last transcript fragment.
    # Retained turns put the model's first reply audio no earlier than 3.2 s
    # after speech end (tool-free) and 5.6-9 s (tool turns), and a running
    # tool suspends the reply anyway, so a cue 0.4 s into "thinking" — about
    # 1.2 s after speech end — cannot collide with a reply; 0.7 s was margin
    # against a synthesis round trip the pre-rendered cues no longer need.
    def __init__(self, tts: TTSCache, play: Callable[[bytes, str], Awaitable[bool]],
                 slow_tools: Iterable[str] = DEFAULT_SLOW_TOOLS, ack_delay_s: float = 1.0,
                 progress_delay_s: float = 0.4,
                 enabled: bool = True,
                 silent_ack_tools: Iterable[str] = DEFAULT_SILENT_ACK_TOOLS):
        self.tts = tts
        self._play = play
        self.slow_tools = {t.strip() for t in slow_tools if t and t.strip()}
        # Compared on the bare name, so an allow-list entry works whether or
        # not the operator includes the MCP namespace.
        self.silent_ack_tools = {
            bare_tool_name(t.strip()) for t in silent_ack_tools if t and t.strip()
        }
        self.ack_delay_s = ack_delay_s
        self.progress_delay_s = max(0.05, progress_delay_s)
        self.enabled = enabled

    async def warm(self) -> None:
        """Pre-render every prompt at startup.

        In a Live voice the whole set goes through one render session and is
        cached for good, so the first filler of the day is never slower than
        a cached one. Nothing is pre-rendered in another voice.
        """
        if self.tts.live:
            await self.tts.render_live(list(PROMPTS.values()))
            return
        for text in PROMPTS.values():
            await self.tts.get(text)

    @staticmethod
    def is_filler(key: str) -> bool:
        """A filler may stay silent; an error or refusal must be heard."""
        return not key.startswith("error_")

    def is_silent(self, tool_name: str) -> bool:
        """True for a fast local read that must never be announced."""
        return bare_tool_name(tool_name) in self.silent_ack_tools

    def is_slow(self, tool_name: str, p50_s: Optional[float]) -> bool:
        # The silent list wins over BOTH the explicit slow list and the
        # measured median: a fast local read that happens to have been slow
        # once must not start narrating itself.
        if self.is_silent(tool_name):
            return False
        if tool_name in self.slow_tools or bare_tool_name(tool_name) in self.slow_tools:
            return True
        return p50_s is not None and p50_s > ACK_THRESHOLD_S

    async def say(self, key: str, device_id: str) -> bool:
        if not self.enabled:
            return False
        text = PROMPTS.get(key)
        if not text:
            return False
        pcm = self.tts.cached(text) or await self.tts.get(
            text, fallback=not self.is_filler(key))
        if not pcm:
            return False
        return await self._play(pcm, device_id)

    FALLBACK_ACK_KEYS = ("ack_sure", "ack_on_it", "ack")

    async def say_fallback_ack(self, device_id: str) -> bool:
        """One static acknowledgement, only from the cache and only in the
        assistant's own voice; never synthesized while the user waits and
        never in a second voice. Independent of ``slow_tool_ack``: it is the
        router's fallback for a silent voice, not a slow-tool cue."""
        keys = list(self.FALLBACK_ACK_KEYS)
        self._fallback_turn = getattr(self, "_fallback_turn", -1) + 1
        start = self._fallback_turn % len(keys)
        for key in keys[start:] + keys[:start]:
            pcm = self.tts.cached(PROMPTS[key])
            if pcm:
                return await self._play(pcm, device_id)
        return False

    async def say_progress(self, tool_name: str, arguments, speaker_name: Optional[str],
                           transcript: str, device_id: str) -> bool:
        """The contextual progress line for a tool, in the assistant's voice.

        In a Live voice the line is one of the finite pre-rendered cues
        (``app/progress_ack.py``), chosen from the tool's shape: no synthesis
        while the user waits, and never a second voice. Otherwise it is the
        structured, personalised phrase synthesized by the speech endpoint.
        """
        if not self.enabled:
            return False
        if self.tts.live:
            text = generic_progress_phrase(tool_name, transcript)
            pcm = self.tts.cached(text)
            if pcm is None:
                logger.info("ℹ️ progress cue not yet rendered in the Live voice; "
                            "staying silent rather than mixing voices")
                return False
            return await self._play(pcm, device_id)
        text = progress_phrase(tool_name, arguments, speaker_name, transcript=transcript)
        return await self.say_text(text, device_id)

    async def say_text(self, text: str, device_id: str, *, timeout_s: float = 1.2) -> bool:
        """Speak a request-specific progress line without persisting its text."""
        if not self.enabled or not text:
            return False
        pcm = self.tts.cached(text)
        if pcm is None and not self.tts.live:
            try:
                pcm = await asyncio.wait_for(
                    self.tts.get(text, persist=False), timeout=max(0.1, timeout_s)
                )
            except asyncio.TimeoutError:
                pcm = None
        if pcm is None:
            # Fixed, pre-warmed fallback: responsiveness beats exact wording,
            # and in a Live voice nothing is synthesized while the user waits.
            pcm = self.tts.cached(PROMPTS["ack"])
        return bool(pcm) and await self._play(pcm, device_id)


def classify_error(reason: str) -> str:
    text = (reason or "").lower()
    if "rate limit" in text or "rate_limit" in text or "429" in text:
        return "error_rate_limit"
    return "error_generic"
