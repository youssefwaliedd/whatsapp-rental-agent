"""Turning a detected mistake into a lesson the agent can act on.

The specification draws a hard line through what may be learned automatically.
Conversation tactics, question ordering, objection handling, when to offer
alternatives — all learnable. Prices, availability, discount limits, insurance
terms, legal rules — never. Those come from approved configuration, and a
customer's claim about them is not evidence.

That boundary is enforced here rather than trusted, because it is the one
failure that would be genuinely dangerous: a learning loop that absorbed "you
always give me 20% off" into the agent's standing instructions would have
corrupted the system's single most important property.

`reject_unsafe_lesson` is the gate. A lesson carrying a figure, a percentage or
a policy claim is refused, and no strategy can ever contain one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A lesson quoting money or a percentage is asserting a business fact.
_MONEY = re.compile(r"(?:AED|\$|USD)\s*\d|(?<!\w)\d{3,}(?!\w)|\d+\s*%")

#: Phrases that make a lesson a policy claim rather than a tactic.
_POLICY_CLAIM = re.compile(
    r"\b(the (?:price|rate|deposit|discount|limit|excess|fee) (?:is|are|should be)|"
    r"always (?:give|offer|charge|apply)|customers? (?:get|receive|are entitled)|"
    r"free (?:delivery|cancellation) (?:is|for)|minimum age is|policy is)\b",
    re.IGNORECASE,
)


class UnsafeLesson(ValueError):
    """A lesson that would teach the agent a business fact rather than a tactic."""


@dataclass(frozen=True)
class Lesson:
    """One correction, traceable to the mistake that produced it."""

    mistake_type: str
    text: str
    severity: str


def reject_unsafe_lesson(text: str) -> None:
    """Raise if a lesson asserts a business fact.

    The safety boundary the specification requires, enforced as a gate rather
    than a guideline: nothing that reaches a strategy can carry a figure or a
    policy claim, so no amount of learning can move a price.
    """
    if _MONEY.search(text):
        raise UnsafeLesson(
            f"lesson states a figure and would teach a business fact: {text!r}"
        )
    if _POLICY_CLAIM.search(text):
        raise UnsafeLesson(f"lesson asserts policy rather than tactics: {text!r}")


#: How each detected mistake becomes standing guidance. Written as instructions
#: to the agent, in the same voice as the system prompt, and deliberately about
#: *behaviour* — none of them can carry a number.
CORRECTIONS: dict[str, str] = {
    "unsupported_claim": (
        "Never state a price, total, deposit or limit you have not obtained from a "
        "tool in this conversation. If you do not have the figure, call the tool and "
        "quote what it returns — do not estimate it, round it, or carry it over from "
        "earlier."
    ),
    "missed_escalation": (
        "When a customer mentions an accident, an injury, the police, a theft or a "
        "breakdown, hand over to a colleague before anything else. Ask first whether "
        "they are safe, then stop selling entirely."
    ),
    "unauthorised_discount": (
        "Never name a discount before checking what is permitted for that specific "
        "car and those specific dates. Check first, then offer — and offer below your "
        "limit so you have room to move."
    ),
    "repeated_question": (
        "Read the state block before every reply and ask only for what is genuinely "
        "missing. If the customer has already told you something, use it rather than "
        "asking again."
    ),
    "no_alternatives_offered": (
        "Never tell a customer a car is unavailable without having substitutes in "
        "hand. Find the alternatives first, then lead with what you can do rather "
        "than with the refusal."
    ),
    "too_many_options": (
        "Offer two or three cars, never more, and say why each one suits this "
        "customer. A longer list reads as a catalogue and converts worse."
    ),
}


def lesson_for(mistake_type: str, fallback: str | None = None) -> Lesson | None:
    """The standing correction for a mistake type, if one is safe to teach."""
    text = CORRECTIONS.get(mistake_type) or fallback
    if not text:
        return None
    reject_unsafe_lesson(text)
    return Lesson(mistake_type=mistake_type, text=text, severity="medium")


def lessons_from(mistakes) -> list[Lesson]:
    """Corrections for a set of detected mistakes, worst first, deduplicated.

    A mistake with no safe correction is skipped rather than guessed at — the
    loop declining to learn something is a good outcome, not a gap.
    """
    order = {"high": 0, "medium": 1, "low": 2}
    lessons: list[Lesson] = []
    seen: set[str] = set()

    for mistake in sorted(mistakes, key=lambda m: (order.get(m.severity, 3), -m.occurrences)):
        if mistake.type in seen:
            continue
        try:
            lesson = lesson_for(mistake.type, fallback=mistake.correct_behavior)
        except UnsafeLesson:
            continue
        if lesson is None:
            continue
        seen.add(mistake.type)
        lessons.append(Lesson(mistake.type, lesson.text, mistake.severity))

    return lessons
