"""A figure nobody has confirmed is not zero.

Delta's listings advertise "no deposit required (T&Cs apply)" while their terms
require AED 5,000-20,000 subject to the vehicle — a range no engine can quote.
Encoding that as `0` made the agent state the marketing as policy: *"AED 0
refundable deposit"* on a Ferrari. The customer plans around it, arrives, and is
asked for thousands at handover.

So the unknown is now genuinely unknown all the way through — the model, the
quote, the tool result and the rendered message each have to say so rather than
resolve it to a comfortable number. The same applies to the per-kilometre rate,
where zero promises free kilometres past the allowance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from rental_agent.config import Rules, load_rules
from rental_agent.evaluation.checks import run_all
from rental_agent.formatting import UNCONFIRMED, quote_message, vehicle_card
from rental_agent.tools.rental_tools import _quote, _vehicle_detail, _vehicle_summary

from .conftest import FROZEN_NOW, dt


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
    redundant_asks: list[str]


@pytest.fixture
def unconfirmed_car(engine):
    """A real car from the fixture fleet, with the figures the operator has not
    confirmed removed — which is exactly the shape Delta's own data arrives in."""
    vehicle = engine.list_fleet()[0]
    return vehicle.model_copy(update={"deposit": None, "extra_km_price": None})


# --- what the customer sees -------------------------------------------------


def test_a_card_says_the_deposit_is_confirmed_rather_than_showing_zero(unconfirmed_car):
    card = vehicle_card(unconfirmed_car)
    assert UNCONFIRMED in card
    assert "AED 0" not in card


def test_a_confirmed_deposit_still_prints_as_a_figure(engine):
    card = vehicle_card(engine.list_fleet()[0])
    assert "refundable deposit" in card
    assert UNCONFIRMED not in card


def test_the_quote_never_shows_a_due_at_delivery_that_omits_the_deposit(engine, unconfirmed_car):
    quote = quote_for(engine, unconfirmed_car)
    assert quote.deposit is None
    # The dangerous version: a total that looks complete and is not.
    assert quote.total_due_at_delivery is None

    rendered = quote_message(quote, load_rules())
    assert "plus the deposit once confirmed" in rendered
    assert "AED 0" not in rendered


def test_no_zero_deposit_line_appears_among_the_quote_lines(engine, unconfirmed_car):
    quote = quote_for(engine, unconfirmed_car)
    assert not [line for line in quote.lines if line.code == "deposit"]


def test_an_unknown_per_km_rate_does_not_read_as_free_kilometres(engine, unconfirmed_car):
    rendered = quote_message(quote_for(engine, unconfirmed_car), load_rules())
    assert "km included" in rendered
    assert "0/km" not in rendered


# --- what the model is told -------------------------------------------------


def test_the_tool_result_names_what_is_unconfirmed_rather_than_omitting_it(unconfirmed_car, engine):
    summary = _vehicle_summary(unconfirmed_car)
    assert summary["deposit"] is None
    # Named, not merely absent: a missing key reads as "nothing to pay".
    assert "deposit" in summary["unconfirmed"]

    detail = _vehicle_detail(unconfirmed_car, engine)
    assert set(detail["unconfirmed"]) >= {"deposit", "extra_km_price"}


def test_a_null_figure_never_reaches_the_model_as_the_string_none(unconfirmed_car, engine):
    payload = _quote(quote_for(engine, unconfirmed_car))
    assert payload["deposit"] is None
    assert payload["total_due_at_delivery"] is None
    assert "None" not in [payload["deposit"], payload["extra_km_price"]]


def test_a_confirmed_vehicle_reports_nothing_unconfirmed(engine):
    assert _vehicle_summary(engine.list_fleet()[0])["unconfirmed"] == []


# --- a floor is not a figure ------------------------------------------------


def test_an_excess_published_as_a_range_is_rendered_with_from(engine):
    quote = quote_for(engine, engine.list_fleet()[0]).model_copy(
        update={"insurance_excess_is_minimum": True}
    )
    assert "Insurance excess: from AED" in quote_message(quote, load_rules())


def test_an_operator_with_a_fixed_excess_still_states_it_plainly(engine):
    quote = quote_for(engine, engine.list_fleet()[0])
    rendered = quote_message(quote.model_copy(update={"insurance_excess_is_minimum": False}),
                             load_rules())
    assert "Insurance excess: AED" in rendered
    assert "from AED" not in rendered


