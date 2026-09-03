"""The booking connector, and the failures a real one will eventually produce.

The point of the interface is that everything above it — the tools, the guards,
the wording — is written against seven operations and six outcomes, so Delta's
system arrives as a new class rather than a rewrite. These tests are written
against the interface for the same reason: they should still mean something when
the provider underneath them is not ours.
"""

from __future__ import annotations

import pytest

from rental_agent.booking_provider import (
    Outcome,
    ProviderNotConfigured,
    SimulatedProvider,
    build_provider,
)
from rental_agent.tools.registry import execute_tool

from .conftest import dt


@pytest.fixture
def provider(booking_ctx):
    return booking_ctx.provider


def _quote(ctx, *, day=10, hours=(17, 17), vehicle=None):
    vehicle = vehicle or ctx.engine.list_fleet()[0]
    answer = ctx.provider.quote(
        vehicle_id=vehicle.id,
        pickup_at=dt(day, hours[0]),
        return_at=dt(day + 2, hours[1]),
        delivery_location="Dubai Marina",
    )
    assert answer.outcome is Outcome.CONFIRMED, answer.message
    return answer, vehicle


def _other_customer(ctx, phone="+971500000042"):
    from rental_agent.context import ToolContext

    other = ToolContext(
        session=ctx.session, now_fn=ctx.now_fn, reference_date=ctx.reference_date
    )
    customer, _ = other.customers.get_or_create(phone, ctx.now())
    conversation, _ = other.conversations.get_or_create(customer.customer_id, ctx.now())
    other.customer_id = customer.customer_id
    other.conversation_id = conversation.conversation_id
    return other


# --------------------------------------------------------------------------
# The journey
# --------------------------------------------------------------------------


def test_a_demo_booking_goes_through(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    answer = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="k1"
    )

    assert answer.outcome is Outcome.CONFIRMED
    assert answer.reference.startswith("DEMO-")
    assert answer.is_demonstration is True


def test_a_car_that_is_not_free_is_refused_with_a_reason(booking_ctx, provider):
    quote, vehicle = _quote(booking_ctx)
    provider.record_external_booking(
        vehicle_id=vehicle.id, pickup_at=dt(10, 17), return_at=dt(12, 17), who="phone"
    )

    answer = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="k2"
    )

    assert answer.outcome is Outcome.UNAVAILABLE
    assert answer.reason == "vehicle_taken"


def test_the_car_goes_between_the_quote_and_the_booking(booking_ctx, provider):
    """The reason an availability check is only true until somebody else acts.
    Nothing in the agent's path knows the phone rang."""
    quote, vehicle = _quote(booking_ctx)
    assert provider.check_availability(vehicle.id, dt(10, 17), dt(12, 17)).available is True

    provider.record_external_booking(
        vehicle_id=vehicle.id, pickup_at=dt(11, 9), return_at=dt(11, 20), who="walk-in"
    )

    assert provider.check_availability(vehicle.id, dt(10, 17), dt(12, 17)).available is False
    answer = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="k3"
    )
    assert answer.outcome is Outcome.UNAVAILABLE


def test_two_customers_one_car_exactly_one_wins(booking_ctx, provider):
    """Both hold a valid quote for the same car and window — quoted before
    either books, which is the only version of this race that is interesting.
    Quoting second and being refused proves nothing about confirming."""
    quote_a, vehicle = _quote(booking_ctx)
    rival = _other_customer(booking_ctx)
    quote_b = rival.provider.quote(
        vehicle_id=vehicle.id, pickup_at=dt(10, 17), return_at=dt(12, 17)
    )
    assert quote_b.outcome is Outcome.CONFIRMED

    mine = provider.reserve(
        quote_id=quote_a.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="a"
    )
    theirs = rival.provider.reserve(
        quote_id=quote_b.quote_id, customer_ref=rival.customer_id, idempotency_key="b"
    )

    assert [mine.outcome, theirs.outcome] == [Outcome.CONFIRMED, Outcome.UNAVAILABLE]
    live = [
        r for r in booking_ctx.reservations.all()
        if r.vehicle_id == vehicle.id and r.status == "confirmed"
    ]
    assert len(live) == 1


