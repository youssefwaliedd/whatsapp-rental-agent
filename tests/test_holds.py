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

from rental_agent.agent import holds
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
    rules["booking"] = {
        "holds_require_confirmation": True,
        # The real config carries this too, and dropping it here made every
        # hold immortal in the tests that most needed it not to be.
        "hold_expires_after_minutes": 120,
    }
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

    assert booking.apply_hold_decision(
        holding_ctx, held["reservation_id"], "approved"
    )["outcome"] == "confirmed"
    conversation = holding_ctx.conversations.get(holding_ctx.conversation_id)
    assert conversation.sales_outcome == "booked"


def test_releasing_cancels_it_so_nothing_later_calls_it_theirs(holding_ctx):
    quote, _ = quote_anything(holding_ctx, 20)
    held = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    assert booking.apply_hold_decision(
        holding_ctx, held["reservation_id"], "declined"
    )["outcome"] == "released"


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


# --- the state block must not contradict the message being sent -------------
#
# From a live run, one message after the owner confirmed the car: "your booking
# is now all set... a colleague of mine is now taking over to finalize the final
# details." Nobody was taking over. A colleague had answered one question and the
# agent was delivering it.
#
# `mark_relayed` runs after the send, deliberately — an answer nobody delivered
# has resolved nothing — so a relay happens while the conversation is still
# flagged escalated, and the state block was telling the model a colleague was
# taking over at the exact moment it was delivering that colleague's answer.


def _state(**kwargs):
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="c", customer_id="u")
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def test_a_waiting_conversation_still_says_a_colleague_is_taking_over():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from rental_agent.agent.prompt import render_state

    now = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Dubai"))
    text = render_state(_state(escalated=True, escalation_reason="accident"), now=now)
    assert "ESCALATED" in text
    assert "Do not sell, quote or book" in text


def test_delivering_the_answer_hands_the_conversation_back():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from rental_agent.agent.prompt import render_state

    now = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Dubai"))
    text = render_state(
        _state(escalated=True, escalation_reason="booking_hold"),
        now=now,
        directive="A colleague has confirmed the car is free.",
    )
    assert "back with you" in text
    # The instruction it would otherwise have obeyed instead of the directive.
    assert "Do not sell, quote or book" not in text


def test_a_confirmed_hold_tells_the_agent_to_carry_on():
    from types import SimpleNamespace

    from rental_agent.services import handover

    case = SimpleNamespace(reason="booking_hold", decision="approved",
                           detail="DEMO-1044: BMW M4 Competition",
                           question=None, decision_note=None)
    directive = handover.relay_directive(case)
    assert "Nobody is taking over" in directive


# --- and the message is stopped before it goes out --------------------------
#
# The evaluator above reads transcripts, which means it reports this after the
# customer has been told the car is theirs. Eleven times in real conversations,
# every one of them after holds existed: the mechanic was working and the
# wording was undoing it.
#
# So the same rule runs on the way out, and it is stated as proof rather than
# suspicion — confirmation language needs a confirmed booking behind it. A hold
# is not one. Neither is nothing at all, which is the worse case and the one a
# hold-shaped guard would have waved straight through.


def _agent(*replies):
    from tests.fake_anthropic import FakeClient, says

    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings

    return Agent(FakeClient(script=[says(r) for r in replies]),
                 AgentSettings(extraction_enabled=False))


@pytest.fixture
def on_hold(holding_ctx):
    """A live hold, taken the way the model takes one."""
    quote, _ = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    return holding_ctx


def _age(ctx, reservation_id, minutes):
    """Push a hold back in time, so the clock has run past it."""
    from datetime import timedelta

    reservation = ctx.reservations.get(reservation_id)
    reservation.created_at = ctx.now() - timedelta(minutes=minutes)
    ctx.session.flush()
    return reservation


# --- what the words claim ---------------------------------------------------


@pytest.mark.parametrize("said", [
    "You're all set — the car is reserved for Friday.",
    "I have secured the car for you.",
    "Your booking is confirmed.",
    "The car is yours from Friday.",
    "We've got you down for the Roma.",
])
def test_the_guard_recognises_the_promise(said):
    assert holds.inspect(said).explicit is True


