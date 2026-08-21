"""The 24-hour customer service window.

WhatsApp lets a business reply freely for 24 hours after a customer writes to
it. Outside that window Meta rejects free-form messages, and only a template
approved in advance will reach the person.

This is not a detail of the transport. It is a rule about *when the business is
allowed to speak*, and it cuts straight through the escalation loop:

    6pm   customer disputes a late fee, the agent escalates
    ...   the owner goes home without answering
    9am   the owner taps Approve
          -> the reply is rejected, the case is marked resolved,
             and the customer never hears anything

That failure is silent, which is what makes it dangerous. Nobody gets an error;
the conversation simply stops.

The fix is not to cram the answer into a template. Templates take positional
parameters and get approved months in advance, so they cannot carry a decision
nobody has made yet. Instead the template's only job is to **reopen the
window** — once the customer replies, the business may speak freely again, and
the real answer is delivered then, written by the agent with the full decision
in hand.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from ..context import ToolContext
from ..store.models import Message

#: Meta's rule. Not configurable — it is theirs, not ours.
WINDOW_HOURS = 24

#: Sent when the window has closed and we still owe the customer an answer. A
#: template body is fixed at approval time, so this must say nothing specific:
#: it exists to earn a reply, not to carry the decision.
DEFAULT_REOPEN_TEMPLATE = "case_update"


def last_inbound_at(ctx: ToolContext, conversation_id: str) -> datetime | None:
    """When the customer last wrote. The clock starts here."""
    if ctx.session is None:
        return None
    return ctx.session.scalar(
        select(Message.created_at)
        .where(Message.conversation_id == conversation_id, Message.direction == "inbound")
        .order_by(Message.created_at.desc())
        .limit(1)
    )


def closes_at(ctx: ToolContext, conversation_id: str) -> datetime | None:
    opened = last_inbound_at(ctx, conversation_id)
    return opened + timedelta(hours=WINDOW_HOURS) if opened else None


def is_open(ctx: ToolContext, conversation_id: str, now: datetime | None = None) -> bool:
    """Whether a free-form message would actually be delivered right now.

    A conversation with no inbound message at all is treated as closed. That is
    the safe direction: the business has never been written to, so it has no
    licence to start talking.
    """
    deadline = closes_at(ctx, conversation_id)
    if deadline is None:
        return False
    return (now or ctx.now()) < deadline


def hours_left(ctx: ToolContext, conversation_id: str, now: datetime | None = None) -> float:
    deadline = closes_at(ctx, conversation_id)
    if deadline is None:
        return 0.0
    remaining = (deadline - (now or ctx.now())).total_seconds() / 3600
    return max(0.0, remaining)
