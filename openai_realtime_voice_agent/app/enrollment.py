"""Voice enrollment: guided, on-device capture of a household member's voice.

Broader goal than wake words: one guided session per person yields (a) real
wake-phrase positives for microWakeWord retraining, and (b) natural-speech
audio suitable for voice-print (speaker-ID) enrollment later. The user starts
it by voice ("I want to teach you my voice"); the model calls the
voice_enrollment tool and then FOLLOWS THE SCRIPT the tool returns, keeping the
conversation loop alive turn by turn while this recorder dumps every inbound
mic frame to a WAV.

Files land in /share/voice-enrollment/<person>_<timestamp>.wav (16 kHz mono
PCM16). /share persists across add-on rebuilds and is reachable from the HA
host, from where recordings are pulled into the household's private sample
store. THESE ARE PERSONAL DATA: never commit them to a repo.
"""
import asyncio
import logging
import os
import re
import time
import wave
from typing import Any, Awaitable, Callable, Dict, Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

ENROLL_DIR = "/share/voice-enrollment"


async def _set_wake_sound(on: bool) -> None:
    """Toggle the device's wake-chime switch during enrollment (best effort).

    The chime otherwise plays over the guidance every time a wake-phrase
    repetition re-wakes the device (observed live — made instructions
    inaudible). Entity id comes from the WAKE_SOUND_ENTITY option; empty = skip.
    """
    entity = os.environ.get("WAKE_SOUND_ENTITY", "").strip()
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not entity or not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"http://supervisor/core/api/services/switch/turn_{'on' if on else 'off'}",
                headers={"Authorization": f"Bearer {token}"},
                json={"entity_id": entity},
            )
            r.raise_for_status()
        logger.info(f"🔔 wake sound {'restored' if on else 'muted'} ({entity})")
    except Exception as e:
        logger.warning(f"⚠️ could not toggle wake sound {entity}: {e!r}")
SAMPLE_RATE = 16000
MAX_SESSION_SECONDS = 15 * 60  # hard stop so a forgotten session can't record forever


class EnrollmentRecorder:
    """Continuous mic-stream recorder, toggled by the voice_enrollment tool."""

    def __init__(self):
        self._wav: Optional[wave.Wave_write] = None
        self.person: Optional[str] = None
        self.path: Optional[str] = None
        self._started_at: float = 0.0

    @property
    def active(self) -> bool:
        return self._wav is not None

    def start(self, person: str) -> str:
        if self._wav is not None:
            self.stop()
        safe = re.sub(r"[^a-z0-9_]+", "", person.lower().replace(" ", "_")) or "unknown"
        os.makedirs(ENROLL_DIR, exist_ok=True)
        path = os.path.join(ENROLL_DIR, f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}.wav")
        w = wave.open(path, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        self._wav = w
        self.person = safe
        self.path = path
        self._started_at = time.monotonic()
        logger.info(f"🎓 voice enrollment started for '{safe}' → {path}")
        try:
            import asyncio as _a
            from .ha_sensors import PUBLISHER
            _a.get_running_loop().create_task(PUBLISHER.enrollment(True))
        except Exception:
            pass
        return path

    def feed(self, pcm: bytes) -> None:
        if self._wav is None:
            return
        if time.monotonic() - self._started_at > MAX_SESSION_SECONDS:
            logger.warning("🎓 enrollment hit the 15-minute safety cap — stopping")
            self.stop()
            return
        try:
            self._wav.writeframes(pcm)
        except Exception as e:
            logger.warning(f"⚠️ enrollment write failed, stopping: {e!r}")
            self.stop()

    def stop(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"person": self.person, "path": self.path, "seconds": 0.0}
        w, self._wav = self._wav, None
        if w is not None:
            try:
                frames = w.getnframes()
                info["seconds"] = round(frames / SAMPLE_RATE, 1)
                w.close()
            except Exception as e:
                logger.warning(f"⚠️ enrollment close failed: {e!r}")
        if info["path"]:
            logger.info(
                f"🎓 voice enrollment stopped for '{info['person']}' — "
                f"{info['seconds']}s captured at {info['path']}"
            )
        self.person = None
        self.path = None
        try:
            import asyncio as _a
            from .ha_sensors import PUBLISHER
            _a.get_running_loop().create_task(PUBLISHER.enrollment(False))
        except Exception:
            pass
        return info




def get_false_alarm_tool_definition() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "mark_false_wake",
        "description": (
            "Mark the most recent wake as a FALSE trigger. Use when the user says "
            "the device woke by mistake — e.g. 'that was a false alarm', 'nobody "
            "called you', 'you weren't being spoken to'. Confirms in one short "
            "sentence; no apology beyond that."
        ),
        "parameters": {"type": "object", "properties": {}},
    }


