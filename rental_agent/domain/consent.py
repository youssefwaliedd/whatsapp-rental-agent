"""Whether the customer has actually asked to book yet.

Observed live: "dubai marina. first show me the pictures o that car" created a
reservation. The address was the answer to a question, and *first* was the
customer saying not yet — and a booking was taken anyway. The same shape as the
older one where a mistyped command was read as consent.

A booking is the one action in this system that is hard to take back: it puts a
reference in front of the customer, raises a case with the owner and, where a
request keeps a car, takes it off the market. So it is worth being deterministic
about, in the same way an incident is: the model may propose a booking, and a
gate decides whether the customer's own words permit it.

Deliberately narrow. This does not try to detect consent — that would block
every real booking the moment the phrasing surprised it. It detects the customer
explicitly deferring, which is a much smaller and more reliable thing to spot.
"""

from __future__ import annotations

import re

#: The customer putting something before the booking. "First show me", "before
#: we book", "not yet", "hold on".
DEFERS = re.compile(
    r"\bfirst\b"
    r"|\bbefore (?:we|you|i|booking|that)\b"
    r"|\bnot yet\b|\bnot right now\b|\bnot now\b"
    r"|\bhold on\b|\bhang on\b|\bwait\b"
    r"|\bdon'?t book\b|\bdo not book\b"
    r"|\blet me (?:think|see|check|look)\b"
    r"|\bjust (?:show|send|tell|checking|looking|browsing)\b"
    r"|\bcan i see\b(?![^.!?]{0,20}\bthen book\b)",
    re.I,
)

#: An explicit instruction that outranks a deferral in the same breath — "show
#: me the pictures first, then book it" means book it.
OVERRIDES = re.compile(
    r"\b(?:then|after that|afterwards)\s+(?:go ahead and\s+)?(?:book|reserve|confirm)\b"
    r"|\bbook it (?:anyway|now)\b",
    re.I,
)


def defers_booking(message: str | None) -> bool:
    """Whether this message asks for something *before* a booking is made."""
    text = message or ""
    if OVERRIDES.search(text):
        return False
    return bool(DEFERS.search(text))


GUIDANCE = (
    "The customer asked for something before booking — they said so in this message. "
    "Do not create the booking. Do what they asked for first, then ask whether they "
    "would like you to go ahead. Booking over a 'first' or a 'not yet' takes a decision "
    "away from them that is theirs to make."
)
