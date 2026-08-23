"""A link generated from the quoted price — their section 2, fourth bullet.

The rule the whole feature is built around: **no caller supplies an amount.**
The tool takes a reservation and a purpose, and the figure comes from what the
engine calculated and stored. There is no parameter a model could fill in, which
is the only version of this that is safe to have.

Worth recording that the operator may not want it. Their terms say payment
happens at collection, and in thirteen conversations their team never sent a
link — what they take up front is a holding payment against a specific car. So
the purpose is explicit, and one whose figure nobody has confirmed is refused.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rental_agent.payments import build_provider
from rental_agent.payments.base import PaymentError
from rental_agent.payments.stripe import minor_units
from rental_agent.tools.registry import execute_tool

from .conftest import dt


@pytest.fixture
def booked(booking_ctx):
    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(
        booking_ctx,
        "create_demo_quote",
        {"vehicle_id": vehicle.id, "pickup_at": dt(10, 17).isoformat(),
         "return_at": dt(12, 17).isoformat(), "delivery_location": "Dubai Marina"},
    )
    assert "error" not in quote, quote
    reservation = execute_tool(
        booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}
    )
    assert "error" not in reservation, reservation
    return quote, reservation


# --- the amount comes from the quote, and only from the quote ---------------


def test_the_link_charges_what_the_engine_calculated(booking_ctx, booked):
    quote, reservation = booked
    result = execute_tool(
        booking_ctx, "create_payment_link", {"reservation_id": reservation["reservation_id"]}
    )
    assert "error" not in result, result
    assert result["amount"] == quote["total_charge"]
    assert result["payment_url"]


def test_the_tool_has_no_way_to_be_given_an_amount():
    from rental_agent.agent.schemas import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "create_payment_link")
    properties = tool["input_schema"]["properties"]
    assert set(properties) == {"reservation_id", "purpose"}
    # The one parameter that must never exist.
    assert not any("amount" in name or "price" in name for name in properties)


def test_an_amount_passed_anyway_is_ignored(booking_ctx, booked):
    quote, reservation = booked
    result = execute_tool(
        booking_ctx,
        "create_payment_link",
        {"reservation_id": reservation["reservation_id"], "amount": "1.00"},
    )
    assert result["amount"] == quote["total_charge"]


# --- what it refuses --------------------------------------------------------


def test_a_held_booking_is_not_asked_for_money(booking_ctx, booked):
    _, reservation = booked
    record = booking_ctx.reservations.get(reservation["reservation_id"])
    record.status = "held"
    booking_ctx.session.flush()

    result = execute_tool(
        booking_ctx, "create_payment_link", {"reservation_id": reservation["reservation_id"]}
    )
    assert result["error"] == "reservation_not_confirmed"


def test_an_unconfirmed_deposit_cannot_be_charged(booking_ctx, booked):
    _, reservation = booked
    record = booking_ctx.reservations.get(reservation["reservation_id"])
    record.deposit = None
    booking_ctx.session.flush()

    result = execute_tool(
        booking_ctx,
        "create_payment_link",
        {"reservation_id": reservation["reservation_id"], "purpose": "deposit"},
    )
    assert result["error"] == "amount_unconfirmed"
    assert "colleague" in result["message"]


def test_an_unconfirmed_holding_amount_cannot_be_charged(booking_ctx, booked):
    _, reservation = booked
    booking_ctx.engine.rules.as_dict()["payment"]["links"]["holding_amount"] = None
    try:
        result = execute_tool(
            booking_ctx,
            "create_payment_link",
            {"reservation_id": reservation["reservation_id"], "purpose": "holding"},
        )
        assert result["error"] == "amount_unconfirmed"
    finally:
        booking_ctx.engine.rules.as_dict()["payment"]["links"]["holding_amount"] = 500


def test_a_purpose_the_operator_does_not_charge_for_is_refused(booking_ctx, booked):
    _, reservation = booked
    result = execute_tool(
        booking_ctx,
        "create_payment_link",
        {"reservation_id": reservation["reservation_id"], "purpose": "valet"},
    )
    assert result["error"] == "unknown_purpose"


# --- the link is recorded against the booking -------------------------------


def test_the_reservation_remembers_the_link(booking_ctx, booked):
    _, reservation = booked
    result = execute_tool(
        booking_ctx, "create_payment_link", {"reservation_id": reservation["reservation_id"]}
    )
    record = booking_ctx.reservations.get(reservation["reservation_id"])
    assert record.payment_status == "link_sent"
    assert record.payment_reference == result["payment_reference"]
    assert any(h["event"] == "payment_link_created" for h in record.history)


# --- the providers ----------------------------------------------------------


def test_nothing_takes_money_unless_configured(monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    assert build_provider().name == "simulated"


def test_the_suite_never_reaches_a_real_provider():
    # A developer with stripe configured for a demo would otherwise have every
    # test in this file open a Checkout session against their live account.
    import os

    assert os.environ["PAYMENT_PROVIDER"] == "simulated"


def test_the_demo_link_does_not_look_like_a_real_payment_page():
    link = build_provider("simulated").create_link(
        amount=Decimal("100"), currency="AED", reference="PAY-1", description="x"
    )
    # A demo link plausible enough to click is one somebody types a card into.
    assert ".invalid/" in link.url
    assert link.is_demo


def test_stripe_needs_a_key(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with pytest.raises(PaymentError, match="STRIPE_API_KEY"):
        build_provider("stripe")


@pytest.mark.parametrize("amount,currency,expected", [
    ("1887.90", "AED", 188790),
    ("3599", "AED", 359900),
    ("0.05", "AED", 5),
    ("1887", "JPY", 1887),          # zero-decimal currency
])
def test_amounts_convert_to_minor_units(amount, currency, expected):
    # The classic payment bug, and it fails in the expensive direction.
    assert minor_units(Decimal(amount), currency) == expected
