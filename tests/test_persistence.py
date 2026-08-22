"""Identity, memory and message handling.

The product promise is that the agent remembers the customer. These tests pin
the mechanics of that promise.
"""

from __future__ import annotations

from datetime import timedelta

from rental_agent.domain.enums import Stage
from rental_agent.domain.models import ConversationState
from tests.conftest import FROZEN_NOW, dt


# --------------------------------------------------------------------------
# Customer identity
# --------------------------------------------------------------------------


def test_the_same_number_is_the_same_customer(booking_ctx):
    first = booking_ctx.customers.by_whatsapp_id("+971500000001")
    again, created = booking_ctx.customers.get_or_create("+971500000001", FROZEN_NOW)
    assert created is False
    assert again.customer_id == first.customer_id


def test_a_new_number_is_a_new_customer(booking_ctx):
    other, created = booking_ctx.customers.get_or_create("+971500000002", FROZEN_NOW)
    assert created is True
    assert other.customer_id != booking_ctx.customer_id


def test_returning_customer_updates_last_seen(booking_ctx):
    later = FROZEN_NOW + timedelta(days=3)
    customer, _ = booking_ctx.customers.get_or_create("+971500000001", later)
    assert customer.last_seen_at == later


# --------------------------------------------------------------------------
# Conversation continuity
# --------------------------------------------------------------------------


def test_a_follow_up_days_later_reuses_the_same_conversation(booking_ctx):
    """A WhatsApp chat has no concept of a new conversation. Treating a
    follow-up as a fresh lead is the memory failure the product exists to fix."""
    later = FROZEN_NOW + timedelta(days=2)
    conversation, created = booking_ctx.conversations.get_or_create(
        booking_ctx.customer_id, later
    )
    assert created is False
    assert conversation.conversation_id == booking_ctx.conversation_id


def test_a_closed_conversation_starts_a_new_one(booking_ctx):
    closed = booking_ctx.conversations.get(booking_ctx.conversation_id)
    closed.outcome = "completed"
    booking_ctx.session.flush()

    fresh, created = booking_ctx.conversations.get_or_create(
        booking_ctx.customer_id, FROZEN_NOW + timedelta(days=30)
    )
    assert created is True
    assert fresh.conversation_id != booking_ctx.conversation_id


# --------------------------------------------------------------------------
# Conversation state
# --------------------------------------------------------------------------


def test_state_round_trips_without_loss(booking_ctx):
    state = booking_ctx.load_state()
    state.pickup_at = dt(4, 19)
    state.return_at = dt(7, 19)
    state.delivery_location = "Dubai Marina"
    state.stage = Stage.OPTIONS_PRESENTED
    state.presented_vehicle_ids = ["veh_13", "veh_14"]
    booking_ctx.save_state(state)

    reloaded = booking_ctx.load_state()
    assert reloaded.pickup_at == dt(4, 19)
    assert reloaded.return_at == dt(7, 19)
    assert reloaded.delivery_location == "Dubai Marina"
    assert reloaded.stage is Stage.OPTIONS_PRESENTED
    assert reloaded.presented_vehicle_ids == ["veh_13", "veh_14"]


def test_state_drives_what_is_still_missing(booking_ctx):
    state = booking_ctx.load_state()
    assert state.missing_requirements() == ["pickup_at", "return_at", "delivery_location"]

    state.pickup_at = dt(4, 19)
    state.delivery_location = "Dubai Marina"
    booking_ctx.save_state(state)

    # The agent must ask for the return time and nothing else.
    assert booking_ctx.load_state().missing_requirements() == ["return_at"]


def test_stage_is_mirrored_onto_the_conversation_row(booking_ctx):
    """Denormalised so `/demo-conversations` and the evaluator can filter by
    stage without deserialising every state blob."""
    state = booking_ctx.load_state()
    state.stage = Stage.QUOTED
    booking_ctx.save_state(state)
    assert booking_ctx.conversations.get(booking_ctx.conversation_id).stage == "quoted"


def test_state_is_stored_apart_from_the_transcript(booking_ctx):
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound",
        content="I need a black G63 this weekend",
        now=FROZEN_NOW,
    )
    conversation = booking_ctx.conversations.get(booking_ctx.conversation_id)
    assert "G63" not in str(conversation.state)


