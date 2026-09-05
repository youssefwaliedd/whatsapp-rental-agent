"""Not saying a car is free until something has looked.

Observed: "the Audi RS3 is available for those dates", and two messages later,
"the specific unit I initially looked at is unavailable". Nothing had checked in
between — the first sentence was a guess that the second one corrected, after the
customer had already chosen on the strength of it.

Availability is the one fact this operator publishes nowhere, so it is also the
one the model has least business inferring. The prompt has always said so. This
makes it structural, the same way invented figures are: a reply that asserts a
car is free is not sent unless a tool confirmed it for the dates currently on the
table.

Deliberately not matched per vehicle. Tying the claim to a named car means
parsing model names out of prose, and getting that wrong blocks good replies. The
question asked here is the one the transcript actually failed: had *anything*
checked?
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

#: Asserting a car can be had. "Let me check whether it is available" is an
#: intention and must not match, or the agent is blocked from saying what it is
#: about to do.
CLAIMS_AVAILABLE = re.compile(
    r"\b(?:is|it'?s|are|we have|there'?s|that'?s)\s+(?:still\s+)?available\b"
    r"|\bavailable\s+(?:for|on|from|then|now)\b"
    r"|\b(?:good|great) news[^.]{0,40}\bavailable\b"
    r"|\bwe do have (?:it|one|that)\b"
    r"|\byes,?\s+(?:it|that)\s+is\b(?=[^.]{0,30}\bavailable\b)?",
    re.I,
)

#: Phrases that make an availability word into a question or an intention.
NOT_A_CLAIM = re.compile(
    r"\b(?:check|checking|see|confirm|verify|look(?:ing)? into|find out)\b[^.]{0,40}\bavailab",
    re.I,
)

#: Tools that establish whether a car is free. A quote counts because the engine
#: validates availability before pricing — a quote that came back is a check.
AVAILABILITY_TOOLS = {
    "search_available_vehicles",
    "find_alternatives",
    "create_demo_quote",
    "calculate_quote",
    "extend_demo_rental",
    "modify_demo_reservation",
}


def claims_available(reply: str) -> bool:
    text = reply or ""
    for sentence in re.split(r"[.!?؟\n]+", text):
        if NOT_A_CLAIM.search(sentence) or re.search(
            r"(?:سأتحقق|نتحقق|هتأكد|سنتأكد|ليست|غير)\s*.*(?:متاح|متوفر)", sentence
        ):
            continue
        if CLAIMS_AVAILABLE.search(sentence) or re.search(r"(?:متاحة?|متوفرة?)\b", sentence):
            return True
    return False


def _same_moment(raw: Any, moment: datetime | None) -> bool:
    """Whether a tool argument refers to the window currently being discussed."""
    if moment is None or not raw:
        return moment is None
    try:
        return datetime.fromisoformat(str(raw)).replace(second=0, microsecond=0) == (
            moment.replace(second=0, microsecond=0)
        )
    except (TypeError, ValueError):
        return False


def checked_for(tool_calls: Iterable[Any], pickup: datetime | None, ret: datetime | None) -> bool:
    """Did anything establish availability for these dates?

    Dates matter because a check against last week's window says nothing about
    this one — which is how "available" and "booked out" ended up two messages
    apart, with a search in between for different days.
    """
    for call in tool_calls:
        result = call.result if isinstance(call.result, dict) else {}
        if call.tool_name not in AVAILABILITY_TOOLS or result.get("error"):
            continue
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if pickup is not None and not _same_moment(arguments.get("pickup_at"), pickup):
            continue
        if ret is not None and not _same_moment(arguments.get("return_at"), ret):
            continue
        # A search that found nothing checked, and found nothing. Saying a car is
        # available on the back of it is exactly the failure.
        if call.tool_name == "search_available_vehicles" and not result.get("vehicles"):
            continue
        return True
    return False


CORRECTION = (
    "STOP. Your reply tells the customer a car is available, and nothing has checked "
    "whether it is free for these dates. Do not send it.\n"
    "This operator publishes no availability anywhere, so you cannot know it without "
    "asking a tool. Call search_available_vehicles for the dates they gave you, and "
    "answer from what it returns.\n"
    "If you would rather not check yet, say you are checking — do not say it is free.\n"
    "Keep everything else you wrote. Only the availability claim has to go — the "
    "customer asked you something, and dropping their question to talk about "
    "availability answers a question they did not ask."
)

#: Sent when the model asserts availability twice without checking.
SAFE_REPLY = (
    "I cannot confirm availability without checking your rental dates."
)