def create_false_alarm_tool_handler() -> Callable[["FunctionCallParams"], Awaitable[None]]:
    async def false_alarm_handler(params: "FunctionCallParams") -> None:
        try:
            probes_dir = "/share/voice-probes"
            files = sorted(
                f for f in os.listdir(probes_dir)
                if f.startswith("probe_") and f.endswith(".wav")
            )
            if not files:
                await params.result_callback({"status": "no recent wake capture found"})
                return
            latest = files[-1]
            marked = latest.replace("probe_", "falsewake_", 1)
            os.rename(os.path.join(probes_dir, latest), os.path.join(probes_dir, marked))
            logger.info(f"🏷️ marked false wake: {marked}")
            try:
                from .ha_sensors import PUBLISHER
                await PUBLISHER.false_wake()
            except Exception:
                pass
            await params.result_callback(
                {"status": "marked", "note": "Logged as a false trigger for retraining. Confirm briefly."}
            )
        except Exception as e:
            logger.error(f"❌ mark_false_wake failed: {e}", exc_info=True)
            await params.result_callback({"error": "could not mark it; say so briefly"})

    return false_alarm_handler


def get_enrollment_tool_definition() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "voice_enrollment",
        "description": (
            "Start or stop a guided voice-training (enrollment) recording session "
            "for a household member. Use when someone asks to train, teach, or "
            "enroll their voice (e.g. 'teach the assistant my voice', 'voice "
            "training', 'continue voice training'). Call start IMMEDIATELY and "
            "WITHOUT a person name — the system identifies the speaker by voice "
            "automatically (never ask who is enrolling unless the tool says it "
            "could not identify them, or they are enrolling someone else). Then "
            "follow the returned protocol exactly. Recording captures everything "
            "the microphone hears until stopped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "status"],
                    "description": "start a session, stop the current one, or check status",
                },
                "person": {
                    "type": "string",
                    "description": (
                        "First name of the person enrolling. OPTIONAL: leave it out "
                        "and the system uses the voice-identified speaker "
                        "automatically. Only provide it when enrolling someone "
                        "other than the current speaker."
                    ),
                },
            },
            "required": ["action"],
        },
    }


