"""Let the model hold the microphone open, instead of the house guessing.

The device has always been able to do this. `va_client` accepts
`{"type":"request_follow_up"}`, waits for the reply's audio to finish playing,
plays the chime and opens the microphone -- and its own comment says why it
exists: "Server's model called the request_follow_up tool -- it asked a
question and wants the user to answer without saying a wake word." The tool it
names has never existed on this side.

So the window was opened on a timer instead: `follow_up_ms`, sent once at
connect, applied by the device after EVERY reply. Say "that was all", get
"Bra. Hörs." back, and the microphone still opened for another eight seconds,
with a chime, listening to the room for no reason. The system prompt even
promises the opposite -- "the house keeps the microphone open exactly as long
as your reply ends with a question mark" -- which nothing implemented.

Reading the reply's text for a question mark would be the obvious fix and is
the wrong one: on Gemini the assistant transcript does not reliably reach this
pipeline at all (measured 2026-09-09 -- every user line logged, not one
assistant line), so the rule would silently never fire. A tool is explicit,
works the same on both engines, and puts the decision where the decision
actually is: with the model that just chose what to say.

WHEN it is sent matters as much as whether. The tool is called BEFORE the
reply is spoken, and the device opens the microphone as soon as its speaker
has drained -- which, at tool-call time, it usually has. Sending it there
would open the microphone before the question is asked. So the call only
RECORDS the request; PhaseEmitter sends it when the engine says the turn is
complete, with the answer's audio already queued for the device to wait out.
"""
import logging
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)


def get_follow_up_tool_definition() -> Dict[str, Any]:
    """The tool as the model sees it.

    Written to be self-explanatory without help from the system prompt --
    tool selection belongs in the tool layer, not in the prompt.
    """
    return {
        "type": "function",
        "name": "request_follow_up",
        "description": (
            "Keep the mic open after this reply so the user can answer without "
            "the wake word. Call it in the SAME turn as a reply ending in a "
            "question you need answered (which device, which room, yes or no). "
            "Do NOT call it after a finished answer, confirmation or goodbye: "
            "the open mic hears the TV and your own echo."
        ),
        "parameters": {"type": "object", "properties": {}},
    }


def create_follow_up_tool_handler(
    note_request: Callable[[], None],
) -> Callable[[Any], Awaitable[None]]:
    """Build the handler for one device.

    Args:
        note_request: Called with no arguments to record that this turn's
            reply wants the microphone held open. Deliberately NOT the send
            itself -- see this module's docstring for why the send waits for
            the end of the turn.

    Returns:
        The async handler to register as "request_follow_up".
    """

    async def handler(params) -> None:
        try:
            note_request()
            logger.info("🎤 model asked to keep the mic open for an answer")
            await params.result_callback(
                {"status": "the microphone will stay open after your reply"}
            )
        except Exception as e:
            # A failure here costs a follow-up window, not a turn: the user can
            # still answer by saying the wake word. Never let it kill the reply.
            logger.warning(f"⚠️ request_follow_up failed: {e!r}")
            await params.result_callback({"error": "could not hold the mic open"})

    return handler
