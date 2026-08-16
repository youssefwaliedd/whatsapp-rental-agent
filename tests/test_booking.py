"""The booking lifecycle through the tool layer.

Everything here goes through `execute_tool`, i.e. exactly the path the agent
will take. Testing the services directly would skip the idempotency ledger,
which is where the interesting failures live.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from rental_agent.context import ToolContext
from rental_agent.domain.enums import Stage
from rental_agent.tools.registry import execute_tool
from tests.conftest import FROZEN_NOW, REFERENCE_DATE, dt


def quote_for(ctx, vehicle_id="veh_13", pickup=None, ret=None, location="Dubai Marina", **extra):
    return execute_tool(
        ctx,
        "create_demo_quote",
        {
            "vehicle_id": vehicle_id,
            "pickup_at": (pickup or dt(4, 19)).isoformat(),
            "return_at": (ret or dt(7, 19)).isoformat(),
            "delivery_location": location,
            **extra,
        },
    )


def book(ctx, **kwargs):
    quote = quote_for(ctx, **kwargs)
    assert "error" not in quote, quote
    return execute_tool(ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})


# --------------------------------------------------------------------------
# Quote → reservation
# --------------------------------------------------------------------------


def test_a_quote_is_persisted_with_its_exact_figures(booking_ctx):
    result = quote_for(booking_ctx)
    stored = booking_ctx.quotes.get(result["quote_id"])
    assert stored.total_charge == Decimal("7560.00")
    assert stored.payload["vehicle_id"] == "veh_13"


def test_booking_produces_the_specification_reference(booking_ctx):
    reservation = book(booking_ctx)
    assert reservation["reservation_id"] == "DEMO-1042"
    assert reservation["is_demo"] is True
    assert reservation["status"] == "confirmed"
    assert "demonstration" in reservation["demo_notice"].lower()


def test_the_booked_total_comes_from_the_quote(booking_ctx):
    reservation = book(booking_ctx)
    assert reservation["total_charge"] == "7560.00"
    assert reservation["deposit"] == "5000.00"


def test_a_reservation_cannot_be_created_without_a_quote(booking_ctx):
    """No path exists to a booked total the engine did not calculate."""
    result = execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": "DQ-9999"})
    assert result["error"] == "quote_not_found"


def test_an_expired_quote_must_be_re_quoted(booking_ctx):
    quote = quote_for(booking_ctx)
    booking_ctx.set_now(FROZEN_NOW + timedelta(hours=25))
    result = execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    assert result["error"] == "quote_expired"


def test_booking_advances_the_conversation_stage(booking_ctx):
    book(booking_ctx)
    assert booking_ctx.load_state().stage is Stage.RESERVED
    assert booking_ctx.load_state().reservation_id == "DEMO-1042"


def test_an_underage_driver_cannot_book_a_supercar(booking_ctx):
    customer = booking_ctx.customers.get(booking_ctx.customer_id)
    customer.driver_age = 26
    booking_ctx.session.flush()

    quote = quote_for(booking_ctx, vehicle_id="veh_19", pickup=dt(4, 19), ret=dt(6, 19))
    result = execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    assert result["error"] == "driver_too_young"
    assert result["minimum_age"] == 30


# --------------------------------------------------------------------------
# A booking holds the car
# --------------------------------------------------------------------------


def test_a_booked_vehicle_is_unavailable_to_everybody_else(booking_ctx, session):
    book(booking_ctx)

    other = ToolContext(session=session, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)
    customer, _ = other.customers.get_or_create("+971500000099", FROZEN_NOW)
    conversation, _ = other.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
    other.customer_id, other.conversation_id = customer.customer_id, conversation.conversation_id

    result = quote_for(other)
    assert result["error"] == "vehicle_unavailable"


def test_a_booked_vehicle_disappears_from_search(booking_ctx):
    book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "search_available_vehicles",
        {
            "pickup_at": dt(4, 19).isoformat(),
            "return_at": dt(7, 19).isoformat(),
            "models": ["G63"],
        },
    )
    assert "veh_13" not in [v["vehicle_id"] for v in result["vehicles"]]
    assert "veh_14" in [v["vehicle_id"] for v in result["vehicles"]]


def test_non_overlapping_dates_are_still_bookable(booking_ctx):
    book(booking_ctx)
    later = quote_for(booking_ctx, pickup=dt(10, 19), ret=dt(12, 19))
    assert "error" not in later


def test_cancelling_releases_the_vehicle(booking_ctx):
    reservation = book(booking_ctx)
    execute_tool(
        booking_ctx, "cancel_demo_reservation", {"reservation_id": reservation["reservation_id"]}
    )
    assert "error" not in quote_for(booking_ctx)


# --------------------------------------------------------------------------
# Modification — the self-conflict trap
# --------------------------------------------------------------------------


def test_moving_the_delivery_by_an_hour_is_allowed(booking_ctx):
    """The specification's contextual follow-up: "can you make it 8 instead?".

    The availability check must exclude the booking being modified, or the car
    conflicts with itself and a one-hour change is refused.
    """
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "modify_demo_reservation",
        {"reservation_id": reservation["reservation_id"], "pickup_at": dt(4, 20).isoformat()},
    )
    assert "error" not in result, result
    assert result["pickup_at"].startswith("2026-09-04T20:00")
    assert result["changed"] == ["pickup_at"]


def test_a_modification_is_re_priced_by_the_engine(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "modify_demo_reservation",
        {"reservation_id": reservation["reservation_id"], "return_at": dt(9, 19).isoformat()},
    )
    assert result["total_charge"] == "12600.00"  # 5 days × 2400 + VAT
    assert result["price_difference"] == "5040.00"


def test_a_modification_records_its_history(booking_ctx):
    reservation = book(booking_ctx)
    execute_tool(
        booking_ctx,
        "modify_demo_reservation",
        {"reservation_id": reservation["reservation_id"], "delivery_location": "Sharjah"},
    )
    stored = booking_ctx.reservations.get(reservation["reservation_id"])
    events = [entry["event"] for entry in stored.history]
    assert events == ["created", "modified"]
    assert stored.history[-1]["before"]["delivery_location"] == "Dubai Marina"


def test_a_modification_into_someone_elses_booking_is_refused(booking_ctx, session):
    mine = book(booking_ctx)

    other = ToolContext(session=session, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)
    customer, _ = other.customers.get_or_create("+971500000098", FROZEN_NOW)
    conversation, _ = other.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
    other.customer_id, other.conversation_id = customer.customer_id, conversation.conversation_id
    theirs = book(other, pickup=dt(10, 19), ret=dt(14, 19))
    assert "error" not in theirs

    result = execute_tool(
        booking_ctx,
        "modify_demo_reservation",
        {"reservation_id": mine["reservation_id"], "return_at": dt(11, 19).isoformat()},
    )
    assert result["error"] == "vehicle_unavailable"


def test_a_cancelled_reservation_cannot_be_modified(booking_ctx):
    reservation = book(booking_ctx)
    execute_tool(
        booking_ctx, "cancel_demo_reservation", {"reservation_id": reservation["reservation_id"]}
    )
    result = execute_tool(
        booking_ctx,
        "modify_demo_reservation",
        {"reservation_id": reservation["reservation_id"], "pickup_at": dt(4, 20).isoformat()},
    )
    assert result["error"] == "reservation_not_live"


# --------------------------------------------------------------------------
# Extension
# --------------------------------------------------------------------------


def test_an_extension_charges_only_the_difference(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "extend_demo_rental",
        {"reservation_id": reservation["reservation_id"], "new_return_at": dt(9, 19).isoformat()},
    )
    assert "error" not in result, result
    assert result["additional_charge"] == "5040.00"
    assert result["previous_return_at"].startswith("2026-09-07T19:00")


def test_a_long_extension_reaches_the_weekly_rate(booking_ctx):
    """Re-rating the whole rental, not just the extra days, means a customer
    who extends into a second week is not penalised for it."""
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "extend_demo_rental",
        {"reservation_id": reservation["reservation_id"], "new_return_at": dt(11, 19).isoformat()},
    )
    assert result["rate_basis"] == "weekly"
    assert result["total_charge"] == "15750.00"


def test_an_extension_backwards_is_rejected(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "extend_demo_rental",
        {"reservation_id": reservation["reservation_id"], "new_return_at": dt(6, 19).isoformat()},
    )
    assert result["error"] == "invalid_request"


def test_an_extension_blocked_by_another_booking_is_refused(booking_ctx, session):
    mine = book(booking_ctx)

    other = ToolContext(session=session, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)
    customer, _ = other.customers.get_or_create("+971500000097", FROZEN_NOW)
    conversation, _ = other.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
    other.customer_id, other.conversation_id = customer.customer_id, conversation.conversation_id
    book(other, pickup=dt(8, 19), ret=dt(12, 19))

    result = execute_tool(
        booking_ctx,
        "extend_demo_rental",
        {"reservation_id": mine["reservation_id"], "new_return_at": dt(10, 19).isoformat()},
    )
    assert result["error"] == "vehicle_unavailable"
    assert "escalate" in result["hint"]


def test_a_late_extension_request_hits_the_notice_period(booking_ctx):
    reservation = book(booking_ctx)
    # Four hours before return; the configured notice period is six.
    booking_ctx.set_now(dt(7, 15))
    result = execute_tool(
        booking_ctx,
        "extend_demo_rental",
        {"reservation_id": reservation["reservation_id"], "new_return_at": dt(9, 19).isoformat()},
    )
    assert result["error"] == "policy_notice_period"
    assert result["requires_human_approval"] is True


# --------------------------------------------------------------------------
# Cancellation bands
# --------------------------------------------------------------------------


def test_cancelling_early_is_free(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "cancel_demo_reservation",
        {"reservation_id": reservation["reservation_id"], "reason": "changed plans"},
    )
    assert result["cancellation_band"] == "free"
    assert result["cancellation_fee"] == "0.00"
    assert result["status"] == "cancelled"


def test_cancelling_inside_the_window_costs_the_configured_percentage(booking_ctx):
    reservation = book(booking_ctx)
    booking_ctx.set_now(dt(4, 0))  # 19 hours before pickup, inside the 48h window
    result = execute_tool(
        booking_ctx, "cancel_demo_reservation", {"reservation_id": reservation["reservation_id"]}
    )
    assert result["cancellation_band"] == "late"
    assert result["cancellation_fee_percent"] == "25"
    assert result["cancellation_fee"] == "1890.00"


def test_not_showing_up_costs_the_full_amount(booking_ctx):
    reservation = book(booking_ctx)
    booking_ctx.set_now(dt(5, 12))  # after pickup
    result = execute_tool(
        booking_ctx, "cancel_demo_reservation", {"reservation_id": reservation["reservation_id"]}
    )
    assert result["cancellation_band"] == "no_show"
    assert result["cancellation_fee"] == "7560.00"


# --------------------------------------------------------------------------
# Documents, payment, delivery — all simulated
# --------------------------------------------------------------------------


def test_documents_report_what_is_still_missing(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "record_demo_documents",
        {"reservation_id": reservation["reservation_id"], "documents": ["passport"]},
    )
    assert result["complete"] is False
    assert "international_driving_permit" in result["still_missing"]
    assert "credit_card_in_driver_name" in result["required"]  # luxury_suv extra


def test_complete_documents_move_the_stage_on(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "record_demo_documents",
        {
            "reservation_id": reservation["reservation_id"],
            "documents": [
                "passport",
                "visit_visa_or_entry_stamp",
                "home_country_licence",
                "international_driving_permit",
                "credit_card_in_driver_name",
            ],
        },
    )
    assert result["complete"] is True
    assert booking_ctx.load_state().stage is Stage.PAYMENT_PENDING


def test_payment_is_explicitly_simulated(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx, "simulate_payment", {"reservation_id": reservation["reservation_id"]}
    )
    assert result["simulated"] is True
    assert result["payment_reference"].startswith("DEMOPAY-")
    assert result["payment_status"] == "authorised"
    assert "no card was charged" in result["demo_notice"]


def test_an_unsupported_payment_method_is_refused(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "simulate_payment",
        {"reservation_id": reservation["reservation_id"], "method": "crypto"},
    )
    assert result["error"] == "payment_method_not_accepted"


def test_delivery_scheduling_flags_out_of_hours(booking_ctx):
    reservation = book(booking_ctx)
    result = execute_tool(
        booking_ctx,
        "schedule_demo_delivery",
        {"reservation_id": reservation["reservation_id"], "delivery_at": dt(4, 23).isoformat()},
    )
    assert result["within_operating_hours"] is False
    assert booking_ctx.load_state().stage is Stage.DELIVERY_SCHEDULED


# --------------------------------------------------------------------------
# Customer memory
# --------------------------------------------------------------------------


def test_the_active_reservation_is_what_a_follow_up_refers_to(booking_ctx):
    book(booking_ctx)
    result = execute_tool(booking_ctx, "get_active_reservation", {})
    assert result["has_active_reservation"] is True
    assert result["reservation"]["reservation_id"] == "DEMO-1042"


def test_a_cancelled_booking_is_no_longer_active(booking_ctx):
    reservation = book(booking_ctx)
    execute_tool(
        booking_ctx, "cancel_demo_reservation", {"reservation_id": reservation["reservation_id"]}
    )
    assert execute_tool(booking_ctx, "get_active_reservation", {})["has_active_reservation"] is False


def test_a_returning_customer_is_recognised(booking_ctx):
    book(booking_ctx)
    result = execute_tool(booking_ctx, "get_customer", {})
    assert result["is_returning_customer"] is True
    assert result["previous_demo_bookings"][0]["reservation_id"] == "DEMO-1042"


def test_conversational_preferences_are_remembered(booking_ctx):
    result = execute_tool(
        booking_ctx, "save_customer_preference", {"key": "preferred_color", "value": "black"}
    )
    assert result["saved"] is True
    assert execute_tool(booking_ctx, "get_customer", {})["preferences"]["preferred_color"] == "black"


@pytest.mark.parametrize(
    "key", ["discount", "always_gets_20_percent_discount", "daily_price", "deposit", "policy"]
)
def test_a_customer_claim_can_never_become_a_business_rule(booking_ctx, key):
    """The specification's hard boundary: "you always give me 20% off" must not
    survive into anything that affects a price."""
    result = execute_tool(booking_ctx, "save_customer_preference", {"key": key, "value": 20})
    assert result["error"] == "preference_not_allowed"
    assert execute_tool(booking_ctx, "get_customer", {})["preferences"] == {}


def test_a_saved_preference_cannot_change_a_quote(booking_ctx):
    """Belt and braces: even a permitted preference has no pricing effect."""
    before = quote_for(booking_ctx)["total_charge"]
    execute_tool(booking_ctx, "save_customer_preference", {"key": "preferred_color", "value": "black"})
    after = quote_for(booking_ctx)["total_charge"]
    assert before == after == "7560.00"
