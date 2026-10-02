"""What the agent says when a tool is slow (see providers/tool_registration.py).

Björn's voice, short, and never a question: a question mark would open the
follow-up mic. The persona line asks the model to say it itself before a
lookup it knows is slow; the deterministic one only fills in when it did not.
"""
import os
import random

EARLY_ACK_PHRASES = (
    "Vänta, jag kollar.",
    "Två sek, jag tittar.",
    "Jag kollar, vänta lite.",
    "Ett ögonblick, jag letar.",
    "Vänta, jag tar reda på det.",
)

EARLY_ACK_INSTRUCTION = (
    "\n\nINNAN EN LÅNGSAM UPPSLAGNING (webbsökning, delegera_till_raawr, musik, "
    "sökning i huset): säg först ett kort 'jag kollar' med egna ord, utan "
    "frågetecken, och gör sedan anropet."
)


def pick_early_ack(last=None) -> str:
    """A phrase, never the same as last time."""
    return random.choice([p for p in EARLY_ACK_PHRASES if p != last])


# The acknowledgement has to come in the answer's voice. 0.23.1 rendered it
# with OpenAI's TTS while the answer came from Gemini's Charon, and the owner
# heard two people (2026-10-02 18:12). Gemini's TTS has the same prebuilt
# voices as Live, so Charon reads the ack as well.
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
CLIP_RATE = 24000  # what the device lane plays: 24 kHz mono PCM16 (EnrollmentConductor)
CACHE_DIR = "/data/enroll_prompts"
READ_ALOUD = "Läs upp på svenska, lugnt och avslappnat: "


def to_clip_rate(pcm: bytes, mime: str) -> bytes:
    """PCM16 mono at the rate in `mime` ("audio/L16;codec=pcm;rate=24000") -> 24 kHz."""
    import numpy as np

    rate = CLIP_RATE
    for part in (mime or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == "rate" and value.isdigit():
            rate = int(value)
    if rate == CLIP_RATE:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(len(samples) * CLIP_RATE / rate)
    out = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    return out.astype(np.int16).tobytes()


async def gemini_tts(text: str, api_key: str, voice: str, model: str = "") -> bytes:
    """`text` in a Gemini prebuilt voice, as 24 kHz PCM16, cached on disk."""
    import hashlib

    from google import genai
    from google.genai import types

    model = model or os.environ.get("GEMINI_TTS_MODEL", "").strip() or GEMINI_TTS_MODEL
    path = os.path.join(
        CACHE_DIR, "gemini_" + hashlib.md5(f"{model}:{voice}:{text}".encode()).hexdigest() + ".pcm"
    )
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            return f.read()
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
    )
    pcm = b""
    # The bare phrase is refused now and then (empty candidate, finish OTHER:
    # "Två sek, jag tittar." every time, 2026-10-02). Framed as something to
    # read aloud it is spoken, and the frame is not (checked with STT).
    for _ in range(2):
        response = await client.aio.models.generate_content(
            model=model, contents=READ_ALOUD + text, config=config
        )
        content = response.candidates[0].content if response.candidates else None
        blob = content.parts[0].inline_data if content and content.parts else None
        if blob is not None and blob.data:
            pcm = to_clip_rate(blob.data, blob.mime_type)
            break
    if not pcm:
        raise ValueError("Gemini TTS returned no audio")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(pcm)
    except OSError:
        pass  # a clip that is not on disk is fetched again after a restart
    return pcm


XAI_TTS_URL = "https://api.x.ai/v1/tts"


async def xai_tts(text: str, api_key: str, voice: str) -> bytes:
    """`text` in an xAI voice, as 24 kHz PCM16, cached on disk (0.25.0).

    The ack on the xai engine comes in the session's own voice, like Charon
    on Gemini. XAI_ACK_PREFIX (default empty) goes in front of the phrase, for
    an inline speech tag such as "[breath] " -- the owner picks by ear.
    """
    import hashlib

    import httpx

    text = os.environ.get("XAI_ACK_PREFIX", "") + text
    path = os.path.join(CACHE_DIR, "xai_" + hashlib.md5(f"{voice}:{text}".encode()).hexdigest() + ".pcm")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            return f.read()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            XAI_TTS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"text": text, "voice_id": voice, "language": "sv",
                  "output_format": {"codec": "pcm", "sample_rate": CLIP_RATE}},
        )
    r.raise_for_status()
    if not r.content:
        raise ValueError("xAI TTS returned no audio")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
    except OSError:
        pass  # a clip that is not on disk is fetched again after a restart
    return r.content