def test_the_flag_is_read_from_the_rules_rather_than_hardcoded():
    assert Rules({"insurance": {"cdw_excess_is_minimum": True}}).excess_is_minimum is True
    assert Rules({"insurance": {}}).excess_is_minimum is False


def test_deltas_own_config_marks_the_excess_as_a_floor():
    # 17.2 gives "from AED 5,000 up to, depending on the car model". The fixture
    # operator has a fixed excess, so only the real config can assert this — and
    # it is worth asserting, because the flag going missing would silently turn a
    # floor back into a figure.
    live = Rules(json.loads((Path(__file__).parents[1] / "config/rules.json").read_text()))
    assert live.excess_is_minimum is True


# --- what the evaluator catches ---------------------------------------------


def test_saying_there_is_no_deposit_is_caught_even_though_it_has_no_number():
    calls = [Call("get_vehicle_details", {"deposit": None, "unconfirmed": ["deposit"]})]
    findings = run_all(
        [Msg("outbound", "Great news — no deposit needed on this one!")],
        calls, State([], []), escalated=False,
    )
    assert "absence_claimed_for_unconfirmed_figure" in [f.type for f in findings]
    assert findings[0].severity == "high"


@pytest.mark.parametrize("phrasing", [
    "there is no security deposit",
    "you can take it without a deposit",
    "it's deposit-free",
    "the deposit is waived for you",
    "zero deposit on this car",
])
def test_the_marketing_phrasings_are_all_caught(phrasing):
    calls = [Call("get_vehicle_details", {"deposit": None, "unconfirmed": ["deposit"]})]
    findings = run_all([Msg("outbound", phrasing)], calls, State([], []), escalated=False)
    assert "absence_claimed_for_unconfirmed_figure" in [f.type for f in findings]


def test_a_confirmed_zero_deposit_is_not_a_finding():
    # An operator who genuinely charges no deposit must still be able to say so.
    calls = [Call("get_vehicle_details", {"deposit": "0", "unconfirmed": []})]
    findings = run_all(
        [Msg("outbound", "No deposit needed on this one.")],
        calls, State([], []), escalated=False,
    )
    assert "absence_claimed_for_unconfirmed_figure" not in [f.type for f in findings]


def test_stating_a_deposit_figure_no_tool_produced_is_still_caught():
    calls = [Call("get_vehicle_details", {"deposit": None, "unconfirmed": ["deposit"]})]
    findings = run_all(
        [Msg("outbound", "The deposit is AED 5000.")],
        calls, State([], []), escalated=False,
    )
    assert "unsupported_claim" in [f.type for f in findings]


def quote_for(engine, vehicle):
    """Put the unconfirmed vehicle into the engine's fleet and quote it."""
    engine._vehicles = tuple(
        vehicle if v.id == vehicle.id else v for v in engine.list_fleet()
    )
    engine._by_id[vehicle.id] = vehicle
    return engine.calculate_quote(
        vehicle_id=vehicle.id,
        pickup_at=dt(10, 11),
        return_at=dt(13, 11),
        skip_availability_check=True,
    )


# --- promising a figure that will never arrive ------------------------------
#
# Every line below was said by the agent in one live conversation on 23 Aug,
# after being asked four times for the no-deposit service fee. It never invented
# a number — it invented a *stage*: a point in the booking where the figure would
# appear. There is no such stage, and no number in the sentence for the
# unsupported-claim check to catch.


UNCONFIRMED_DEPOSIT = [Call("create_demo_quote", {"deposit": None, "unconfirmed": ["deposit"]})]


@pytest.mark.parametrize("said", [
    # Passive, no actor, no number — the earlier pattern missed this one, seen in
    # play on 23 Aug after the escalation route was already in.
    "Regarding the deposit, that figure is confirmed specifically for this car at the "
    "final booking stage, and I'll be able to give you the exact amount then.",
    "I'm unable to provide an estimate for the deposit as it's something that's only "
    "confirmed for your specific rental at the final stage.",
    "It involves a non-refundable service fee, which I can confirm for you once we "
    "move to the final booking stage.",
    "The no-deposit service fee is specific to your booking and is calculated by the "
    "system at the final stage.",
    "Would you like me to proceed to the reservation stage so we can lock in the final "
    "figures for you, including that no-deposit fee?",
    "The specific amount is calculated based on the car, the rental length, and your "
    "profile, which I can confirm for you once we set up the reservation.",
])
def test_promising_the_figure_appears_at_a_later_stage_is_caught(said):
    findings = run_all([Msg("outbound", said)], UNCONFIRMED_DEPOSIT, State([], []), escalated=False)
    assert "deferred_promise_for_unconfirmed_figure" in [f.type for f in findings]


