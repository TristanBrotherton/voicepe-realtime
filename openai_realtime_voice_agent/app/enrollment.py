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
import logging
import os
import re
import time
import wave
from typing import Any, Awaitable, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

ENROLL_DIR = "/share/voice-enrollment"
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
        return info


ENROLLMENT_SCRIPT = (
    "Recording is ON — everything the microphone hears is now being captured, so run "
    "this session briskly and follow the protocol exactly. You are guiding {person} "
    "through voice training. Keep YOUR replies to a few words so the recording is "
    "mostly their voice. Protocol, one step per conversational turn: "
    "(1) Tell them: repeat the phrase 'hey leonard' once, naturally, after each of "
    "your go-aheads — and after each repetition reply with only a brief prompt like "
    "'again', 'next', 'good'. Collect FIVE normal repetitions this way. "
    "(2) Ask for TWO said quickly and casually, as if walking past. "
    "(3) Ask for TWO said lazily or mumbled. "
    "(4) Ask them to step across the room and give THREE louder ones from there. "
    "(5) Ask them to speak normally for about 45 seconds — describe their day or "
    "read anything nearby; give one short encouragement halfway if they stop early. "
    "(6) Then call voice_enrollment with action 'stop', and ONLY AFTER the tool "
    "confirms, thank them briefly. If they want to pause or the session drops, they "
    "can wake you again and say 'continue voice training' — start a fresh session "
    "with the same name. Do not chat, do not explain the technology, do not comment "
    "on their performance beyond the brief prompts."
)


def get_enrollment_tool_definition() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "voice_enrollment",
        "description": (
            "Start or stop a guided voice-training (enrollment) recording session "
            "for a household member. Use when someone asks to train, teach, or "
            "enroll their voice (e.g. 'teach the assistant my voice', 'voice "
            "training'). Start it with the person's first name, then follow the "
            "returned protocol exactly. Recording captures everything the "
            "microphone hears until stopped."
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
                    "description": "First name of the person enrolling (required for start)",
                },
            },
            "required": ["action"],
        },
    }


def create_enrollment_tool_handler(
    recorder: EnrollmentRecorder,
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    async def enrollment_tool_handler(params: "FunctionCallParams") -> None:
        args = params.arguments or {}
        action = (args.get("action") or "").strip().lower()
        person = (args.get("person") or "").strip()
        try:
            if action == "start":
                if not person:
                    await params.result_callback(
                        {"error": "A first name is required to start enrollment — ask who is enrolling."}
                    )
                    return
                recorder.start(person)
                await params.result_callback(
                    {"status": "recording", "instructions": ENROLLMENT_SCRIPT.format(person=person)}
                )
            elif action == "stop":
                info = recorder.stop()
                if not info.get("path"):
                    await params.result_callback({"status": "no active enrollment session"})
                else:
                    await params.result_callback(
                        {
                            "status": "saved",
                            "person": info["person"],
                            "seconds_recorded": info["seconds"],
                            "note": "Recording is off. Thank them briefly; the household admin will process the file.",
                        }
                    )
            elif action == "status":
                await params.result_callback(
                    {"recording": recorder.active, "person": recorder.person}
                )
            else:
                await params.result_callback({"error": f"unknown action '{action}'"})
        except Exception as e:
            logger.error(f"❌ voice_enrollment failed: {e}", exc_info=True)
            try:
                recorder.stop()
            except Exception:
                pass
            await params.result_callback(
                {"error": "Enrollment hit a technical problem; recording is off. Apologize briefly."}
            )

    return enrollment_tool_handler
