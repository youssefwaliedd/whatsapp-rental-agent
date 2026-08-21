"""Human in the loop — the case lifecycle.

The transport tests cover the WhatsApp mechanics of this. What is tested here is
the part that would still be wrong if the messaging were perfect: whether an
owner's authority is recorded with the right scope, whether an unanswered case
is noticed, and whether a decision that resolved one customer's problem can leak
into what the next customer is told.

That last one is the reason this module exists. A learning loop that absorbs
"fine, waive it this once" as a standing lesson has quietly rewritten the policy
its owner never agreed to change.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rental_agent.evaluation import checks
from rental_agent.services import handover
from rental_agent.tools.registry import execute_tool
from tests.conftest import FROZEN_NOW


def escalate(ctx, detail="the AED 400 late fee is unfair"):
    execute_tool(ctx, "escalate_conversation", {"reason": "fee_dispute", "detail": detail})
    ctx.session.flush()
    return ctx.escalations.latest_for_conversation(ctx.conversation_id)


@pytest.fixture
def case(booking_ctx):
    escalation = escalate(booking_ctx)
    return handover.open_case(booking_ctx, escalation, "fee dispute — AED 400 late fee")


# --------------------------------------------------------------------------
# Opening a case
# --------------------------------------------------------------------------


def test_opening_a_case_records_what_was_asked(booking_ctx, case):
    assert case.status == handover.AWAITING
    assert case.question == "fee dispute — AED 400 late fee"
    assert case.notified_at is not None


def test_a_case_code_is_stable_across_a_retry(booking_ctx, case):
    """Derived rather than random, so a retried notification quotes the same
    code and the owner is not handed two names for one case."""
    again = handover.case_code(case.id, case.conversation_id)
    assert again == case.case_code
    assert len(case.case_code) == 3


def test_the_question_survives_the_answer(booking_ctx, case):
    """Six months later, "approved" on its own means nothing."""
    handover.record_decision(booking_ctx, case, outcome="approved", note="just this once")
    assert case.question == "fee dispute — AED 400 late fee"
    assert case.decision_note == "just this once"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_a_reply_to_our_message_finds_its_case(booking_ctx, case):
    handover.record_notification(booking_ctx, case, "wamid.ASK1")
    found = handover.find_case(booking_ctx, reply_to_message_id="wamid.ASK1")
    assert found is not None and found.id == case.id


def test_a_quoted_case_code_finds_its_case(booking_ctx, case):
    found = handover.find_case(booking_ctx, text=f"ok approve {case.case_code}")
    assert found is not None and found.id == case.id


def test_a_button_finds_its_case(booking_ctx, case):
    found = handover.find_case(booking_ctx, button_id=f"approve:{case.case_code}")
    assert found is not None and found.id == case.id


def test_a_relayed_case_is_no_longer_findable(booking_ctx, case):
    """A closed case must not swallow the answer to a later one."""
    handover.record_decision(booking_ctx, case, outcome="approved")
    handover.mark_relayed(booking_ctx, case)
    assert handover.find_case(booking_ctx, text="approve") is None


# --------------------------------------------------------------------------
# Refusing to guess
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("approve", "approved"),
        ("yes", "approved"),
        ("go ahead", "approved"),
        ("no", "declined"),
        ("decline it", "declined"),
        ("i'll call them", "owner_calling"),
    ],
)
def test_an_unambiguous_reply_is_read(booking_ctx, reply, expected):
    assert handover.outcome_of(booking_ctx, button_id=None, text=reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "no problem, waive it",       # opens with "no" and means yes
        "depends how long they've rented from us",
        "hmm",
        "",
    ],
)
def test_an_ambiguous_reply_is_refused_rather_than_guessed(booking_ctx, reply):
    """Guessing here resolves a real customer's case wrongly. "No" and "no
    problem" are one word apart and mean opposite things."""
    assert handover.outcome_of(booking_ctx, button_id=None, text=reply) is None


def test_an_unknown_outcome_is_refused_at_the_boundary(booking_ctx, case):
    with pytest.raises(ValueError):
        handover.record_decision(booking_ctx, case, outcome="maybe")


# --------------------------------------------------------------------------
# The owner not answering
# --------------------------------------------------------------------------


def test_a_fresh_case_needs_nothing(booking_ctx, case):
    assert handover.needs_reminder(booking_ctx, case) is False
    assert handover.has_timed_out(booking_ctx, case) is False


def test_an_overdue_case_earns_one_reminder_not_a_stream(booking_ctx, case):
    later = FROZEN_NOW + timedelta(minutes=20)
    assert handover.needs_reminder(booking_ctx, case, later) is True

    handover.mark_reminded(booking_ctx, case)
    assert handover.needs_reminder(booking_ctx, case, later) is False


def test_a_timed_out_case_stays_answerable(booking_ctx, case):
    """A late decision is still worth having, so the timeout is a branch off
    waiting rather than a terminal state."""
    much_later = FROZEN_NOW + timedelta(hours=2)
    assert handover.has_timed_out(booking_ctx, case, much_later) is True

    handover.mark_timed_out(booking_ctx, case)
    assert handover.find_case(booking_ctx, text=case.case_code) is not None


