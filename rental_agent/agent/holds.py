"""Not telling a customer a booking is done until one is.

Where the operator keeps availability somewhere the engine cannot read, a
booking is a HOLD until a person who can see the fleet confirms it. The
mechanic has worked since `4aac8a7`; the language never followed it. Run against
real conversations the evaluator found "is reserved", "all set" and "I have
secured" eleven times on bookings that were only held — every one after the fix,
so the hold was doing its job while the message undid it.

`check_confirmed_a_hold` finds these afterwards, which is too late by
construction: by the time it reads the transcript the customer has been told the
car is theirs and may already be driving to collect it.

The rule is stated as proof, not as suspicion: confirmation language goes out
only when a booking that really is confirmed backs it. A hold does not, and
neither does no booking at all — which is the worse case of the two, and the one
a hold-shaped guard would have missed entirely.

Two tiers, because the words differ in how much they claim. "Your booking is
confirmed" says one thing and can only mean it. "All set" is a pleasantry that
happens to be one of the eleven: it is a claim about the booking when the
sentence is about the booking, and not when it is about a delivery address.
"""

from __future__ import annotations

import re

#: Sentence enders, Arabic question mark included.
_SENTENCE = re.compile(r"[.!?؟\n]+")

#: A booking reference as it appears in a message — DEMO-1042 today, whatever
#: the prefix becomes after the de-demo pass. Resolved against the database
#: rather than trusted: a quote reference matches this shape too.
REFERENCE = re.compile(r"\b([A-Z]{2,6}-\d{2,6})\b")

#: Says the booking is done, and cannot mean anything else.
CONFIRMED_CLAIM = re.compile(
    r"\b(?:is|it'?s|that'?s|you'?re|your booking is|all)\s+(?:now\s+)?"
    r"(?:booked|confirmed|reserved|secured|locked in)\b"
    r"|\b(?:i|we)(?:'ve| have)\s+(?:booked|confirmed|reserved|secured|locked in)\b"
    r"|\bbooking (?:is )?confirmed\b"
    r"|\b(?:it|that|the car|the vehicle)\s*(?:is|'?s)\s+yours\b"
    r"|\bwe'?ve got you down\b"
    # Arabic. Delta's own thirteen conversations are in English, but their
    # market is not, and a guard that only reads English fails silently.
    r"|تم\s+الحجز|تم\s+التأكيد|تم\s+تأكيد|الحجز\s+مؤكد|مؤكد\s+الحجز"
    r"|محجوزة?\b|السيارة\s+لك|السيارة\s+محجوزة",
    re.I,
)

#: Says something is done without saying what. A claim about the booking only
#: when the sentence is about the booking.
GENERIC_DONE = re.compile(
    r"\ball set\b|\byou'?re good to go\b|\ball done\b"
    r"|كل\s+شيء\s+جاهز|كلشي\s+جاهز",
    re.I,
)

#: What makes a generic phrase a claim about the booking rather than about the
#: address, the documents or the licence.
BOOKING_SUBJECT = re.compile(
    r"\b(?:booking|reservation|car|vehicle|rental|reserved|booked)\b"
    r"|الحجز|السيارة|المركبة"
    r"|\b[A-Z]{2,6}-\d{2,6}\b",
    re.I,
)

#: A confirmation that has not happened yet is the correct thing to say — "once
#: it's confirmed I'll send the link" is the wording this guard exists to
#: produce. Cut out before the search rather than used to wave the whole reply
#: through, because one honest sentence must not license a wrong one beside it.
CONDITIONAL = re.compile(
    r"\b(?:once|when|as soon as|after|until|unless|if|before|pending|subject to)\b"
    r"[^.!?؟]{0,80}?\b(?:booked|confirmed|reserved|secured)\b"
    r"|\b(?:not|isn'?t|aren'?t|won'?t be|can'?t be|cannot be)\s+(?:yet\s+)?"
    r"(?:booked|confirmed|reserved|secured)\b"
    r"|\bawaiting confirmation\b|\bnot yet confirmed\b"
    r"|\b(?:i|we)(?:'ll| will|'m| am|'re| are)\s+[^.!?؟]{0,30}?"
    r"\b(?:confirm|confirming|check|checking)\b"
    r"|\b(?:choice|decision|call)\s+is\s+yours\b"
    r"|(?:بعد|عند|بمجرد|بانتظار|في\s+انتظار|لم\s+يتم|سيتم|لن\s+يتم)"
    r"\s*[^.!?؟]{0,40}?(?:التأكيد|تأكيد|الحجز)",
    re.I,
)