@pytest.mark.parametrize("said", [
    "تم تأكيد الحجز.",
    "تم الحجز، نراكم يوم الجمعة.",
    "السيارة محجوزة لك.",
    "السيارة لك من يوم الجمعة.",
])
def test_the_guard_reads_arabic_too(said):
    # Delta's own thirteen conversations are in English. Their market is not,
    # and a guard that only reads English fails silently rather than loudly.
    assert holds.inspect(said).explicit is True


@pytest.mark.parametrize("said", [
    "I've held the Ferrari for you — a colleague is confirming it right now.",
    "Once it's confirmed I'll send you the payment link.",
    "It isn't confirmed yet; I'll come straight back.",
    "The choice is yours — Roma or the RS3?",
    "الحجز بانتظار التأكيد.",
    "لم يتم تأكيد الحجز بعد.",
    "Your booking request is awaiting confirmation. The vehicle is not yet confirmed.",
])
def test_the_guard_leaves_honest_wording_alone(said):
    assert bool(holds.inspect(said)) is False


def test_a_conditional_sentence_does_not_license_the_promise_beside_it():
    # Both in one reply: the honest clause is cut out, not read as a pass.
    claim = holds.inspect(
        "Once the paperwork is confirmed we'll deliver. Your car is booked!"
    )
    assert claim.explicit is True


def test_all_set_about_the_booking_is_a_claim():
    assert holds.inspect("All set — your car is ready for Friday.").generic is True
    assert holds.inspect("You're all set!").generic is True


def test_all_set_about_the_delivery_address_is_not():
    claim = holds.inspect("All set — I've noted Dubai Marina for the delivery.")
    assert bool(claim) is False


def test_the_reference_the_reply_names_is_picked_up():
    assert holds.inspect("Your booking DEMO-1042 is confirmed.").references == {"DEMO-1042"}


# --- what has to back it ----------------------------------------------------


def test_calling_an_unconfirmed_booking_done_is_rewritten(on_hold):
    agent = _agent("You're all set — the car is reserved for Friday.",
                   "Your booking request is awaiting confirmation.")
    turn = agent.respond(on_hold, "great, book it")

    assert "reserved" not in turn.reply
    # Recorded even though the retry succeeded, because it happened.
    assert turn.confirmed_a_hold is True


def test_a_model_that_insists_does_not_get_to_promise_a_booking(on_hold):
    agent = _agent("You're all set — the car is reserved for Friday.",
                   "It's reserved, yes.")
    turn = agent.respond(on_hold, "great, book it")

    assert turn.reply == holds.SAFE_REPLY
    assert "not yet confirmed" in turn.reply
    # Nothing takes the car off the market, so nothing may say it is being kept.
    assert "held" not in turn.reply.lower()
    assert turn.confirmed_a_hold is True


def test_a_booking_announced_with_no_booking_at_all_is_refused(booking_ctx):
    # The case a hold-shaped guard misses: nothing was ever taken, and the
    # customer is told it is done.
    agent = _agent("You're all set — your booking is confirmed for Friday.",
                   "Your booking is confirmed.")
    turn = agent.respond(booking_ctx, "book it then")

    assert turn.reply == holds.NO_BOOKING_REPLY
    assert turn.confirmed_a_hold is True


def test_a_pleasantry_with_no_booking_anywhere_is_left_alone(booking_ctx):
    agent = _agent("All set — what dates were you thinking?")
    turn = agent.respond(booking_ctx, "hi")

    assert turn.reply == "All set — what dates were you thinking?"
    assert turn.confirmed_a_hold is False


def test_a_confirmed_booking_may_still_be_called_one(booking_ctx, reserved):
    # This operator's availability is in the fleet file, so nothing is held and
    # the promise is true.
    agent = _agent("You're all set — your booking is confirmed.")
    turn = agent.respond(booking_ctx, "great, book it")

    assert turn.reply == "You're all set — your booking is confirmed."
    assert turn.confirmed_a_hold is False


