"""The clock without the cloud (raawr US-032 AC-7).

"Vad är klockan" is answered from the local STT's text and the agent's own
clock, in the engine's voice from clips rendered at startup -- no cloud engine
is asked, so it answers offline and within a second. The reply is two cached
clips, "Klockan är fjorton" + "och tjugotvå minuter": 24 + 59 clips, not 1440.
"""
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")

_ENTAL = ["noll", "ett", "två", "tre", "fyra", "fem", "sex", "sju", "åtta", "nio",
          "tio", "elva", "tolv", "tretton", "fjorton", "femton", "sexton", "sjutton",
          "arton", "nitton"]
_TIOTAL = {20: "tjugo", 30: "trettio", 40: "fyrtio", 50: "femtio"}

# The question, not just the word: "ställ klockan på sju" or "väck mig klockan
# sex" are orders for the model. Whisper writes "var" for "vad" (2026-10-02).
_FRAGA = re.compile(
    r"^(?:(?:hej )?björn )?(?:"
    r"(?:vad|var|va|hur mycket) (?:är|e) (?:klockan|tiden)"
    r"|(?:vet du |kan du säga )?(?:vad|var|va|hur mycket) klockan är"
    r"|hur dags är det"
    r")(?: just)?(?: nu)?(?: då)?(?: björn)?$"
)


def tal(n: int) -> str:
    """0..59 as a Swedish number word."""
    if n < 20:
        return _ENTAL[n]
    tio, en = divmod(n, 10)
    return _TIOTAL[tio * 10] + (_ENTAL[en] if en else "")


def ar_klockfraga(text: Optional[str]) -> bool:
    if not text:
        return False
    ren = re.sub(r"[^\wåäö ]+", " ", text.lower())
    return bool(_FRAGA.match(" ".join(ren.split())))


def timme(h: int) -> str:
    return f"Klockan är {tal(h)}"


def minut(m: int) -> str:
    """'och tjugotvå minuter'; the full hour has no minute clip.

    Gemini TTS gives no audio for a bare number ("tjugo", "fjorton") or
    "noll två" (2026-10-07, whatever the punctuation); "och två minuter" it says.
    """
    return "och en minut" if m == 1 else f"och {tal(m)} minuter"


def delar(nu: Optional[datetime] = None) -> list[str]:
    """The clips that say the time now, in order."""
    nu = (nu or datetime.now(TZ)).astimezone(TZ)
    return [timme(nu.hour)] + ([minut(nu.minute)] if nu.minute else [])


def alla_delar() -> list[str]:
    """Every clip delar() can ask for: rendered at startup, so they play offline."""
    return [timme(h) for h in range(24)] + [minut(m) for m in range(1, 60)]