# --------------------------------------------------------------------------
# Retries, timeouts and the operations that must not happen twice
# --------------------------------------------------------------------------


def test_the_same_request_twice_makes_one_booking(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    first = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="dup"
    )
    second = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="dup"
    )

    assert first.reference == second.reference
    assert second.replayed is True
    assert len(booking_ctx.reservations.for_customer(booking_ctx.customer_id)) == 1


def test_a_timeout_after_success_is_reconciled_not_repeated(booking_ctx, provider):
    """The failure this whole ledger exists for: the booking was made and the
    answer never came back. Retrying would give the customer two cars."""
    quote, _ = _quote(booking_ctx)
    provider.fail_next = "timeout"
    hung = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="t1"
    )
    assert hung.outcome is Outcome.UNKNOWN
    assert hung.settled is False

    # Whatever the caller does next, it must not be a second attempt.
    again = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="t1"
    )
    assert again.outcome is Outcome.UNKNOWN
    assert len(booking_ctx.reservations.for_customer(booking_ctx.customer_id)) == 0

    assert provider.resolve("t1").outcome is Outcome.UNKNOWN


def test_a_provider_that_is_down_books_nothing_and_says_so(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    provider.fail_next = "down"
    answer = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="d1"
    )

    assert answer.outcome is Outcome.PROVIDER_UNAVAILABLE
    assert answer.settled is False
    assert booking_ctx.reservations.for_customer(booking_ctx.customer_id) == []


def test_resolving_an_operation_that_succeeded_finds_the_booking(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    made = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="r1"
    )

    resolved = provider.resolve("r1")
    assert resolved.outcome is Outcome.CONFIRMED
    assert resolved.reference == made.reference
    assert resolved.replayed is True


def test_resolving_a_key_nobody_used(booking_ctx, provider):
    assert provider.resolve("never-sent").outcome is Outcome.UNAVAILABLE


# --------------------------------------------------------------------------
# Whose booking it is
# --------------------------------------------------------------------------


def test_a_customer_cannot_read_another_customers_booking(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    mine = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="o1"
    )
    stranger = _other_customer(booking_ctx)

    answer = provider.get_reservation(mine.reference, customer_ref=stranger.customer_id)

    # The same answer as a reference that does not exist: saying "not yours"
    # confirms it is somebody's.
    assert answer.outcome is Outcome.UNAVAILABLE
    assert answer.reason == "not_found"


def test_a_customer_cannot_cancel_another_customers_booking(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    mine = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="o2"
    )
    stranger = _other_customer(booking_ctx)

    answer = provider.cancel_reservation(
        mine.reference, customer_ref=stranger.customer_id, idempotency_key="o3"
    )

    assert answer.outcome is Outcome.UNAVAILABLE
    assert booking_ctx.reservations.get(mine.reference).status == "confirmed"


def test_the_tools_refuse_a_stranger_too(booking_ctx):
    """The safeguard has to hold on the path the model actually calls."""
    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(booking_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": dt(10, 17).isoformat(),
        "return_at": dt(12, 17).isoformat(),
    })
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound", content="ok book it", now=booking_ctx.now(),
    )
    made = execute_tool(
        booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}
    )
    stranger = _other_customer(booking_ctx)

    result = execute_tool(stranger, "cancel_demo_reservation", {
        "reservation_id": made["reservation_id"]
    })

    assert result["error"] == "reservation_not_found"
    assert booking_ctx.reservations.get(made["reservation_id"]).status == "confirmed"


# --------------------------------------------------------------------------
# Changing a booking, and what it costs
# --------------------------------------------------------------------------


def test_a_change_is_reflected_in_the_dates_and_the_price(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    made = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="m0"
    )

    answer = provider.modify_reservation(
        made.reference,
        customer_ref=booking_ctx.customer_id,
        idempotency_key="m1",
        changes={"pickup_at": dt(10, 20), "price_accepted": True},
    )

    assert answer.outcome is Outcome.CONFIRMED
    assert answer.pickup_at == dt(10, 20)
    assert booking_ctx.reservations.get(made.reference).pickup_at == dt(10, 20)