def test_conditional_wording_goes_out_untouched(on_hold):
    agent = _agent("I have the request in — once it's confirmed I'll send the link.")
    turn = agent.respond(on_hold, "great, book it")

    assert turn.reply.startswith("I have the request in")
    assert turn.confirmed_a_hold is False


def test_one_car_confirmed_and_another_being_checked(reserved, holding_ctx):
    """The truth about the confirmed one must still be sayable.

    `reserved` was taken while this operator still booked outright, so it is
    genuinely confirmed. The hold is a second car on the same customer.
    """
    quote, _ = quote_anything(holding_ctx, 20)
    held = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    assert held["status"] == ReservationStatus.HELD.value

    agent = _agent(f"Your booking {reserved['reservation_id']} is confirmed.")
    turn = agent.respond(holding_ctx, "is my first booking sorted?")

    assert turn.reply == f"Your booking {reserved['reservation_id']} is confirmed."
    assert turn.confirmed_a_hold is False


def test_but_not_about_the_one_still_being_checked(reserved, holding_ctx):
    quote, _ = quote_anything(holding_ctx, 20)
    held = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})

    agent = _agent(f"Your booking {held['reservation_id']} is confirmed.",
                   "That one is still awaiting confirmation.")
    turn = agent.respond(holding_ctx, "and the second car?")

    assert turn.reply == "That one is still awaiting confirmation."
    assert turn.confirmed_a_hold is True


def test_all_set_naming_neither_booking_is_refused(reserved, holding_ctx):
    """True of one booking, false of the other, and the customer cannot tell."""
    quote, _ = quote_anything(holding_ctx, 20)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})

    agent = _agent("You're all set!", f"Booking {reserved['reservation_id']} is confirmed.")
    turn = agent.respond(holding_ctx, "all sorted?")

    assert turn.reply == f"Booking {reserved['reservation_id']} is confirmed."
    assert turn.confirmed_a_hold is True


def test_a_reference_that_resolves_to_nothing_falls_back_to_what_they_have(on_hold):
    # DQ-510 is a quote code, not a booking. It must not be read as proof.
    agent = _agent("Your booking DQ-510 is confirmed.",
                   "It is awaiting confirmation.")
    turn = agent.respond(on_hold, "sorted?")

    assert turn.confirmed_a_hold is True


# --- and a hold that ran out of time is not a booking either ----------------


