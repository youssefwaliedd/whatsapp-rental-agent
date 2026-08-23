"""A booking is a hold until somebody who can see the fleet says otherwise.

Delta publishes availability nowhere. Their team checks it by hand — "please
allow me a moment to check" — in something the engine cannot read. So a booking
the agent makes is a claim about a car nobody has looked at: it may have gone out
by phone an hour ago.

The customer arriving for a car that is not there is the worst outcome a rental
business has, and it is the one thing the architecture could not prevent, because
the missing piece is not code. So the agent holds, a person confirms, and every
promise made in between is conditional.

Questionnaire 25 asks the operator to choose this explicitly. It is the safe
default either way, and the fixture operator — whose availability really is in
the fleet file — has it off, so both paths stay covered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from rental_agent.domain.enums import ReservationStatus
from rental_agent.evaluation.checks import check_confirmed_a_hold
from rental_agent.services import booking
from rental_agent.tools.registry import execute_tool

from .conftest import dt


@dataclass
class Msg:
    direction: str
    content: str
    id: int = 1


@dataclass
class Call:
    tool_name: str
    result: dict
    arguments: Any = None


HELD_CALL = [Call("create_demo_reservation", {"status": "held", "awaiting_confirmation": True})]


def quote_anything(ctx, day: int):
    """A quote for whichever car is actually free — the seeded calendar blocks
    some of them, and which ones is not this test's business."""
    for vehicle in ctx.engine.list_fleet():
        result = execute_tool(
            ctx,
            "create_demo_quote",
            {"vehicle_id": vehicle.id,
             "pickup_at": dt(day, 17).isoformat(),
             "return_at": dt(day + 2, 17).isoformat()},
        )
        if "error" not in result:
            return result, vehicle
    raise AssertionError("no vehicle was free")


@pytest.fixture
def reserved(booking_ctx):
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
    return execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})


# --- the operator whose availability is known -------------------------------


def test_an_operator_with_real_availability_still_just_books(booking_ctx, reserved):
    assert booking.holds_require_confirmation(booking_ctx) is False
    assert reserved["status"] == "confirmed"
    assert "awaiting_confirmation" not in reserved


# --- the operator whose availability nobody can see -------------------------


@pytest.fixture
def holding_ctx(booking_ctx):
    """The same context, with the operator's availability unknown.

    Restores afterwards: the rules are cached for the whole session, so a test
    that flips this and walks away turns every later booking into a hold.
    """
    rules = booking_ctx.engine.rules.as_dict()
    before = rules.get("booking")
    rules["booking"] = {"holds_require_confirmation": True}
    yield booking_ctx
    if before is None:
        rules.pop("booking", None)
    else:
        rules["booking"] = before


def test_a_booking_becomes_a_hold(holding_ctx, reserved):
    held, _ = quote_anything(holding_ctx, 14)
    result = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": held["quote_id"]})
    assert result["status"] == ReservationStatus.HELD.value
    assert result["awaiting_confirmation"] is True
    assert "Do NOT say it is booked" in result["guidance"]


def test_a_hold_asks_a_person_and_names_the_car(holding_ctx):
    quote, vehicle = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    case = holding_ctx.escalations.latest_for_conversation(holding_ctx.conversation_id)
    assert case.reason == "booking_hold"
    # The owner should read a car, not an id they would have to look up.
    assert vehicle.display_name in case.detail


def test_a_hold_is_not_yet_a_sale(holding_ctx):
    quote, _ = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    conversation = holding_ctx.conversations.get(holding_ctx.conversation_id)
    assert conversation.sales_outcome != "booked"


# --- what the owner's answer does -------------------------------------------


def test_confirming_turns_the_hold_into_a_booking(holding_ctx):
    quote, _ = quote_anything(holding_ctx, 14)
    held = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})

    assert booking.apply_hold_decision(holding_ctx, held["reservation_id"], "approved") == "confirmed"
    conversation = holding_ctx.conversations.get(holding_ctx.conversation_id)
    assert conversation.sales_outcome == "booked"


def test_releasing_cancels_it_so_nothing_later_calls_it_theirs(holding_ctx):
    quote, _ = quote_anything(holding_ctx, 20)
    held = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    assert booking.apply_hold_decision(holding_ctx, held["reservation_id"], "declined") == "cancelled"


