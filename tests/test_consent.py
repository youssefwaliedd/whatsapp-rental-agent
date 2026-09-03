"""Not booking over a customer who said "not yet".

Observed live: "dubai marina. first show me the pictures o that car" created
DEMO-1047. The address was the answer to a question the agent had asked, and
*first* was the customer saying wait — and a booking was taken anyway. It is the
same shape as the older one where a mistyped command was read as consent.

A booking is the hardest action here to take back: it puts a reference in front
of the customer, raises a case with the owner, and where a request keeps a car it
takes that car off the market. So consent is decided by a gate on the customer's
own words rather than by the model's reading of them, the same way an incident is.
"""

from __future__ import annotations

import pytest

from rental_agent.domain.consent import defers_booking
from rental_agent.tools.registry import execute_tool

from .conftest import dt


@pytest.mark.parametrize("said", [
    "dubai marina. first show me the pictures o that car",
    "wait, what is the deposit?",
    "hold on — can I see the interior?",
    "just show me what you have",
    "let me think about it",
    "can I see the range rover sport v8?",
    "not yet, I need to check with my wife",
    "don't book anything yet",
])
def test_a_customer_putting_something_first_is_not_consent(said):
    assert defers_booking(said) is True


@pytest.mark.parametrize("said", [
    "ok book it",
    "yes please go ahead",
    "friday 5pm to sunday 5pm",
    "dubai marina",
    "sounds good, let's do it",
])
def test_a_customer_saying_yes_still_books(said):
    assert defers_booking(said) is False


@pytest.mark.parametrize("said", [
    "show me the pictures first, then book it",
    "book it now",
])
def test_an_explicit_instruction_outranks_the_word_first(said):
    """"First show me the pictures, then book it" means book it. A gate that
    could not hear that would block a real booking for saying "first"."""
    assert defers_booking(said) is False


def test_the_booking_is_refused_and_the_model_is_told_why(booking_ctx):
    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(booking_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": dt(10, 17).isoformat(),
        "return_at": dt(12, 17).isoformat(),
    })
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound",
        content="dubai marina. first show me the pictures of that car",
        now=booking_ctx.now(),
    )
    booking_ctx.session.flush()

    result = execute_tool(
        booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}
    )

    assert result["error"] == "not_asked_for_yet"
    assert "Do not create the booking" in result["message"]
    assert booking_ctx.reservations.for_customer(booking_ctx.customer_id) == []


def test_a_clear_yes_still_reaches_a_booking(booking_ctx):
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
    booking_ctx.session.flush()

    result = execute_tool(
        booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}
    )

    assert "error" not in result
    assert result["reservation_id"].startswith("DEMO-")