def create_enrollment_tool_handler(
    conductor: "EnrollmentConductor",
    get_speaker_name: Optional[Callable[[], Optional[str]]] = None,
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    async def enrollment_tool_handler(params: "FunctionCallParams") -> None:
        args = params.arguments or {}
        action = (args.get("action") or "").strip().lower()
        person = (args.get("person") or "").strip()
        try:
            if action == "start":
                if conductor.running:
                    await params.result_callback({"status": "a session is already running"})
                    return
                if not person and get_speaker_name is not None:
                    # The voice verdict races this tool call (the probe needs
                    # ~3 s of mic audio) — wait for it briefly instead of asking
                    # a question the VAD tends to drop.
                    for _ in range(12):  # up to ~6 s
                        person = (get_speaker_name() or "").strip()
                        if person:
                            break
                        await asyncio.sleep(0.5)
                if not person:
                    await params.result_callback(
                        {"error": (
                            "Could not identify the speaker by voice. Ask for their "
                            "first name, then call start again with person set — and "
                            "tell them to answer promptly."
                        )}
                    )
                    return
                await _set_wake_sound(False)
                conductor.start(person)
                await params.result_callback(
                    {"status": "guided session running on the device",
                     "instructions": (
                         "An automated audio coach now guides them directly — you are "
                         "NOT involved. Say one very short acknowledgment (a few "
                         "words), then output NOTHING further: no commentary, no "
                         "questions, no tool calls, until this tool reports again."
                     )}
                )
            elif action == "stop":
                await conductor.stop()
                await _set_wake_sound(True)
                await params.result_callback(
                    {"status": "stopped", "note": "Recording is off. Thank them briefly."}
                )
            elif action == "status":
                await params.result_callback(
                    {"recording": conductor.recorder.active, "person": conductor.recorder.person,
                     "session_running": conductor.running}
                )
            else:
                await params.result_callback({"error": f"unknown action '{action}'"})
        except Exception as e:
            logger.error(f"❌ voice_enrollment failed: {e}", exc_info=True)
            try:
                await conductor.stop()
                await _set_wake_sound(True)
            except Exception:
                pass
            await params.result_callback(
                {"error": "Enrollment hit a technical problem; recording is off. Apologize briefly."}
            )

    return enrollment_tool_handler

class EnrollmentConductor:
    """Fixed-schedule audio coach for firmware enrollment mode.

    No conversation mechanics: the device is put into enrollment mode (mic
    pinned open, wake/stop models disarmed), mic audio flows only to the
    recorder, and guidance is pre-scripted TTS pushed down the speaker lane on
    a timed schedule. Cancellable at any moment (device button, tool stop,
    WS drop)."""

    CHUNK = 4800          # 100 ms of 24 kHz mono PCM16
    REP_GAP_S = 4.5

    def __init__(self, recorder, broadcast_json, broadcast_bytes, api_key,
                 phrase="hey leonard", tts_voice="fable"):
        self.recorder = recorder
        self.broadcast_json = broadcast_json
        self.broadcast_bytes = broadcast_bytes
        self.api_key = api_key
        self.phrase = phrase or "your wake word"
        self.tts_voice = tts_voice or "fable"
        self._task = None
        self.on_finished = None   # async callback(info: dict) after stop

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def _tts(self, text):
        """Synthesize one prompt to 24 kHz mono PCM16, cached in /data."""
        import hashlib
        os.makedirs("/data/enroll_prompts", exist_ok=True)
        key = hashlib.md5(f"{self.tts_voice}:{text}".encode()).hexdigest()
        path = f"/data/enroll_prompts/{key}.pcm"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as f:
                return f.read()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": "gpt-4o-mini-tts", "voice": self.tts_voice,
                      "input": text, "response_format": "pcm",
                      "instructions": "Calm, composed British butler. Brisk but unhurried."},
            )
            r.raise_for_status()
            pcm = r.content
        with open(path, "wb") as f:
            f.write(pcm)
        return pcm

    async def _say(self, text):
        pcm = await self._tts(text)
        for i in range(0, len(pcm), self.CHUNK):
            await self.broadcast_bytes(pcm[i:i + self.CHUNK])
            await asyncio.sleep(0.095)

    def start(self, person):
        if self.running:
            return False
        self._task = asyncio.get_running_loop().create_task(self._run(person))
        return True

    async def stop(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._finish()

    async def _finish(self):
        info = self.recorder.stop() if self.recorder.active else {}
        try:
            await self.broadcast_json({"type": "enroll", "mode": "stop"})
        except Exception:
            pass
        if self.on_finished is not None and info.get("path"):
            try:
                await self.on_finished(info)
            except Exception:
                pass
        return info

    async def _run(self, person):
        p = self.phrase
        try:
            self.recorder.start(person)
            await self.broadcast_json({"type": "enroll", "mode": "start"})
            await asyncio.sleep(0.8)
            await self._say(
                f"Voice training. Each time I say 'next', say '{p}' once, "
                f"naturally. Twenty-five in all, in a few different styles. "
                f"Press the button on top at any time to stop. Here we go."
            )
            await asyncio.sleep(1.0)
            styles = {9: "Now quickly, as if walking past.",
                      14: "Now lazily. Mumble it.",
                      19: "Now from across the room, louder.",
                      25: "Last one. Any way you like."}
            for rep in range(1, 26):
                if rep in styles:
                    await self._say(styles[rep])
                    await asyncio.sleep(0.6)
                await self._say("Next, please.")
                await asyncio.sleep(self.REP_GAP_S)
            await self._say(
                "Well done. Now talk normally for about ninety seconds. "
                "Describe your day, read something nearby, or simply ramble. "
                "Starting now."
            )
            await asyncio.sleep(45)
            await self._say("Keep going.")
            await asyncio.sleep(45)
            await self._say("That's everything. Session complete. Thank you.")
            await asyncio.sleep(2)
            await self._finish()
            logger.info("🎓 enrollment conductor finished normally")
        except asyncio.CancelledError:
            logger.info("🎓 enrollment conductor cancelled")
            raise
        except Exception as e:
            logger.error(f"❌ enrollment conductor failed: {e}", exc_info=True)
            await self._finish()