def test_an_expired_hold_stops_being_the_customers_booking(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    assert reservation is not None
    _age(on_hold, reservation.reservation_id, 121)

    assert booking_service.live_hold(on_hold) is None
    assert booking_service.confirmation_backing(on_hold) == booking_service.NOTHING


def test_an_expired_hold_is_released_by_the_sweep(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    _age(on_hold, reservation.reservation_id, 121)

    assert booking_service.expire_holds(on_hold) == [reservation.reservation_id]
    assert on_hold.reservations.get(reservation.reservation_id).status == "cancelled"
    # Twice must not double-release, and must not touch anything else.
    assert booking_service.expire_holds(on_hold) == []


def test_a_hold_inside_its_window_is_left_alone(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    _age(on_hold, reservation.reservation_id, 119)

    assert booking_service.expire_holds(on_hold) == []
    assert booking_service.live_hold(on_hold) is not None


def test_the_owner_answering_late_is_still_honoured(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    _age(on_hold, reservation.reservation_id, 121)
    booking_service.expire_holds(on_hold)

    # Expiry stops the agent treating it as current. It does not overrule the
    # one person who can actually see the car.
    assert booking_service.apply_hold_decision(
        on_hold, reservation.reservation_id, "approved"
    )["outcome"] == "confirmed"


def test_a_hold_the_owner_released_is_not_revived(holding_ctx):
    from rental_agent.services import booking as booking_service

    quote, _ = quote_anything(holding_ctx, 14)
    held = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    booking_service.apply_hold_decision(holding_ctx, held["reservation_id"], "declined")

    assert booking_service.apply_hold_decision(
        holding_ctx, held["reservation_id"], "approved"
    ) is None


def test_an_expired_hold_no_longer_blocks_confirmation_language(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    _age(on_hold, reservation.reservation_id, 121)

    # Nothing on file now, so the explicit promise is still refused — but as
    # the "no booking" case, not as the "awaiting" one.
    agent = _agent("Your booking is confirmed.", "Your booking is confirmed.")
    turn = agent.respond(on_hold, "sorted?")
    assert turn.reply == holds.NO_BOOKING_REPLY


# --- the agent can see a hold, and cannot change one ------------------------


def test_the_agent_can_see_the_request_it_is_waiting_on(on_hold):
    result = execute_tool(on_hold, "get_active_reservation", {})

    assert result["has_active_reservation"] is False
    assert result["awaiting_confirmation"] is True
    assert result["held_reservation"]["status"] == ReservationStatus.HELD.value
    assert "never as booked" in result["guidance"]


def test_an_expired_request_is_not_shown_as_current(on_hold):
    from rental_agent.services import booking as booking_service

    _age(on_hold, booking_service.live_hold(on_hold).reservation_id, 121)
    result = execute_tool(on_hold, "get_active_reservation", {})

    assert result["has_active_reservation"] is False
    assert "held_reservation" not in result


def test_make_it_eight_instead_is_recorded_not_applied(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    was = reservation.pickup_at
    result = execute_tool(on_hold, "modify_demo_reservation", {
        "reservation_id": reservation.reservation_id,
        "pickup_at": dt(10, 20).isoformat(),
    })

    assert result["applied"] is False
    assert result["awaiting_confirmation"] is True
    assert "Do not say the new time is booked" in result["guidance"]
    # The booking itself is untouched: a held car has no modification rights.
    assert on_hold.reservations.get(reservation.reservation_id).pickup_at == was


def test_the_person_being_asked_hears_about_the_change(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    execute_tool(on_hold, "modify_demo_reservation", {
        "reservation_id": reservation.reservation_id,
        "pickup_at": dt(10, 20).isoformat(),
    })

    case = on_hold.escalations.latest_for_conversation(on_hold.conversation_id)
    # Otherwise the owner confirms the window they were sent, which is no
    # longer the one the customer wants.
    assert "has since asked for" in (case.question or "")


def test_extending_a_request_is_recorded_the_same_way(on_hold):
    from rental_agent.services import booking as booking_service

    reservation = booking_service.live_hold(on_hold)
    result = execute_tool(on_hold, "extend_demo_rental", {
        "reservation_id": reservation.reservation_id,
        "new_return_at": dt(13, 17).isoformat(),
    })

    assert result["applied"] is False
    assert result["requested_change"]["return_at"].startswith("2026-09-13")


def test_a_confirmed_booking_still_modifies_normally(booking_ctx, reserved):
    result = execute_tool(booking_ctx, "modify_demo_reservation", {
        "reservation_id": reserved["reservation_id"],
        "delivery_location": "Dubai Marina",
    })

    assert "applied" not in result
    assert result.get("error") is None


# --- confirmation is a decision about a car, not a status change ------------
#
# An approval can arrive two hours late, after the customer has moved the dates,
# and after somebody else's booking has been confirmed on the same car. Nothing
# holds a car off the market while a request waits, so the only thing standing
# between two approvals and a double booking is the check made at the moment of
# confirming.


def _confirm(ctx, reservation_id):
    from rental_agent.services import booking as booking_service

    return booking_service.apply_hold_decision(ctx, reservation_id, "approved")


def _another_customer(ctx, phone="+971500000002"):
    """A second customer on the same database, wanting the same car."""
    from rental_agent.context import ToolContext

    other = ToolContext(
        session=ctx.session, now_fn=ctx.now_fn, reference_date=ctx.reference_date
    )
    customer, _ = other.customers.get_or_create(phone, ctx.now())
    conversation, _ = other.conversations.get_or_create(customer.customer_id, ctx.now())
    other.customer_id = customer.customer_id
    other.conversation_id = conversation.conversation_id
    return other


def test_confirming_rechecks_the_car_and_refuses_a_double_booking(holding_ctx):
    """Two customers, one car, one window. Only one may become a booking."""
    quote, vehicle = quote_anything(holding_ctx, 14)
    mine = execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})

    rival_ctx = _another_customer(holding_ctx)
    rival_quote = execute_tool(rival_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": dt(14, 17).isoformat(),
        "return_at": dt(16, 17).isoformat(),
    })
    theirs = execute_tool(
        rival_ctx, "create_demo_reservation", {"quote_id": rival_quote["quote_id"]}
    )
    # Both requests exist at once: neither holds the car off the market, by design.
    assert mine["status"] == theirs["status"] == ReservationStatus.HELD.value
    assert mine["reservation_id"] != theirs["reservation_id"]

    assert _confirm(holding_ctx, mine["reservation_id"])["outcome"] == "confirmed"

    # Confirmation is the only thing standing between two approvals and a
    # customer arriving for a car that is already out.
    refused = _confirm(rival_ctx, theirs["reservation_id"])
    assert refused["outcome"] == "conflict"
    assert "double-book" in refused["message"]
    assert holding_ctx.reservations.get(theirs["reservation_id"]).status == "held"


def test_a_late_approval_rechecks_too(on_hold):
    """The car may have gone in the two hours nobody answered."""
    from rental_agent.services import booking as booking_service

    waiting = booking_service.live_hold(on_hold)
    _age(on_hold, waiting.reservation_id, 121)
    booking_service.expire_holds(on_hold)

    # Somebody else's booking lands on the car while the request sat unanswered.
    rival_ctx = _another_customer(on_hold)
    rival_quote = execute_tool(rival_ctx, "create_demo_quote", {
        "vehicle_id": waiting.vehicle_id,
        "pickup_at": waiting.pickup_at.isoformat(),
        "return_at": waiting.return_at.isoformat(),
    })
    taken = execute_tool(
        rival_ctx, "create_demo_reservation", {"quote_id": rival_quote["quote_id"]}
    )
    assert _confirm(rival_ctx, taken["reservation_id"])["outcome"] == "confirmed"

    refused = _confirm(on_hold, waiting.reservation_id)
    assert refused["outcome"] == "conflict"
    assert on_hold.reservations.get(waiting.reservation_id).status == "cancelled"


def test_confirming_prices_the_window_it_is_confirming(on_hold):
    """Not the quote taken earlier — that one may have expired, and the dates
    may not even be the same ones any more."""
    from rental_agent.services import booking as booking_service

    waiting = booking_service.live_hold(on_hold)
    execute_tool(on_hold, "modify_demo_reservation", {
        "reservation_id": waiting.reservation_id,
        "return_at": dt(17, 17).isoformat(),          # a day longer than requested
    })
    was = waiting.total_charge

    result = _confirm(on_hold, waiting.reservation_id)
    confirmed = on_hold.reservations.get(waiting.reservation_id)

    assert result["outcome"] == "confirmed"
    assert result["price_changed"] is True
    assert confirmed.total_charge > was
    assert str(confirmed.total_charge) == result["total_charge"]


def test_the_owners_answer_confirms_the_dates_the_customer_now_wants(on_hold):
    from rental_agent.services import booking as booking_service

    waiting = booking_service.live_hold(on_hold)
    execute_tool(on_hold, "modify_demo_reservation", {
        "reservation_id": waiting.reservation_id,
        "pickup_at": dt(14, 20).isoformat(),          # "make it 8 instead"
    })

    result = _confirm(on_hold, waiting.reservation_id)
    confirmed = on_hold.reservations.get(waiting.reservation_id)

    assert confirmed.status == "confirmed"
    assert confirmed.pickup_at == dt(14, 20)
    assert result["applied_change"]["pickup_at"].startswith("2026-09-14T20:00")
    # And what the owner approved is stated in those terms, not the old ones.
    assert "20:00" in result["summary"]
    assert waiting.reservation_id in result["summary"]


def test_the_owner_hears_about_the_change_before_they_answer(on_hold):
    from rental_agent.services import booking as booking_service

    waiting = booking_service.live_hold(on_hold)
    execute_tool(on_hold, "modify_demo_reservation", {
        "reservation_id": waiting.reservation_id,
        "pickup_at": dt(14, 20).isoformat(),
    })

    [notice] = booking_service.unsent_change_notices(on_hold)
    assert notice["reservation_id"] == waiting.reservation_id
    assert "pickup at" in notice["wanted"]
    # Once only: the owner must not be told the same thing on every later turn.
    assert booking_service.unsent_change_notices(on_hold) == []


# --- the customer can withdraw a request nobody has confirmed ---------------


def test_withdrawing_costs_nothing_and_closes_the_question(on_hold):
    from rental_agent.services import booking as booking_service
    from rental_agent.services import handover

    waiting = booking_service.live_hold(on_hold)
    result = execute_tool(on_hold, "cancel_demo_reservation", {
        "reservation_id": waiting.reservation_id,
        "reason": "changed their mind",
    })

    assert result["withdrawn"] is True
    # No fee ladder: the ladder prices a booking somebody had, and nobody had this.
    assert result["cancellation_fee"] == "0.00"
    assert result["cancellation_band"] == "not_confirmed"

    case = on_hold.escalations.latest_for_conversation(on_hold.conversation_id)
    assert case.status == handover.WITHDRAWN
    assert case.status not in handover.OPEN_STATUSES
    assert handover.find_case(on_hold, button_id=f"approve:{case.case_code}") is None


def test_an_approval_after_a_withdrawal_does_not_revive_it(on_hold):
    from rental_agent.services import booking as booking_service

    waiting = booking_service.live_hold(on_hold)
    execute_tool(on_hold, "cancel_demo_reservation", {"reservation_id": waiting.reservation_id})

    answered = booking_service.apply_hold_decision(
        on_hold, waiting.reservation_id, "approved"
    )
    assert answered["outcome"] == "withdrawn"
    assert on_hold.reservations.get(waiting.reservation_id).status == "cancelled"


def test_a_withdrawn_request_backs_nothing(on_hold):
    from rental_agent.services import booking as booking_service

    waiting = booking_service.live_hold(on_hold)
    execute_tool(on_hold, "cancel_demo_reservation", {"reservation_id": waiting.reservation_id})

    assert booking_service.confirmation_backing(on_hold) == booking_service.NOTHING
    agent = _agent("You're all set — it's confirmed.", "It's confirmed.")
    turn = agent.respond(on_hold, "is it booked?")
    assert turn.reply == holds.NO_BOOKING_REPLY


def test_a_confirmed_booking_still_cancels_on_the_fee_ladder(booking_ctx, reserved):
    result = execute_tool(booking_ctx, "cancel_demo_reservation", {
        "reservation_id": reserved["reservation_id"],
    })

    assert "withdrawn" not in result
    assert result["cancellation_band"] in {"free", "late", "no_show"}


# --- the whole sequence, through the webhook --------------------------------
#
# Request taken → customer moves the time → owner taps Confirm → the booking is
# the time they asked for, and the message says so. Every link in that chain was
# a place the change could have been dropped.


@pytest.fixture
def owner_app(session_factory, holding_ctx):
    from fastapi.testclient import TestClient

    from rental_agent.agent.loop import AgentTurn
    from rental_agent.whatsapp.settings import WhatsAppSettings
    from rental_agent.whatsapp.webhook import create_app
    from tests.conftest import FROZEN_NOW, REFERENCE_DATE
    from tests.test_whatsapp import APP_SECRET, VERIFY_TOKEN, RecordingClient, StubAgent

    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply="Noted."))
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: agent,
        settings=WhatsAppSettings(
            phone_number_id="PID", access_token="TOK",
            app_secret=APP_SECRET, verify_token=VERIFY_TOKEN,
            staff_number="971500009999",
        ),
        client=outbound,
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    return TestClient(app), outbound, agent


def _asked_of_the_owner(ctx):
    """Put the hold's case in front of the owner, the way a live turn does.

    `create_demo_reservation` raises the case; the webhook opens it when it
    sends the question. A case nobody was asked is not answerable, which is
    correct, and is why the tests have to do this explicitly.
    """
    from rental_agent.services import handover

    # The customer wrote to us, or there would be no request and no open
    # 24-hour window to answer them in.
    ctx.messages.record(
        conversation_id=ctx.conversation_id or "",
        direction="inbound",
        content="book it please",
        now=ctx.now(),
    )
    case = ctx.escalations.latest_for_conversation(ctx.conversation_id)
    return handover.open_case(ctx, case, "is this car free?")


def _owner_taps(client, button_id, title="Confirm the car"):
    from tests.test_whatsapp import post, text_payload

    payload = text_payload(sender="971500009999", message_id="wamid.OWNER1")
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "interactive"
    message["interactive"] = {
        "type": "button_reply",
        "button_reply": {"id": button_id, "title": title},
    }
    return post(client, payload)


def test_the_change_the_customer_asked_for_is_what_ends_up_booked(owner_app, holding_ctx):
    from rental_agent.services import booking as booking_service

    client, outbound, agent = owner_app

    quote, _ = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    waiting = booking_service.live_hold(holding_ctx)
    case = _asked_of_the_owner(holding_ctx)

    # "actually, make it 8 instead"
    execute_tool(holding_ctx, "modify_demo_reservation", {
        "reservation_id": waiting.reservation_id,
        "pickup_at": dt(14, 20).isoformat(),
    })
    holding_ctx.session.commit()

    assert _owner_taps(client, f"approve:{case.case_code}").status_code == 200

    booked = holding_ctx.reservations.get(waiting.reservation_id)
    holding_ctx.session.refresh(booked)
    assert booked.status == "confirmed"
    assert booked.pickup_at == dt(14, 20)

    # And the customer is told about the booking they actually have.
    [directive] = agent.relays
    assert "20:00" in directive
    assert waiting.reservation_id in directive
    assert "differs from what you last discussed" in directive
    assert any(to == "+971500000001" for to, _ in outbound.texts)


def test_an_approval_the_car_cannot_take_tells_the_customer_the_truth(owner_app, holding_ctx):
    """The owner says yes and the car is gone. They must hear the outcome, not
    the intention."""
    from rental_agent.services import booking as booking_service

    client, outbound, agent = owner_app

    quote, vehicle = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    waiting = booking_service.live_hold(holding_ctx)
    case = _asked_of_the_owner(holding_ctx)

    rival_ctx = _another_customer(holding_ctx)
    rival_quote = execute_tool(rival_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": waiting.pickup_at.isoformat(),
        "return_at": waiting.return_at.isoformat(),
    })
    theirs = execute_tool(
        rival_ctx, "create_demo_reservation", {"quote_id": rival_quote["quote_id"]}
    )
    _confirm(rival_ctx, theirs["reservation_id"])
    holding_ctx.session.commit()

    _owner_taps(client, f"approve:{case.case_code}")

    still_waiting = holding_ctx.reservations.get(waiting.reservation_id)
    holding_ctx.session.refresh(still_waiting)
    assert still_waiting.status == "held"

    [directive] = agent.relays
    assert "NOT available" in directive


def test_a_withdrawn_request_is_not_resurrected_by_a_late_tap(owner_app, holding_ctx):
    from rental_agent.services import booking as booking_service

    client, outbound, agent = owner_app

    quote, _ = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    waiting = booking_service.live_hold(holding_ctx)
    case = _asked_of_the_owner(holding_ctx)

    execute_tool(holding_ctx, "cancel_demo_reservation", {
        "reservation_id": waiting.reservation_id, "reason": "changed their mind",
    })
    holding_ctx.session.commit()

    _owner_taps(client, f"approve:{case.case_code}")

    withdrawn = holding_ctx.reservations.get(waiting.reservation_id)
    holding_ctx.session.refresh(withdrawn)
    assert withdrawn.status == "cancelled"
    # Nothing is said to the customer, because nothing happened to them.
    assert agent.relays == []
    assert any("no open cases" in text.lower() for _, text in outbound.texts)


# --- and the tick must not say what the words are forbidden from saying ------


def _booking_agent(quote_id, reply):
    """An agent that actually takes the booking, the way a real turn does."""
    from tests.fake_anthropic import FakeClient, calls, says

    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings

    return Agent(
        FakeClient(script=[
            calls("create_demo_reservation", quote_id=quote_id),
            says(reply),
        ]),
        AgentSettings(extraction_enabled=False),
    )


def test_a_request_awaiting_confirmation_is_not_ticked(holding_ctx):
    """A green tick says "done" in the one language a customer cannot misread.
    The words were guarded and the emoji was not, which left the reaction as the
    last place the old claim could still get out."""
    from rental_agent.whatsapp import reactions as reactions_mod

    quote, _ = quote_anything(holding_ctx, 14)
    agent = _booking_agent(quote["quote_id"],
                           "Your request is in and a colleague is confirming it now.")
    turn = agent.respond(holding_ctx, "book it")

    assert turn.booking_awaits_confirmation is True
    assert reactions_mod.for_turn(turn, holding_ctx.engine.rules) is None


def test_a_real_booking_is_still_ticked(booking_ctx):
    """This operator's availability is in the fleet file, so the booking is
    real and the tick is true."""
    from rental_agent.whatsapp import reactions as reactions_mod

    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(booking_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": dt(10, 17).isoformat(),
        "return_at": dt(12, 17).isoformat(),
    })
    agent = _booking_agent(quote["quote_id"], "All booked — your reference is on its way.")
    turn = agent.respond(booking_ctx, "book it")

    assert turn.booking_awaits_confirmation is False
    assert "create_demo_reservation" in turn.tools_succeeded
    assert reactions_mod.for_turn(turn, booking_ctx.engine.rules) == "✅"