class Claim:
    """What a reply asserts about a booking, and which booking.

    `explicit` is the wording that can only mean the booking is done.
    `generic` is "all set" in a sentence that is about the booking.
    `references` are the booking codes the reply names, if any — the difference
    between a customer with one confirmed booking and one held, and a guard that
    blocks a true sentence about the confirmed one.
    """

    __slots__ = ("explicit", "generic", "references")

    def __init__(self, explicit: bool, generic: bool, references: frozenset[str]):
        self.explicit = explicit
        self.generic = generic
        self.references = references

    def __bool__(self) -> bool:
        return self.explicit or self.generic

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Claim(explicit={self.explicit}, generic={self.generic}, refs={set(self.references)})"


def _is_bare(sentence: str) -> bool:
    """Whether the sentence is the pleasantry and nothing else.

    "You're all set!" is about the only thing on the table. "All set — I've
    noted Dubai Marina for the delivery" is about the address.
    """
    rest = GENERIC_DONE.sub(" ", sentence)
    return len(re.sub(r"[^\w؀-ۿ]+", "", rest)) <= 12


def inspect(reply: str) -> Claim:
    """What the reply claims, with conditional wording taken out first."""
    text = reply or ""
    references = frozenset(REFERENCE.findall(text))

    explicit = generic = False
    for sentence in _SENTENCE.split(text):
        cleaned = CONDITIONAL.sub(" ", sentence)
        if CONFIRMED_CLAIM.search(cleaned):
            explicit = True
        elif GENERIC_DONE.search(cleaned) and (
            BOOKING_SUBJECT.search(cleaned) or _is_bare(cleaned)
        ):
            generic = True
    return Claim(explicit, generic, references)


def claims_confirmed(reply: str) -> bool:
    """Whether the reply tells the customer the booking is done."""
    return bool(inspect(reply))


CORRECTION = (
    "STOP. Your reply tells the customer their booking is done. Nothing confirms that. "
    "Do not send it.\n"
    "Say the booking request is awaiting confirmation and the vehicle is not yet "
    "confirmed. Give them the reference so they have something to refer to, and say you "
    "will come back as soon as it is confirmed. Never say it is booked, confirmed, "
    "reserved, secured or theirs until it is.\n"
    "Do not say the car is being kept or set aside for them either — nothing is holding "
    "it off the market.\n"
    "If they have more than one booking with you and one of them IS confirmed, say which "
    "one you mean by its reference — otherwise they cannot tell which car you are "
    "talking about.\n"
    "Everything else you wrote can stay. Only the promise has to go."
)

NOTHING_TO_CONFIRM = (
    "STOP. Your reply tells the customer their booking is done, and there is no booking "
    "at all — nothing has been taken. Do not send it.\n"
    "Say plainly what has and has not happened, and ask for whatever you still need to "
    "take the request."
)

#: Sent when the model calls an unconfirmed booking done twice.
SAFE_REPLY = (
    "Your booking request is awaiting confirmation. The vehicle is not yet confirmed. "
    "I'll come back to you as soon as it is."
)

#: The same, for a reply that announced a booking nobody ever made.
NO_BOOKING_REPLY = (
    "Nothing is booked yet — let me get the details confirmed with you first, and I'll "
    "come back as soon as it is done."
)