@pytest.mark.parametrize("said", [
    "That fee is not something I can quote — let me check with the team and come "
    "straight back to you.",
    "A deposit applies; the amount is confirmed for that specific car before booking.",
    "I have asked a colleague and will come back to you with the figure.",
])
def test_asking_a_person_is_the_move_that_actually_exists(said):
    findings = run_all([Msg("outbound", said)], UNCONFIRMED_DEPOSIT, State([], []), escalated=False)
    assert "deferred_promise_for_unconfirmed_figure" not in [f.type for f in findings]


def test_an_operator_whose_system_really_does_calculate_it_is_unaffected():
    # Nothing unconfirmed in the result, so the phrasing is simply true.
    confirmed = [Call("create_demo_quote", {"deposit": "5000", "unconfirmed": []})]
    findings = run_all(
        [Msg("outbound", "The system will calculate the final figure at checkout.")],
        confirmed, State([], []), escalated=False,
    )
    assert "deferred_promise_for_unconfirmed_figure" not in [f.type for f in findings]


# --- escalating too early is its own failure --------------------------------
#
# Observed in play on 23 Aug: asked "Ferrari Roma. Is there a deposit" — the
# first mention — the agent escalated, told the customer a colleague was taking
# over, and stopped replying. Every customer asks that question. The honest
# answer is one it can give.


ESCALATED = Call("escalate_conversation", {"escalated": True, "reason": "unconfirmed_figure"},
                 {"reason": "unconfirmed_figure"})


def test_escalating_on_the_first_ask_is_a_finding():
    findings = run_all(
        [Msg("inbound", "Ferrari Roma. Is there a deposit", 1),
         Msg("outbound", "I've asked a colleague to confirm the exact figure.", 2)],
        [ESCALATED], State([], []), escalated=True,
    )
    assert "escalated_before_answering" in [f.type for f in findings]


def test_escalating_after_they_press_is_correct():
    findings = run_all(
        [Msg("inbound", "is there a deposit?", 1),
         Msg("outbound", "A deposit applies; the amount is confirmed for that car.", 2),
         Msg("inbound", "how much is the deposit though, roughly?", 3),
         Msg("outbound", "Let me check with the team.", 4)],
        [ESCALATED], State([], []), escalated=True,
    )
    assert "escalated_before_answering" not in [f.type for f in findings]


def test_an_accident_escalation_is_never_second_guessed():
    accident = Call("escalate_conversation", {"escalated": True, "reason": "accident"},
                    {"reason": "accident"})
    findings = run_all(
        [Msg("inbound", "I crashed the car", 1)], [accident], State([], []), escalated=True,
    )
    assert "escalated_before_answering" not in [f.type for f in findings]


# --- an answer is not a decision --------------------------------------------
#
# Observed in play on 23 Aug: the owner was asked "is there a deposit", replied
# "the deposit on the Roma is 5,000 AED, refundable", and the system answered
# "I couldn't read that as a yes or a no" — discarding the one thing only they
# could supply. A figure the operator has never published has no two sides to
# pick between, so it is a third kind of case: the reply is the value.


@pytest.fixture
def ctx_with_rules(engine):
    """A context stub carrying only what handover reads: the rules."""
    from types import SimpleNamespace
    return SimpleNamespace(engine=engine)


def test_a_figure_question_wants_an_answer_not_a_decision(ctx_with_rules):
    from rental_agent.services import handover
    assert handover.needs_an_answer(ctx_with_rules, "unconfirmed_figure") is True
    assert handover.needs_a_decision(ctx_with_rules, "unconfirmed_figure") is False
    # And the shapes that already existed are untouched.
    assert handover.needs_a_decision(ctx_with_rules, "fee_dispute") is True
    assert handover.needs_an_answer(ctx_with_rules, "accident") is False


def test_the_owner_is_not_shown_approve_and_decline_for_a_deposit(ctx_with_rules):
    from rental_agent.services import handover
    labels = [o["label"] for o in handover.decision_options(ctx_with_rules, "unconfirmed_figure")]
    assert "Approve" not in labels and "Decline" not in labels


@pytest.mark.parametrize("reply", [
    "the deposit on the Roma is 5,000 AED, refundable",
    "AED 5,000",
    "5000 for that one",
])
def test_the_owners_figure_is_taken_as_the_answer(ctx_with_rules, reply):
    from rental_agent.services import handover
    assert handover.outcome_of(
        ctx_with_rules, button_id=None, text=reply, reason="unconfirmed_figure"
    ) == "owner_answered"