# --- saying the car is being kept, when nothing keeps it ---------------------


@pytest.mark.parametrize("said", [
    "I have created a holding reservation for you — reference DEMO-1047.",
    "I am holding the car for you until Friday.",
    "We have put the vehicle aside for you.",
    "It is on hold for you.",
    "I've made a hold on that car.",
])
def test_saying_the_car_is_being_kept_is_a_claim(said):
    """Observed live, one message after the agent correctly said "a request
    awaiting confirmation": *I have created a holding reservation for you*.
    Nothing holds the car — the same vehicle is still being quoted to other
    customers — so this is as untrue as saying it is booked, and reads to a
    customer as more certain."""
    assert holds.inspect(said).explicit is True


@pytest.mark.parametrize("said", [
    "Your team takes a holding payment of AED 500 to secure a booking.",
    "The holding fee is non-refundable.",
    "I will hold that thought.",
])
def test_a_holding_payment_is_money_not_a_claim_about_the_car(said):
    assert bool(holds.inspect(said)) is False


def test_the_owner_is_told_the_car_went_not_that_they_declined(owner_app, holding_ctx):
    """They tapped "Confirm the car". Telling them the case is *declined* is
    accurate about the case and baffling next to the button they pressed."""
    from rental_agent.services import booking as booking_service

    client, outbound, agent = owner_app

    quote, vehicle = quote_anything(holding_ctx, 14)
    execute_tool(holding_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
    waiting = booking_service.live_hold(holding_ctx)
    case = _asked_of_the_owner(holding_ctx)

    rival_ctx = _another_customer(holding_ctx)
    rival_quote = execute_tool(rival_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": waiting.pickup_at.isoformat(),
        "return_at": waiting.return_at.isoformat(),
    })
    theirs = execute_tool(
        rival_ctx, "create_demo_reservation", {"quote_id": rival_quote["quote_id"]}
    )
    _confirm(rival_ctx, theirs["reservation_id"])
    holding_ctx.session.commit()

    _owner_taps(client, f"approve:{case.case_code}")

    to_owner = [text for to, text in outbound.texts if to == "971500009999"]
    assert any("no longer free" in text for text in to_owner)
    assert not any("marked *declined*" in text for text in to_owner)