def test_a_change_that_costs_more_needs_accepting_first(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    made = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="m2"
    )
    was = booking_ctx.reservations.get(made.reference).total_charge

    answer = provider.modify_reservation(
        made.reference,
        customer_ref=booking_ctx.customer_id,
        idempotency_key="m3",
        changes={"return_at": dt(14, 17)},          # two days longer
    )

    assert answer.outcome is Outcome.REVISED_QUOTE
    assert answer.revised_quote.previous_total == was
    assert answer.revised_quote.total_charge > was
    # And nothing moved until they said yes.
    assert booking_ctx.reservations.get(made.reference).total_charge == was


def test_accepting_the_new_price_applies_the_change(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    made = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="m4"
    )
    was = booking_ctx.reservations.get(made.reference).total_charge

    answer = provider.modify_reservation(
        made.reference,
        customer_ref=booking_ctx.customer_id,
        idempotency_key="m5",
        changes={"return_at": dt(14, 17), "price_accepted": True},
    )

    assert answer.outcome is Outcome.CONFIRMED
    assert answer.total_charge > was


def test_cancelling_releases_the_car(booking_ctx, provider):
    quote, vehicle = _quote(booking_ctx)
    made = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="c1"
    )
    assert provider.check_availability(vehicle.id, dt(10, 17), dt(12, 17)).available is False

    provider.cancel_reservation(
        made.reference, customer_ref=booking_ctx.customer_id, idempotency_key="c2"
    )

    assert provider.check_availability(vehicle.id, dt(10, 17), dt(12, 17)).available is True


# --------------------------------------------------------------------------
# Quotes that no longer stand
# --------------------------------------------------------------------------


def test_an_expired_quote_is_a_revision_not_a_booking(booking_ctx, provider):
    from datetime import timedelta

    quote, _ = _quote(booking_ctx)
    stored = booking_ctx.quotes.get(quote.quote_id)
    payload = dict(stored.payload)
    payload["expires_at"] = (booking_ctx.now() - timedelta(minutes=1)).isoformat()
    stored.payload = payload
    booking_ctx.session.flush()

    checked = provider.validate_quote(quote.quote_id)
    assert checked.outcome is Outcome.REVISED_QUOTE
    assert checked.reason == "expired"

    answer = provider.reserve(
        quote_id=quote.quote_id, customer_ref=booking_ctx.customer_id, idempotency_key="e1"
    )
    assert answer.outcome is Outcome.REVISED_QUOTE
    assert booking_ctx.reservations.for_customer(booking_ctx.customer_id) == []


def test_a_quote_that_still_stands_is_usable(booking_ctx, provider):
    quote, _ = _quote(booking_ctx)
    checked = provider.validate_quote(quote.quote_id)

    assert checked.outcome is Outcome.CONFIRMED
    assert checked.total_charge == quote.total_charge


# --------------------------------------------------------------------------
# Choosing a provider
# --------------------------------------------------------------------------


def test_the_simulator_is_what_runs_unless_something_says_otherwise(booking_ctx):
    assert isinstance(build_provider(booking_ctx), SimulatedProvider)
    assert build_provider(booking_ctx).is_demonstration is True


def test_selecting_delta_fails_loudly_and_never_serves_demo_data(booking_ctx, monkeypatch):
    """The one failure this package exists to make impossible: answering a
    customer with invented inventory because the real system was unreachable."""
    monkeypatch.setenv("BOOKING_PROVIDER", "delta")

    with pytest.raises(ProviderNotConfigured) as raised:
        build_provider(booking_ctx)

    assert "no adapter exists yet" in str(raised.value)


def test_an_unknown_provider_is_refused_rather_than_guessed(booking_ctx, monkeypatch):
    monkeypatch.setenv("BOOKING_PROVIDER", "whatever")
    with pytest.raises(ProviderNotConfigured):
        build_provider(booking_ctx)


def test_every_customer_facing_booking_says_it_is_a_demonstration(booking_ctx):
    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(booking_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": dt(10, 17).isoformat(),
        "return_at": dt(12, 17).isoformat(),
    })
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound", content="ok book it", now=booking_ctx.now(),
    )
    made = execute_tool(
        booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}
    )

    assert made["is_demo"] is True
    assert made["reservation_id"].startswith("DEMO-")
    assert "demonstration" in made["demo_notice"].lower()