def test_a_decided_case_is_never_chased(booking_ctx, case):
    handover.record_decision(booking_ctx, case, outcome="approved")
    much_later = FROZEN_NOW + timedelta(hours=2)
    assert handover.needs_reminder(booking_ctx, case, much_later) is False
    assert handover.has_timed_out(booking_ctx, case, much_later) is False


# --------------------------------------------------------------------------
# Relaying is a separate event from deciding
# --------------------------------------------------------------------------


def test_a_decision_nobody_delivered_has_not_resolved_anything(booking_ctx, case):
    handover.record_decision(booking_ctx, case, outcome="approved")
    assert case.status == handover.DECIDED
    assert case.relayed_at is None
    assert handover.decided_awaiting_relay(booking_ctx, booking_ctx.conversation_id) is not None


def test_relaying_closes_the_case_and_unblocks_the_conversation(booking_ctx, case):
    handover.record_decision(booking_ctx, case, outcome="approved")
    handover.mark_relayed(booking_ctx, case)

    assert case.status == handover.RELAYED
    assert case.relayed_at is not None
    assert handover.pending_for_conversation(booking_ctx, booking_ctx.conversation_id) is None

    conversation = booking_ctx.conversations.get(booking_ctx.conversation_id)
    assert conversation.escalated is False


# --------------------------------------------------------------------------
# The scope of an owner's authority
#
# The single most important property here. An owner may exceed the rules for one
# customer; what must never happen is that becoming what the next customer is
# quoted.
# --------------------------------------------------------------------------


def test_an_owner_approved_figure_is_not_reported_as_invented():
    """Without this the evaluator flags the agent for relaying its own owner's
    decision, and the learning loop generates a lesson teaching it to refuse."""
    decisions = [{"decision": "approved", "note": "give them 20% off", "question": "fee dispute"}]
    assert checks.owner_authorised_numbers(decisions)

    from tests.test_evaluation import Call, Msg

    findings = checks.check_unsupported_claims(
        [Msg("outbound", "I've had this approved — 20% off, just for you.")],
        [Call("calculate_quote", {"total": "5040.00"})],
        decisions,
    )
    assert findings == []


def test_an_owner_approval_is_authority_for_a_discount():
    decisions = [{"decision": "approved", "note": "20% is fine", "question": "discount request"}]
    assert checks.check_discount_without_authority(
        [__import__("tests.test_evaluation", fromlist=["Msg"]).Msg(
            "outbound", "I can do 20% on that."
        )],
        [],
        decisions,
    ) == []


def test_a_declined_decision_authorises_nothing():
    """Only an approval is authority. A declined case must not license the
    figure that was being argued about."""
    decisions = [{"decision": "declined", "note": "no, 20% is far too much", "question": "x"}]
    assert checks.owner_authorised_numbers(decisions) == set()


def test_authority_does_not_survive_into_another_conversation(booking_ctx, case):
    """The override lives on its own case. A second conversation reading the
    same checks sees no authorisation at all, which is what keeps one 'just this
    once' from becoming the price list."""
    handover.record_decision(booking_ctx, case, outcome="approved", note="20% this once")

    from rental_agent.evaluation.evaluator import owner_decisions

    assert owner_decisions(booking_ctx, booking_ctx.conversation_id)
    assert owner_decisions(booking_ctx, "conv_someone_else") == []


def test_resolving_a_case_gives_the_conversation_back_to_the_agent(booking_ctx, case):
    """Observed live: the case was answered and relayed, and the agent still
    told the customer a colleague would be in touch — about the question it had
    just answered. Clearing the conversation flag is not enough, because the
    agent reads ConversationState."""
    from rental_agent.domain.enums import Stage

    before = booking_ctx.load_state()
    assert before.escalated is True
    assert before.stage is Stage.ESCALATED

    handover.record_decision(booking_ctx, case, outcome="approved")
    handover.mark_relayed(booking_ctx, case)

    after = booking_ctx.load_state()
    assert after.escalated is False
    assert after.stage is not Stage.ESCALATED
    assert after.escalation_reason is None


def test_the_stage_before_escalating_is_the_one_restored(booking_ctx):
    """Dropping the customer back to 'new lead' would lose everything the
    conversation had established and start it re-asking."""
    from rental_agent.domain.enums import Stage

    state = booking_ctx.load_state()
    state.stage = Stage.QUOTED
    booking_ctx.save_state(state)

    escalation = escalate(booking_ctx, "i want a refund")
    opened = handover.open_case(booking_ctx, escalation, "refund request")
    handover.record_decision(booking_ctx, opened, outcome="declined")
    handover.mark_relayed(booking_ctx, opened)

    assert booking_ctx.load_state().stage is Stage.QUOTED


def test_escalating_twice_does_not_strand_the_conversation(booking_ctx, case):
    """A second escalation while one is open must not record ESCALATED as the
    stage to return to, or the conversation can never come back."""
    from rental_agent.domain.enums import Stage

    escalate(booking_ctx, "and now the car has broken down")
    state = booking_ctx.load_state()
    assert state.stage_before_escalation is not Stage.ESCALATED

    handover.record_decision(booking_ctx, case, outcome="approved")
    handover.mark_relayed(booking_ctx, case)
    assert booking_ctx.load_state().stage is not Stage.ESCALATED
