"""Which customer messages get an emoji, and which must never get one.

A reaction is the cheapest acknowledgement WhatsApp has: no notification sound,
no message in the thread, just a mark on the thing the customer sent. Used well
it says *done* without spending a message. Used badly it is memorable for the
wrong reasons.

So the rule here matches the rule everywhere else in this system: **a reaction
reports what the engine did, not what the model felt.** It is derived from the
tools that actually succeeded in the turn, which means the agent cannot decide
to celebrate something that did not happen, and cannot be talked into a reaction
by a customer.

The silences are deliberate and configured explicitly rather than left to
omission. A thumbs-up on *"I've just had an accident"* would be the single most
damaging message this system could send, so escalation maps to `null` in
`rules.json` and is asserted by a test.
"""

from __future__ import annotations

from typing import Any

from ..config import load_rules

#: Which successful tool earns which configured reaction. Order matters: a turn
#: that both books and schedules delivery is a booking, and the confirmation is
#: the thing worth marking.
_TOOL_REACTIONS = [
    ("create_demo_reservation", "on_booking_confirmed"),
    ("cancel_demo_reservation", "on_reservation_cancelled"),
    ("extend_demo_rental", "on_rental_extended"),
    ("modify_demo_reservation", "on_reservation_modified"),
]

#: Used when `rules.json` has no `messaging.reactions` block.
DEFAULTS: dict[str, str | None] = {
    "on_booking_confirmed": "✅",
    "on_reservation_modified": "👍",
    "on_rental_extended": "👍",
    "on_reservation_cancelled": "👍",
    "on_escalation": None,
    "on_unreadable_message": None,
}


def configured(rules=None) -> dict[str, str | None]:
    block = (rules or load_rules()).messaging.get("reactions", {})
    merged = {**DEFAULTS}
    merged.update({k: v for k, v in block.items() if not k.startswith("_")})
    return merged


def for_turn(turn: Any, rules=None) -> str | None:
    """The emoji to put on the customer's message, or None to stay silent.

    Escalation wins over everything. A turn can both hand over to a human and
    have touched a reservation on the way, and in that case the right number of
    emoji is zero — whatever else happened, the customer is in trouble.
    """
    marks = configured(rules)

    if getattr(turn, "escalated", False):
        return marks.get("on_escalation") or None

    # `tools_succeeded`, never `tool_calls` — the latter records what was
    # attempted, and ticking a booking that errored would be a false claim
    # delivered as an emoji.
    succeeded = set(getattr(turn, "tools_succeeded", []) or [])
    for tool_name, key in _TOOL_REACTIONS:
        if tool_name in succeeded:
            return marks.get(key) or None
    return None
