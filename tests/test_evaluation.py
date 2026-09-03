"""The event-based evaluator.

Every check here is computed from recorded events, so a finding is a fact rather
than an opinion and the same conversation always evaluates the same way. That
also means the whole evaluator is testable without a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from rental_agent.evaluation import checks
from rental_agent.tools.registry import execute_tool
from rental_agent.evaluation.checks import Finding, run_all, supported_numbers


@dataclass
class Msg:
    direction: str
    content: str
    id: int = 1


@dataclass
class Call:
    tool_name: str
    result: dict[str, Any]
    arguments: dict[str, Any] | None = None


@dataclass
class State:
    asked_slots: list[str]
    redundant_asks: list[str] = field(default_factory=list)


def types_of(findings: list[Finding]) -> list[str]:
    return [f.type for f in findings]


# --------------------------------------------------------------------------
# Unsupported claims — the guard on the project's central promise
# --------------------------------------------------------------------------


def test_a_price_no_tool_produced_is_caught():
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "That comes to AED 7,560 all in.")],
        [Call("calculate_quote", {"total_charge": "6300.00"})],
    )
    assert types_of(findings) == ["unsupported_claim"]
    assert findings[0].severity == "high"
    assert "7560" in findings[0].evidence["unsupported"]


def test_a_price_a_tool_did_produce_is_accepted():
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "That comes to AED 7,560 all in.")],
        [Call("calculate_quote", {"total_charge": "7560.00"})],
    )
    assert findings == []


def test_formatting_differences_are_not_mistaken_for_invention():
    """The tool returns 7560.00 and the agent writes AED 7,560 — the same fact."""
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "*AED 7,560* total, deposit AED 5,000.")],
        [Call("calculate_quote", {"total_charge": "7560.00", "deposit": "5000.00"})],
    )
    assert findings == []


def test_figures_nested_deep_in_a_tool_result_still_count():
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "The Fortuner is AED 350 a day, total AED 1,102.50.")],
        [
            Call(
                "search_available_vehicles",
                {"vehicles": [{"daily_price": "350", "estimated_total": "1102.50"}]},
            )
        ],
    )
    assert findings == []


def test_a_customers_own_claim_is_not_evidence():
    """"You always give me 20% off" must not license the agent to say 20%."""
    findings = checks.check_unsupported_claims(
        [
            Msg("inbound", "you always give me 8500 off", 1),
            Msg("outbound", "I can do AED 8,500 off for you.", 2),
        ],
        [Call("get_allowed_discount", {"max_percent": "10"})],
    )
    assert types_of(findings) == ["unsupported_claim"]


def test_small_conversational_numbers_are_not_treated_as_prices():
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "I have 2 options for you, over 3 days, 7 seats.")], []
    )
    assert findings == []


def test_a_discount_below_a_permitted_ceiling_is_supported():
    """Offering 5% against a 10% ceiling is exactly the behaviour we want — it
    must not be reported as an invented figure."""
    supported = supported_numbers([Call("get_allowed_discount", {"max_percent": "10"})])
    assert Decimal("5") in supported
    assert Decimal("10") in supported
    assert Decimal("15") not in supported


# --------------------------------------------------------------------------
# Escalation — the costliest failure
# --------------------------------------------------------------------------


def test_an_unescalated_accident_is_caught():
    findings = checks.check_missed_escalation(
        [Msg("inbound", "I've had an accident on Sheikh Zayed Road")], [], escalated=False
    )
    assert types_of(findings) == ["missed_escalation"]
    assert findings[0].severity == "high"


@pytest.mark.parametrize(
    "text", ["the car broke down", "someone hit another car", "my wife is in hospital",
             "the car was stolen", "police are here"],
)
def test_incident_language_is_recognised(text):
    assert checks.check_missed_escalation([Msg("inbound", text)], [], escalated=False)


def test_an_escalated_accident_is_not_a_finding():
    assert checks.check_missed_escalation(
        [Msg("inbound", "I've had an accident")], [], escalated=True
    ) == []


def test_the_escalation_tool_counts_even_if_state_lags():
    assert checks.check_missed_escalation(
        [Msg("inbound", "I've had an accident")],
        [Call("escalate_conversation", {"escalated": True})],
        escalated=False,
    ) == []


def test_ordinary_conversation_does_not_trigger_escalation():
    assert checks.check_missed_escalation(
        [Msg("inbound", "can I get a car for the weekend?")], [], escalated=False
    ) == []


# --------------------------------------------------------------------------
# Discounts, repetition, alternatives, overload
# --------------------------------------------------------------------------


def test_a_discount_offered_without_checking_is_caught():
    findings = checks.check_discount_without_authority(
        [Msg("outbound", "I can do 15% off for you.")], []
    )
    assert types_of(findings) == ["unauthorised_discount"]


def test_vat_is_not_mistaken_for_a_discount():
    assert checks.check_discount_without_authority(
        [Msg("outbound", "AED 7,560 including VAT at 5%.")], []
    ) == []


def test_a_checked_discount_is_fine():
    assert checks.check_discount_without_authority(
        [Msg("outbound", "I can do 10% off.")], [Call("get_allowed_discount", {"max_percent": "10"})]
    ) == []


def test_asking_for_something_already_known_is_caught():
    findings = checks.check_repeated_questions(
        State(["pickup_at", "return_at", "pickup_at"], ["pickup_at"])
    )
    assert types_of(findings) == ["repeated_question"]
    assert findings[0].evidence["slot"] == "pickup_at"


def test_asking_different_things_is_not():
    assert checks.check_repeated_questions(State(["pickup_at", "return_at"])) == []


def test_asking_twice_because_the_customer_never_answered_is_not():
    """A question the customer walked past is not a mistake to learn from —
    flagging it would teach the agent to stop chasing missing details."""
    assert checks.check_repeated_questions(State(["pickup_at", "pickup_at"])) == []


def test_saying_unavailable_with_nothing_to_offer_is_caught():
    findings = checks.check_unavailable_without_alternatives(
        [Msg("outbound", "Sorry, the G63 is not available for those dates.")], []
    )
    assert types_of(findings) == ["no_alternatives_offered"]


def test_saying_unavailable_after_finding_alternatives_is_fine():
    assert checks.check_unavailable_without_alternatives(
        [Msg("outbound", "The G63 is unavailable, but I have a Range Rover.")],
        [Call("find_alternatives", {"count": 1})],
    ) == []


def test_listing_too_many_cars_is_caught():
    findings = checks.check_option_overload(
        [
            Msg(
                "outbound",
                "*Kia Pegas — 2024* ... *Nissan Sunny — 2024* ... "
                "*Toyota Yaris — 2025* ... *Toyota Camry — 2025*",
            )
        ]
    )
    assert types_of(findings) == ["too_many_options"]
    assert len(findings[0].evidence["vehicles"]) == 4


def test_three_cars_is_within_the_rule():
    assert checks.check_option_overload(
        [Msg("outbound", "*Kia Pegas — 2024* ... *Nissan Sunny — 2024* ... *Toyota Yaris — 2025*")]
    ) == []


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_findings_come_back_worst_first():
    findings = run_all(
        messages=[
            Msg("inbound", "I crashed the car", 1),
            Msg("outbound", "*A — 2024* *B — 2024* *C — 2024* *D — 2024* costs AED 9,999", 2),
        ],
        tool_calls=[Call("calculate_quote", {"total_charge": "7560.00"})],
        state=State(["pickup_at", "pickup_at"]),
        escalated=False,
    )
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
    assert "missed_escalation" in types_of(findings)
    assert "unsupported_claim" in types_of(findings)


def test_a_clean_conversation_produces_no_findings():
    findings = run_all(
        messages=[
            Msg("inbound", "black G63 friday to monday, marina", 1),
            Msg("outbound", "The *Mercedes-AMG G63 — 2025* is AED 7,560 for 3 days.", 2),
        ],
        tool_calls=[Call("calculate_quote", {"total_charge": "7560.00", "billable_days": 3})],
        state=State(["pickup_at"]),
        escalated=False,
    )
    assert findings == []


# --------------------------------------------------------------------------
# False positives — an evaluator that cries wolf gets ignored
# --------------------------------------------------------------------------


def test_a_model_year_in_a_car_name_is_not_a_price_claim():
    """"*Mercedes-AMG G63 — 2025*" appears in nearly every message. Reading the
    year as an invented figure would make every finding noise."""
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "The *Mercedes-AMG G63 — 2025* is AED 7,560 for 3 days.")],
        [Call("calculate_quote", {"total_charge": "7560.00"})],
    )
    assert findings == []


def test_a_date_is_not_a_price_claim():
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "Delivery 2026-09-04T19:00:00+04:00, total AED 7,560.")],
        [Call("calculate_quote", {"total_charge": "7560.00"})],
    )
    assert findings == []


def test_a_genuine_invented_price_is_still_caught_alongside_a_car_name():
    """The exclusions must not become a hole to hide a real claim in."""
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "The *Mercedes-AMG G63 — 2025* is AED 9,999 for 3 days.")],
        [Call("calculate_quote", {"total_charge": "7560.00"})],
    )
    assert types_of(findings) == ["unsupported_claim"]
    assert findings[0].evidence["unsupported"] == ["9999"]


# --------------------------------------------------------------------------
# Against real stored conversations
# --------------------------------------------------------------------------


def make_conversation(ctx, turns, escalated=False, priced=True):
    """Write a transcript and its tool calls the way the agent would.

    `priced` runs a real quote first, so the audit log holds figures to check
    claims against — without it the evaluator abstains, as it should.
    """
    from rental_agent.tools.registry import execute_tool
    from tests.conftest import dt as _dt

    if priced:
        execute_tool(
            ctx,
            "calculate_quote",
            {
                "vehicle_id": "veh_13",
                "pickup_at": _dt(4, 19).isoformat(),
                "return_at": _dt(7, 19).isoformat(),
            },
        )

    for direction, content in turns:
        ctx.messages.record(
            conversation_id=ctx.conversation_id,
            direction=direction,
            content=content,
            now=ctx.now(),
        )
    if escalated:
        execute_tool(ctx, "escalate_conversation", {"reason": "accident"})
    ctx.session.flush()


def test_a_clean_booking_conversation_passes(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_conversation
    from tests.test_booking import book

    reservation = book(booking_ctx)
    make_conversation(
        booking_ctx,
        [
            ("inbound", "black G63 friday to monday in marina"),
            ("outbound", f"Booked — reference {reservation['reservation_id']}, "
                         f"total AED {reservation['total_charge']}."),
        ],
    )
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    assert result.passed, [f.type for f in result.findings]


def test_an_invented_total_is_caught_in_a_real_conversation(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_conversation
    from tests.test_booking import book

    book(booking_ctx)
    make_conversation(
        booking_ctx,
        [
            ("inbound", "how much all in?"),
            ("outbound", "That'll be AED 6,200 all in."),   # engine said 7,560
        ],
    )
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    assert not result.passed
    assert "unsupported_claim" in [f.type for f in result.findings]


def test_an_unescalated_accident_is_caught_in_a_real_conversation(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_conversation

    make_conversation(
        booking_ctx,
        [
            ("inbound", "I crashed the car on Sheikh Zayed Road"),
            ("outbound", "Sorry to hear that. Would you like to book another one?"),
        ],
    )
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    assert result.worst_severity == "high"
    assert "missed_escalation" in [f.type for f in result.findings]


def test_a_properly_escalated_accident_passes(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_conversation

    make_conversation(
        booking_ctx,
        [
            ("inbound", "I crashed the car"),
            ("outbound", "Are you safe? Call 999 if anyone is hurt. A colleague is taking over."),
        ],
        escalated=True,
    )
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    assert "missed_escalation" not in [f.type for f in result.findings]


def test_findings_are_persisted_with_their_evidence(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_conversation, record

    make_conversation(
        booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")]
    )
    row = record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    assert row.passed is False
    assert row.findings[0]["evidence"]["unsupported"] == ["4321"]


def test_the_same_mistake_across_conversations_is_one_lesson(booking_ctx, session):
    """A habit repeated is one thing worth correcting, not several — and the
    count means distinct conversations."""
    from rental_agent.context import ToolContext
    from rental_agent.evaluation.evaluator import evaluate_conversation, open_mistakes, record
    from tests.conftest import FROZEN_NOW, REFERENCE_DATE

    for phone in ("+971500000021", "+971500000022"):
        ctx = ToolContext(session=session, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)
        cust, _ = ctx.customers.get_or_create(phone, FROZEN_NOW)
        conv, _ = ctx.conversations.get_or_create(cust.customer_id, FROZEN_NOW)
        ctx.customer_id, ctx.conversation_id = cust.customer_id, conv.conversation_id
        make_conversation(ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
        record(ctx, evaluate_conversation(ctx, ctx.conversation_id))

    mistakes = [m for m in open_mistakes(booking_ctx) if m.type == "unsupported_claim"]
    assert len(mistakes) == 1
    assert mistakes[0].occurrences == 2


def test_re_evaluating_a_conversation_does_not_inflate_the_count(booking_ctx):
    """Otherwise a habit seen once looks like a crisis after a few re-runs."""
    from rental_agent.evaluation.evaluator import evaluate_conversation, open_mistakes, record

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    for _ in range(3):
        record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))

    mistakes = [m for m in open_mistakes(booking_ctx) if m.type == "unsupported_claim"]
    assert len(mistakes) == 1
    assert mistakes[0].occurrences == 1


def test_open_mistakes_come_back_worst_first(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_conversation, open_mistakes, record

    make_conversation(
        booking_ctx,
        [
            ("inbound", "I had an accident"),
            ("outbound", "Sorry. The G63 is not available anyway. AED 9,876 for the other one."),
        ],
    )
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    severities = [m.severity for m in open_mistakes(booking_ctx)]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


def test_the_evaluator_abstains_when_it_cannot_see():
    """Conversations logged before tool results were retained have empty
    results. Checking against them would flag every correct figure as invented —
    a wall of false accusations is how a learning loop teaches itself the wrong
    lesson."""
    blind = [Call("search_available_vehicles", {}), Call("get_customer", {})]
    assert checks.has_usable_evidence(blind) is False
    assert checks.check_unsupported_claims(
        [Msg("outbound", "The Fortuner is AED 350 a day, total AED 1,102.50.")], blind
    ) == []


def test_it_still_judges_when_even_one_result_was_recorded():
    partial = [Call("get_customer", {}), Call("calculate_quote", {"total_charge": "7560.00"})]
    assert checks.has_usable_evidence(partial) is True
    assert checks.check_unsupported_claims([Msg("outbound", "AED 9,999 please.")], partial)


def test_no_tool_calls_at_all_is_also_not_judged():
    """A conversation with no tools yet — a greeting, a question — has nothing
    to check against and is not evidence of anything."""
    assert checks.check_unsupported_claims([Msg("outbound", "AED 5,000 maybe")], []) == []


def test_a_model_year_in_parentheses_is_not_a_price():
    findings = checks.check_unsupported_claims(
        [Msg("outbound", "We have a white *Toyota Fortuner (2025)* at AED 350 a day.")],
        [Call("search_available_vehicles", {"vehicles": [{"daily_price": "350"}]})],
    )
    assert findings == []


def test_a_returning_customer_is_greeted_as_one_only_at_the_start(booking_ctx):
    """Observed live: "Welcome back! It's lovely to assist you again" — twice,
    in the middle of a conversation it was already having. The flag was true on
    every turn, when it describes a greeting."""
    from rental_agent.services.booking import get_customer
    from tests.conftest import dt

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
    execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})

    # A fresh conversation with a customer who has booked before.
    conversation, _ = booking_ctx.conversations.get_or_create(
        booking_ctx.customer_id, booking_ctx.now()
    )
    booking_ctx.conversation_id = conversation.conversation_id
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound", content="hi again", now=booking_ctx.now(),
    )
    booking_ctx.session.flush()
    assert get_customer(booking_ctx)["is_returning_customer"] is True

    # Once we have replied, this is no longer a greeting.
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="outbound", content="Welcome back!", now=booking_ctx.now(),
    )
    booking_ctx.session.flush()
    assert get_customer(booking_ctx)["is_returning_customer"] is False