def test_an_owner_stepping_out_is_still_understood(ctx_with_rules):
    from rental_agent.services import handover
    assert handover.outcome_of(
        ctx_with_rules, button_id=None, text="I'll call them", reason="unconfirmed_figure"
    ) == "owner_calling"
    assert handover.outcome_of(
        ctx_with_rules, button_id=None, text="handled", reason="unconfirmed_figure"
    ) == "owner_handled"


def test_a_decision_case_still_refuses_an_ambiguous_reply(ctx_with_rules):
    from rental_agent.services import handover
    assert handover.outcome_of(
        ctx_with_rules, button_id=None, text="maybe later", reason="fee_dispute"
    ) is None


def test_the_relay_carries_the_figure_verbatim():
    from types import SimpleNamespace
    from rental_agent.services import handover
    case = SimpleNamespace(decision="owner_answered", question="deposit on the Roma",
                           detail=None, decision_note="AED 5,000, refundable")
    directive = handover.relay_directive(case)
    assert "AED 5,000, refundable" in directive
    assert "exactly as written" in directive
    assert "carry on with the conversation" in directive


def test_a_figure_the_owner_supplied_is_not_reported_as_invented():
    from rental_agent.evaluation.checks import owner_authorised_numbers
    from decimal import Decimal
    authorised = owner_authorised_numbers(
        [{"decision": "owner_answered", "note": "the deposit is AED 5,000", "question": ""}]
    )
    assert Decimal("5000") in authorised


# --- it has to survive being written down -----------------------------------
#
# Found in play on 23 Aug, at the worst possible moment: the owner had supplied
# the deposit, the agent had relayed it, the customer said "book it" — and the
# insert failed because the quotes column would not accept a null deposit. On a
# real number that is silence, after the customer has already been told the
# figure and agreed to pay it.


def test_a_quote_with_no_confirmed_deposit_can_be_stored(session):
    from datetime import timedelta
    from decimal import Decimal
    from rental_agent.store.repositories import Quotes

    quotes = Quotes(session)
    stored = quotes.save(
        quote_id="DQ-901",
        vehicle_id="veh_01",
        payload={"quote_id": "DQ-901"},
        total_charge=Decimal("8816.85"),
        deposit=None,
        created_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + timedelta(hours=24),
    )
    session.flush()
    assert stored.deposit is None
    assert session.get(type(stored), "DQ-901").total_charge == Decimal("8816.85")


# --- a question about a crash is not a crash --------------------------------
#
# Observed in play on 23 Aug: "what happens if I crash it?" — asked by someone
# with a live booking, deciding what they were signing up for — was escalated as
# an Accident. The agent went silent and the owner received a case containing no
# incident. Everything needed to answer it is in the policy document.


ACCIDENT_ESCALATION = Call(
    "escalate_conversation", {"escalated": True, "reason": "accident"}, {"reason": "accident"}
)


@pytest.mark.parametrize("asked", [
    "what happens if I crash it?",
    "what if it breaks down in the desert?",
    "am I covered for scratches?",
    "who pays if I have an accident?",
    "what would I owe if someone hits me?",
])
def test_asking_what_would_happen_is_not_an_incident(asked):
    findings = run_all([Msg("inbound", asked)], [ACCIDENT_ESCALATION], State([], []),
                       escalated=True)
    assert "escalated_a_hypothetical" in [f.type for f in findings]


@pytest.mark.parametrize("said", [
    "I've just had an accident, someone hit me at a junction",
    "the car broke down on Sheikh Zayed Road",
    "I crashed it",
    "there's been an accident and the police are here",
])
def test_a_real_incident_is_never_second_guessed(said):
    findings = run_all([Msg("inbound", said)], [ACCIDENT_ESCALATION], State([], []),
                       escalated=True)
    assert "escalated_a_hypothetical" not in [f.type for f in findings]


def test_an_incident_described_as_a_question_is_still_an_incident():
    # "I crashed it, what happens now?" carries both signals. Reading it as a
    # question would tell the agent to answer from the policy document while
    # somebody is standing at the roadside.
    findings = run_all(
        [Msg("inbound", "I crashed it, what happens now?")],
        [ACCIDENT_ESCALATION], State([], []), escalated=True,
    )
    assert "escalated_a_hypothetical" not in [f.type for f in findings]
