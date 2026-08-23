"""How each conversation ended: booked, dropped, or escalated.

Section 4 of the operator's brief, and the piece that turns a pile of transcripts
into something answerable. "How many of the people who asked about the deposit
booked?" and "show me every conversation that went quiet straight after a quote"
are the questions that improve a sales agent, and neither can be asked of
conversations that are all stored identically.

Kept apart from `Conversation.outcome`, which is the lifecycle marker: null there
means the thread is open, so recording a sale in it would close the thread and
greet a returning customer as a stranger.

Booked and escalated are known the moment they happen. Dropped is the absence of
anything — it can only be decided by time passing, so it is swept rather than
recorded.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..context import ToolContext
from ..store.models import Conversation, Reservation

BOOKED = "booked"
DROPPED = "dropped"
ESCALATED = "escalated"

#: Booked outranks everything: a conversation that was escalated and then closed
#: a sale is a success, and one that went quiet after booking is still a booking.
#: Dropped is the weakest — it only ever describes silence.
PRECEDENCE = {DROPPED: 0, ESCALATED: 1, BOOKED: 2}

#: Quiet for this long after a quote, with no booking, is a lost sale rather
#: than a slow one. Deliberately longer than a night: people sleep on a decision
#: about a supercar, and calling that dropped at hour three would make the number
#: meaningless.
DEFAULT_DROPPED_AFTER_HOURS = 24


def record(ctx: ToolContext, outcome: str, *, conversation_id: str | None = None) -> None:
    """Tag the conversation, unless it already carries a stronger result."""
    conversation_id = conversation_id or ctx.conversation_id
    if ctx.session is None or not conversation_id:
        return
    conversation = ctx.conversations.get(conversation_id)
    if conversation is None:
        return

    current = conversation.sales_outcome
    if current is not None and PRECEDENCE.get(current, 0) >= PRECEDENCE.get(outcome, 0):
        return
    conversation.sales_outcome = outcome
    ctx.session.flush()


def mark_booked(ctx: ToolContext) -> None:
    record(ctx, BOOKED)


def mark_escalated(ctx: ToolContext) -> None:
    record(ctx, ESCALATED)


def _dropped_after(ctx: ToolContext) -> timedelta:
    hours = ctx.engine.rules.get("reporting", {}).get(
        "dropped_after_hours", DEFAULT_DROPPED_AFTER_HOURS
    )
    return timedelta(hours=float(hours))


def sweep_dropped(ctx: ToolContext, now: Any = None) -> list[str]:
    """Tag conversations that were quoted, went quiet, and never booked.

    Runs on inbound webhooks, alongside the overdue-case check, for the same
    reason that one does: every message is an opportunity to notice something
    that has been true for a while, and it is honest about being a prototype
    without a scheduler.

    A conversation that already carries a result is left alone — booked stays
    booked when the customer stops replying, which is what booked means.
    """
    if ctx.session is None:
        return []
    now = now or ctx.now()
    cutoff = now - _dropped_after(ctx)

    quiet = ctx.session.scalars(
        select(Conversation).where(
            Conversation.sales_outcome.is_(None),
            Conversation.last_message_at.is_not(None),
            Conversation.last_message_at < cutoff,
        )
    ).all()

    tagged: list[str] = []
    for conversation in quiet:
        state = conversation.state or {}
        # No quote means no sale was ever on the table — an enquiry that went
        # nowhere is not a dropped booking, and counting it as one would bury
        # the conversations actually worth reading.
        if not state.get("quote_id"):
            continue
        booked = ctx.session.scalar(
            select(Reservation).where(
                Reservation.conversation_id == conversation.conversation_id,
                Reservation.status != "cancelled",
            )
        )
        if booked is not None:
            conversation.sales_outcome = BOOKED
            continue
        conversation.sales_outcome = ESCALATED if conversation.escalated else DROPPED
        tagged.append(conversation.conversation_id)

    ctx.session.flush()
    return tagged