def test_a_decision_on_something_that_is_not_held_does_nothing(booking_ctx, reserved):
    # A confirmed booking is not a hold, and an owner tapping confirm on one
    # must not rewrite it.
    assert reserved["status"] == "confirmed"
    assert booking.apply_hold_decision(booking_ctx, reserved["reservation_id"], "approved") is None


def test_the_owner_is_asked_whether_the_car_is_free(holding_ctx):
    from rental_agent.services import handover

    labels = [o["label"] for o in handover.decision_options(holding_ctx, "booking_hold")]
    # "Approve" tells the owner nothing about what they are approving.
    assert "Confirm the car" in labels and "Not available" in labels


def test_the_relay_says_what_happened_to_the_car():
    from types import SimpleNamespace

    from rental_agent.services import handover

    confirmed = SimpleNamespace(reason="booking_hold", decision="approved",
                                detail="DEMO-1042: Audi RS3 HB", question=None, decision_note=None)
    assert "confirmed" in handover.relay_directive(confirmed).lower()

    released = SimpleNamespace(reason="booking_hold", decision="declined",
                               detail="DEMO-1042: Audi RS3 HB", question=None, decision_note=None)
    directive = handover.relay_directive(released)
    assert "NOT available" in directive
    assert "offer it" in directive  # find them something else; they still want a car


# --- and the agent is measured on what it told the customer -----------------


@pytest.mark.parametrize("said", [
    "Your Ferrari Roma is booked! Reference DEMO-1042.",
    "I've confirmed the booking for you.",
    "All set — you're good to go!",
    "Your booking is confirmed, see you Friday.",
])
def test_calling_a_hold_a_booking_is_caught(said):
    findings = check_confirmed_a_hold([Msg("outbound", said)], HELD_CALL)
    assert [f.type for f in findings] == ["confirmed_a_hold"]
    assert findings[0].severity == "high"


@pytest.mark.parametrize("said", [
    "I've held the Ferrari Roma for you — a colleague is confirming it now.",
    "The car is held under DEMO-1042 while the team checks it.",
    "Let me hold that for you and confirm shortly.",
])
def test_holding_language_is_fine(said):
    assert check_confirmed_a_hold([Msg("outbound", said)], HELD_CALL) == []


def test_a_real_booking_may_be_called_a_booking():
    booked = [Call("create_demo_reservation", {"status": "confirmed"})]
    assert check_confirmed_a_hold([Msg("outbound", "You're all booked!")], booked) == []


# --- the owner's answer must reach the case they were asked about -----------
#
# From a live run: the owner tapped "Confirm the car" on a held BMW and the
# system applied it to a police-report case from the night before. The customer
# was told about accident procedure and never got their booking. Both cases had
# the same created_at — the simulator's clock is frozen — so ordering by time
# alone resolved arbitrarily.


def test_the_newest_case_wins_when_two_were_raised_in_the_same_second(booking_ctx):
    from rental_agent.services import handover

    now = booking_ctx.now()
    first = booking_ctx.escalations.create(
        conversation_id=booking_ctx.conversation_id, customer_id=booking_ctx.customer_id,
        reason="police_involvement", detail="older", now=now,
    )
    handover.open_case(booking_ctx, first, "older question")
    second = booking_ctx.escalations.create(
        conversation_id=booking_ctx.conversation_id, customer_id=booking_ctx.customer_id,
        reason="booking_hold", detail="DEMO-1043: BMW M4 Competition", now=now,
    )
    handover.open_case(booking_ctx, second, "is this car free?")

    assert first.created_at == second.created_at  # the tie that caused it
    assert handover.open_cases(booking_ctx)[0].case_code == second.case_code


def test_the_owner_is_asked_whether_the_car_is_free_not_what_to_say(holding_ctx):
    from rental_agent.services import handover

    asked = handover.decision_prompt(holding_ctx, "booking_hold")
    assert "free" in asked.lower()
    # The generic prompt is right for a fee dispute and useless for a held car.
    assert handover.decision_prompt(holding_ctx, "fee_dispute") == "What should I tell them?"
