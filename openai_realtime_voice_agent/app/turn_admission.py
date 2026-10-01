"""Non-negotiable admission rules for post-wake speech.

These are appended after operator-provided instructions so a generic
"ask a clarifying question when unsure" rule cannot turn television audio,
noise, or an accidental wake into an unsolicited conversation.
"""


TURN_ADMISSION_INSTRUCTIONS = """

TURN ADMISSION — THIS OVERRIDES EVERY CLARIFICATION RULE ABOVE:
Before replying or calling a tool, decide whether the current utterance is a
plausible request addressed to the assistant. On the first utterance after a
wake, a lone unclear word, foreign-looking transcription in an English-only
session, sentence fragment, background conversation, broadcast audio, or text
with no plausible request is an accidental wake. For an accidental wake,
produce no spoken output, call no tool, and ask no question. Silence is the
complete and correct response.

Ask a clarifying question only after a clearly addressed, plausible request
contains actionable intent but is missing one necessary detail. Unclear audio
is not an incomplete request. Never turn an accidental wake into a conversation.
"""


def turn_admission_instructions() -> str:
    """Return the final, highest-priority post-wake admission policy."""
    return TURN_ADMISSION_INSTRUCTIONS
