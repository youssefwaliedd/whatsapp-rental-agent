"""Refusing to send a price no tool produced.

Asked for 4-6 September — a two-day rental — the agent said AED 2,831.90. That
is the daily rate times *three* days plus VAT: the engine cannot produce it for
those dates, and the model did the arithmetic itself. It corrected to AED
1,887.90 only because it was asked to show the calculation, which most customers
will never do.

`check_unsupported_claims` finds exactly this, afterwards, once the customer has
the number. So the same rule now runs on the way out, with one chance to write
the message again and a fallback that promises nothing.

The stricter half is what makes it usable. An evaluator that flags a model year
is noise; a guard that flags one stops a message about a car.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from rental_agent.agent.figures import SAFE_REPLY, correction, inspect, money_in


@dataclass
class Call:
    tool_name: str
    result: dict
    arguments: Any = None


QUOTED = [Call("create_demo_quote", {"total_charge": "1887.90", "daily_price": "899",
                                     "quote_id": "DQ-510"})]


# --- what counts as a price -------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("AED 1,887.90", {Decimal("1887.90")}),
    ("1,887.90 AED", {Decimal("1887.90")}),
    ("the total is 2,831.85", {Decimal("2831.85")}),
    ("that comes to 1887.90", {Decimal("1887.90")}),
    ("AED 899 per day", {Decimal("899")}),
])
def test_amounts_are_recognised(text, expected):
    assert money_in(text) == expected


@pytest.mark.parametrize("text", [
    "The BMW M4 Competition 2025 is a great choice.",
    "250 km included per day.",
    "Delivery between 9 AM and 10 PM.",
    "Your reference is DEMO-1046.",
    "Friday 4 Sep to Sunday 6 Sep.",
])
def test_things_that_are_not_prices_are_left_alone(text):
    # A guard that stops a message about a 2025 model year is worse than no
    # guard: it blocks good replies and gets switched off.
    assert money_in(text) == set()


# --- the verdict ------------------------------------------------------------


def test_a_figure_the_engine_produced_passes():
    assert inspect("The total is AED 1,887.90.", QUOTED).ok is True


def test_the_three_day_total_for_a_two_day_rental_is_caught():
    verdict = inspect("The total comes to AED 2,831.85.", QUOTED)
    assert verdict.ok is False
    assert verdict.unsupported == (Decimal("2831.85"),)


def test_a_turn_with_no_tools_is_not_second_guessed():
    # "Good morning" has no evidence to check against, and treating that as a
    # violation would block every conversational reply.
    assert inspect("Good morning! How can I help?", []).ok is True


def test_the_correction_names_the_figure_and_the_alternatives():
    verdict = inspect("The total comes to AED 2,831.85.", QUOTED)
    told = correction(verdict)
    assert "2,831.85" in told
    assert "1,887.90" in told
    # Naming the specific mistake, because "you invented a number" is not
    # something a model can act on.
    assert "Never calculate a total yourself" in told


def test_the_fallback_promises_nothing():
    assert not money_in(SAFE_REPLY)
    assert "could not verify" in SAFE_REPLY.lower()


# --- through the agent ------------------------------------------------------


def _agent(*replies):
    from tests.fake_anthropic import FakeClient, says

    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings

    return Agent(FakeClient(script=[says(r) for r in replies]),
                 AgentSettings(extraction_enabled=False))


def test_an_invented_figure_is_rewritten(booking_ctx):
    agent = _agent("That will be AED 8,765 all in.",
                   "Let me confirm the exact total and come back to you.")
    turn = agent.respond(booking_ctx, "how much?")

    assert "8,765" not in turn.reply
    # Recorded even though the retry succeeded, because it happened.
    assert turn.invented_figures == ("8765",)


def test_a_model_that_insists_does_not_get_to_send_it(booking_ctx):
    agent = _agent("That will be AED 8,765 all in.", "Sorry — AED 8,765 all in.")
    turn = agent.respond(booking_ctx, "how much?")

    assert turn.reply == SAFE_REPLY
    assert turn.invented_figures == ("8765",)


def test_an_honest_reply_is_sent_untouched(booking_ctx):
    agent = _agent("Happy to help — when would you like the car?")
    turn = agent.respond(booking_ctx, "hi")

    assert turn.reply == "Happy to help — when would you like the car?"
    assert turn.invented_figures == ()


# --- and not saying a car is free until something has looked ----------------
#
# Observed: "the Audi RS3 is available for those dates", and two messages later,
# "the specific unit I initially looked at is unavailable". Nothing checked in
# between. The customer had already chosen on the strength of the first sentence.
#
# Availability is the one fact this operator publishes nowhere, which makes it
# the one the model has least business inferring.

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from rental_agent.agent import availability  # noqa: E402

TZ = ZoneInfo("Asia/Dubai")
PICKUP = datetime(2026, 9, 4, 17, tzinfo=TZ)
RETURN = datetime(2026, 9, 6, 17, tzinfo=TZ)


def searched(pickup=PICKUP, ret=RETURN, *, found=True):
    return Call(
        "search_available_vehicles",
        {"count": 1 if found else 0, "vehicles": [{"vehicle_id": "veh_01"}] if found else []},
        {"pickup_at": pickup.isoformat(), "return_at": ret.isoformat()},
    )


@pytest.mark.parametrize("said", [
    "The Audi RS3 is available for those dates.",
    "Good news, the BMW M4 Competition is available!",
    "Yes it is available now.",
    "We do have it for that weekend.",
])
def test_asserting_availability_is_recognised(said):
    assert availability.claims_available(said) is True


@pytest.mark.parametrize("said", [
    "Let me check whether it is available for those dates.",
    "I am checking availability now.",
    "I will confirm availability and come back to you.",
    "The total is AED 1,887.90.",
])
def test_saying_you_will_check_is_not_a_claim(said):
    # Blocking this would stop the agent saying what it is about to do.
    assert availability.claims_available(said) is False


def test_a_search_for_these_dates_supports_the_claim():
    assert availability.checked_for([searched()], PICKUP, RETURN) is True


def test_a_search_for_different_dates_does_not():
    # How "available" and "booked out" ended up two messages apart.
    other = datetime(2026, 9, 20, 17, tzinfo=TZ)
    assert availability.checked_for([searched(other, other)], PICKUP, RETURN) is False


def test_a_search_that_found_nothing_does_not():
    assert availability.checked_for([searched(found=False)], PICKUP, RETURN) is False


def test_a_quote_counts_as_a_check():
    # The engine validates availability before it will price anything.
    quote = Call("create_demo_quote", {"quote_id": "DQ-1", "total_charge": "1887.90"},
                 {"pickup_at": PICKUP.isoformat(), "return_at": RETURN.isoformat()})
    assert availability.checked_for([quote], PICKUP, RETURN) is True


def test_a_failed_quote_does_not():
    failed = Call("create_demo_quote", {"error": "vehicle_unavailable"},
                  {"pickup_at": PICKUP.isoformat(), "return_at": RETURN.isoformat()})
    assert availability.checked_for([failed], PICKUP, RETURN) is False


def test_an_unchecked_claim_is_rewritten(booking_ctx):
    booking_ctx.save_state(
        booking_ctx.load_state().model_copy(update={"pickup_at": PICKUP, "return_at": RETURN})
    )
    agent = _agent("The Audi RS3 is available for those dates.",
                   "Let me confirm that for you and come right back.")
    turn = agent.respond(booking_ctx, "is the RS3 free on the 4th?")

    assert "is available" not in turn.reply
    assert turn.unchecked_availability is True


def test_a_model_that_insists_does_not_get_to_promise_a_car(booking_ctx):
    booking_ctx.save_state(
        booking_ctx.load_state().model_copy(update={"pickup_at": PICKUP, "return_at": RETURN})
    )
    agent = _agent("The Audi RS3 is available for those dates.",
                   "It is available, yes.")
    turn = agent.respond(booking_ctx, "is the RS3 free on the 4th?")

    assert turn.reply == availability.SAFE_REPLY
    assert turn.unchecked_availability is True