# --------------------------------------------------------------------------
# Message deduplication
# --------------------------------------------------------------------------


def test_a_repeated_provider_message_id_is_a_duplicate(booking_ctx):
    """WhatsApp retries deliveries. Without this every retry would be processed
    as a new customer message."""
    first, dup1 = booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound",
        content="book it",
        now=FROZEN_NOW,
        provider_message_id="wamid.ABC123",
    )
    second, dup2 = booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound",
        content="book it",
        now=FROZEN_NOW,
        provider_message_id="wamid.ABC123",
    )
    assert dup1 is False
    assert dup2 is True
    assert second.id == first.id
    assert len(booking_ctx.messages.for_conversation(booking_ctx.conversation_id)) == 1


def test_identical_text_without_a_provider_id_is_not_a_duplicate(booking_ctx):
    """A customer genuinely sending "yes" twice is two messages. Only the
    provider id proves a redelivery."""
    for _ in range(2):
        booking_ctx.messages.record(
            conversation_id=booking_ctx.conversation_id,
            direction="inbound",
            content="yes",
            now=FROZEN_NOW,
        )
    assert len(booking_ctx.messages.for_conversation(booking_ctx.conversation_id)) == 2


def test_messages_keep_their_order(booking_ctx):
    for text in ("one", "two", "three"):
        booking_ctx.messages.record(
            conversation_id=booking_ctx.conversation_id,
            direction="inbound",
            content=text,
            now=FROZEN_NOW,
        )
    stored = booking_ctx.messages.for_conversation(booking_ctx.conversation_id)
    assert [m.content for m in stored] == ["one", "two", "three"]


# --------------------------------------------------------------------------
# Demo references
# --------------------------------------------------------------------------


def test_the_first_demo_reservation_is_demo_1042(booking_ctx):
    """Matches the reference in the product specification."""
    assert booking_ctx.counters.next_reservation_reference() == "DEMO-1042"


def test_demo_references_are_sequential_and_unique(booking_ctx):
    references = [booking_ctx.counters.next_reservation_reference() for _ in range(5)]
    assert references == ["DEMO-1042", "DEMO-1043", "DEMO-1044", "DEMO-1045", "DEMO-1046"]
    assert len(set(references)) == 5


def test_quote_references_use_their_own_sequence(booking_ctx):
    assert booking_ctx.counters.next_quote_reference().startswith("DQ-")
    assert booking_ctx.counters.next_reservation_reference().startswith("DEMO-")


# --------------------------------------------------------------------------
# Storage fidelity
# --------------------------------------------------------------------------


def test_money_survives_the_database_exactly(booking_ctx):
    """Stored as text, not REAL. A quote read back must equal what was shown."""
    from decimal import Decimal

    booking_ctx.quotes.save(
        quote_id="DQ-TEST",
        vehicle_id="veh_13",
        payload={},
        total_charge=Decimal("7560.00"),
        deposit=Decimal("5000.00"),
        created_at=FROZEN_NOW,
        expires_at=FROZEN_NOW,
    )
    booking_ctx.session.expire_all()
    stored = booking_ctx.quotes.get("DQ-TEST")
    assert stored.total_charge == Decimal("7560.00")
    assert str(stored.total_charge) == "7560.00"


def test_timezone_survives_the_database(booking_ctx):
    """A pickup time an hour out is a missed delivery."""
    state = booking_ctx.load_state()
    state.pickup_at = dt(4, 19)
    booking_ctx.save_state(state)
    booking_ctx.session.expire_all()
    reloaded = booking_ctx.load_state()
    assert reloaded.pickup_at.utcoffset() == dt(4, 19).utcoffset()
    assert reloaded.pickup_at.hour == 19


def test_naive_datetimes_are_refused_by_storage(booking_ctx):
    """Caught in tests rather than in a delivery."""
    import pytest
    from datetime import datetime as plain_datetime
    from decimal import Decimal

    from sqlalchemy.exc import StatementError

    with pytest.raises(StatementError, match="naive datetime"):
        booking_ctx.quotes.save(
            quote_id="DQ-NAIVE",
            vehicle_id="veh_13",
            payload={},
            total_charge=Decimal("1"),
            deposit=Decimal("1"),
            created_at=plain_datetime(2026, 9, 4, 19, 0),
            expires_at=FROZEN_NOW,
        )
    booking_ctx.session.rollback()


