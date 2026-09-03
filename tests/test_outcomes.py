"""How each conversation ended — their brief's section 4.

Every conversation is stored, and until now they were all stored identically. The
questions worth asking of a sales agent are comparative: how many of the people
who asked about the deposit went on to book, and which conversations went quiet
straight after a quote. Neither can be asked of an untagged pile.

The tag lives apart from `Conversation.outcome`, which means something else
entirely — null there marks the thread open, so writing "booked" into it would
close the conversation and greet a returning customer as a stranger.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rental_agent.services import outcomes
from rental_agent.services.outcomes import BOOKED, DROPPED, ESCALATED

from .conftest import FROZEN_NOW
from .test_evaluation import make_conversation


def conversation_of(ctx):
    return ctx.conversations.get(ctx.conversation_id)


# --- recording --------------------------------------------------------------


def test_a_booking_tags_the_conversation(booking_ctx):
    outcomes.mark_booked(booking_ctx)
    assert conversation_of(booking_ctx).sales_outcome == BOOKED


def test_an_escalation_tags_the_conversation(booking_ctx):
    outcomes.mark_escalated(booking_ctx)
    assert conversation_of(booking_ctx).sales_outcome == ESCALATED


def test_a_sale_that_was_escalated_first_still_counts_as_booked(booking_ctx):
    outcomes.mark_escalated(booking_ctx)
    outcomes.mark_booked(booking_ctx)
    assert conversation_of(booking_ctx).sales_outcome == BOOKED


def test_a_booking_is_not_downgraded_by_later_silence(booking_ctx):
    outcomes.mark_booked(booking_ctx)
    outcomes.record(booking_ctx, DROPPED)
    assert conversation_of(booking_ctx).sales_outcome == BOOKED


def test_the_lifecycle_marker_is_left_alone(booking_ctx):
    # Null outcome is what keeps the thread open for a returning customer.
    outcomes.mark_booked(booking_ctx)
    assert conversation_of(booking_ctx).outcome is None


# --- sweeping ---------------------------------------------------------------


def quiet_since(ctx, hours: int, *, quoted: bool = True, escalated: bool = False):
    conversation = conversation_of(ctx)
    conversation.last_message_at = FROZEN_NOW - timedelta(hours=hours)
    conversation.state = {"quote_id": "DQ-501"} if quoted else {}
    conversation.escalated = escalated
    ctx.session.flush()
    return conversation


def test_quoted_then_quiet_is_dropped(booking_ctx):
    quiet_since(booking_ctx, 30)
    assert outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    assert conversation_of(booking_ctx).sales_outcome == DROPPED


def test_quiet_but_not_long_enough_is_left_alone(booking_ctx):
    quiet_since(booking_ctx, 3)
    outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    assert conversation_of(booking_ctx).sales_outcome is None


def test_an_enquiry_that_never_reached_a_quote_is_not_a_dropped_booking(booking_ctx):
    # Counting these would bury the conversations actually worth reading.
    quiet_since(booking_ctx, 48, quoted=False)
    outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    assert conversation_of(booking_ctx).sales_outcome is None


def test_a_quiet_conversation_that_was_escalated_is_tagged_escalated(booking_ctx):
    quiet_since(booking_ctx, 48, escalated=True)
    outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    assert conversation_of(booking_ctx).sales_outcome == ESCALATED


def test_going_quiet_after_booking_is_still_a_booking(booking_ctx):
    outcomes.mark_booked(booking_ctx)
    quiet_since(booking_ctx, 72)
    outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    assert conversation_of(booking_ctx).sales_outcome == BOOKED


def test_the_sweep_is_idempotent(booking_ctx):
    quiet_since(booking_ctx, 30)
    first = outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    second = outcomes.sweep_dropped(booking_ctx, now=FROZEN_NOW)
    assert first and not second


def test_the_threshold_is_configurable(booking_ctx):
    assert outcomes._dropped_after(booking_ctx) >= timedelta(hours=12)


def test_booking_through_the_tools_tags_it(booking_ctx):
    """The tag has to happen on the real path, not only when called directly."""
    from rental_agent.tools.registry import execute_tool

    from .conftest import dt

    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(
        booking_ctx,
        "create_demo_quote",
        {
            "vehicle_id": vehicle.id,
            "pickup_at": dt(10, 17).isoformat(),
            "return_at": dt(12, 17).isoformat(),
            "delivery_location": "Dubai Marina",
        },
    )
    assert "error" not in quote, quote
    booked = execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    assert "error" not in booked, booked
    assert conversation_of(booking_ctx).sales_outcome == BOOKED


def test_escalating_through_the_tools_tags_it(booking_ctx):
    from rental_agent.tools.registry import execute_tool

    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id, direction="inbound",
        content="I've just had an accident", now=booking_ctx.now(),
    )
    execute_tool(booking_ctx, "escalate_conversation", {"reason": "accident"})
    assert conversation_of(booking_ctx).sales_outcome == ESCALATED


# --------------------------------------------------------------------------
# A finished conversation judges itself
# --------------------------------------------------------------------------
#
# The evaluation step is the half of the loop that is safe to automate: it
# consults no model, costs nothing, and changes nothing a customer sees. Turning
# what it finds into lessons, proving them and activating them stay deliberate.


def _ended(ctx, outcome="dropped", hours_ago=48):
    from datetime import timedelta

    make_conversation(ctx, [("inbound", "how much for the G63?"), ("outbound", "AED 4,321 total.")])
    conversation = ctx.conversations.get(ctx.conversation_id)
    conversation.sales_outcome = outcome
    conversation.last_message_at = ctx.now() - timedelta(hours=hours_ago)
    ctx.session.flush()
    return conversation


def test_a_finished_conversation_is_evaluated_without_being_asked(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_finished, open_mistakes

    _ended(booking_ctx)

    assert evaluate_finished(booking_ctx) == [booking_ctx.conversation_id]
    assert any(m.type == "unsupported_claim" for m in open_mistakes(booking_ctx))


def test_a_conversation_still_going_is_left_alone(booking_ctx):
    """Booking is not the end — the customer usually keeps talking about
    delivery, and judging it there would judge half a conversation."""
    from rental_agent.evaluation.evaluator import evaluate_finished

    _ended(booking_ctx, outcome="booked", hours_ago=1)

    assert evaluate_finished(booking_ctx) == []


def test_an_untagged_conversation_is_left_alone(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_finished

    make_conversation(booking_ctx, [("inbound", "hi"), ("outbound", "Hello!")])

    assert evaluate_finished(booking_ctx) == []


def test_a_conversation_is_judged_once_and_only_once(booking_ctx):
    """Mistake counts track distinct conversations, so judging one twice would
    inflate the number that decides which habits are worth correcting."""
    from rental_agent.evaluation.evaluator import evaluate_finished, open_mistakes

    _ended(booking_ctx)
    evaluate_finished(booking_ctx)
    counts = [(m.type, m.occurrences) for m in open_mistakes(booking_ctx)]

    assert evaluate_finished(booking_ctx) == []
    assert [(m.type, m.occurrences) for m in open_mistakes(booking_ctx)] == counts


def test_replay_traffic_is_never_judged_as_a_customer(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_finished
    from rental_agent.evaluation.replay import REPLAY_OUTCOME

    conversation = _ended(booking_ctx)
    conversation.outcome = REPLAY_OUTCOME
    booking_ctx.session.flush()

    assert evaluate_finished(booking_ctx) == []
