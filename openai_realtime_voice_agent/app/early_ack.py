"""What the agent says when a tool is slow (see providers/tool_registration.py).

Björn's voice, short, and never a question: a question mark would open the
follow-up mic. The persona line asks the model to say it itself before a
lookup it knows is slow; the deterministic one only fills in when it did not.
"""
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