# --------------------------------------------------------------------------
# What one conversation may know about another
#
# A customer must never be shown a trace of somebody else's chat, and a new
# conversation must not inherit the last one's working memory. What may carry
# is what a good salesperson would remember about a returning customer — their
# name and their stated preferences — and nothing else.
# --------------------------------------------------------------------------


def _conversation_for(session, number, now):
    from rental_agent.context import ToolContext
    from tests.conftest import REFERENCE_DATE

    ctx = ToolContext(session=session, now_fn=lambda: now, reference_date=REFERENCE_DATE)
    customer, _ = ctx.customers.get_or_create(number, now)
    conversation, _ = ctx.conversations.get_or_create(customer.customer_id, now)
    ctx.customer_id = customer.customer_id
    ctx.conversation_id = conversation.conversation_id
    session.flush()
    return ctx


def _say_something_distinctive(ctx, now):
    from rental_agent.tools.registry import execute_tool

    ctx.messages.record(
        conversation_id=ctx.conversation_id,
        direction="inbound",
        content="i need a bus for 50 people",
        now=now,
    )
    execute_tool(ctx, "save_customer_preference", {"key": "favourite_colour", "value": "black"})
    state = ctx.load_state()
    state.delivery_location = "American University of Sharjah"
    state.presented_vehicle_ids = ["veh_13"]
    ctx.save_state(state)


def test_a_new_customer_sees_no_trace_of_anyone_else(session):
    """The state block is what the model reads. Nothing of another customer's
    conversation may appear anywhere in it."""
    from rental_agent.agent.prompt import render_state
    from rental_agent.tools.registry import execute_tool
    from tests.conftest import FROZEN_NOW

    first = _conversation_for(session, "+971500000001", FROZEN_NOW)
    _say_something_distinctive(first, FROZEN_NOW)
    session.commit()

    second = _conversation_for(session, "+971509999999", FROZEN_NOW)
    session.commit()

    assert second.messages.for_conversation(second.conversation_id) == []
    state = second.load_state()
    assert state.delivery_location is None
    assert state.presented_vehicle_ids == []

    customer = execute_tool(second, "get_customer", {})
    assert customer.get("preferences") == {}

    block = render_state(state, now=FROZEN_NOW, customer=customer).lower()
    for leak in ("bus", "sharjah", "veh_13"):
        assert leak not in block, f"{leak!r} leaked into another customer's prompt"


def test_a_new_conversation_does_not_inherit_the_last_ones_working_memory(session):
    """Their dates, their delivery address and the cars they were shown belong
    to the conversation that established them. Carrying them into a new chat
    means answering this week's enquiry with last month's details."""
    from tests.conftest import FROZEN_NOW

    ctx = _conversation_for(session, "+971500000001", FROZEN_NOW)
    _say_something_distinctive(ctx, FROZEN_NOW)
    first_id = ctx.conversation_id
    ctx.conversations.get(first_id).outcome = "ended"
    session.commit()

    ctx = _conversation_for(session, "+971500000001", FROZEN_NOW)
    assert ctx.conversation_id != first_id

    assert ctx.messages.for_conversation(ctx.conversation_id) == []
    state = ctx.load_state()
    assert state.delivery_location is None
    assert state.presented_vehicle_ids == []


def test_what_does_survive_is_what_a_salesperson_would_remember(session):
    """A returning customer should not have to repeat their name or restate a
    preference they have already given. That is the whole of it — remembered
    preferences carry, working memory does not."""
    from rental_agent.tools.registry import execute_tool
    from tests.conftest import FROZEN_NOW

    ctx = _conversation_for(session, "+971500000001", FROZEN_NOW)
    _say_something_distinctive(ctx, FROZEN_NOW)
    ctx.conversations.get(ctx.conversation_id).outcome = "ended"
    session.commit()

    ctx = _conversation_for(session, "+971500000001", FROZEN_NOW)
    customer = execute_tool(ctx, "get_customer", {})
    assert customer["preferences"] == {"favourite_colour": "black"}
