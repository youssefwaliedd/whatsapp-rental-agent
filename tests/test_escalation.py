"""Escalation.

Scenario 8: a customer reports an accident and must reach a human. Failing to
escalate is the most costly mistake in the whole system, so the bias throughout
is toward escalating.
"""

from __future__ import annotations

import pytest

from rental_agent.domain.enums import Stage
from rental_agent.services.escalation import classify_reason
from rental_agent.tools.registry import execute_tool


def test_an_accident_escalates_and_is_marked_urgent(booking_ctx):
    result = execute_tool(
        booking_ctx,
        "escalate_conversation",
        {"reason": "accident", "detail": "Customer hit a barrier on Sheikh Zayed Road"},
    )
    assert result["escalated"] is True
    assert result["urgent"] is True
    assert result["recognised_trigger"] is True
    assert "police report" in result["guidance"]


def test_escalation_stops_the_agent_selling(booking_ctx):
    execute_tool(booking_ctx, "escalate_conversation", {"reason": "accident"})
    state = booking_ctx.load_state()
    assert state.stage is Stage.ESCALATED
    assert state.escalated is True
    assert state.escalation_reason == "accident"


def test_escalation_is_recorded_for_staff(booking_ctx):
    execute_tool(booking_ctx, "escalate_conversation", {"reason": "vehicle_theft_or_loss"})
    open_items = booking_ctx.escalations.open_escalations()
    assert len(open_items) == 1
    assert open_items[0].reason == "vehicle_theft_or_loss"
    assert open_items[0].conversation_id == booking_ctx.conversation_id


def test_the_conversation_row_is_flagged(booking_ctx):
    execute_tool(booking_ctx, "escalate_conversation", {"reason": "legal_threat"})
    conversation = booking_ctx.conversations.get(booking_ctx.conversation_id)
    assert conversation.escalated is True
    assert conversation.escalation_reason == "legal_threat"


def test_an_unrecognised_reason_still_escalates(booking_ctx):
    """A tool that can reject an escalation will eventually reject the wrong one."""
    result = execute_tool(
        booking_ctx, "escalate_conversation", {"reason": "customer is extremely upset"}
    )
    assert result["escalated"] is True
    assert result["reason"] == "other"
    assert result["recognised_trigger"] is False
    assert result["raw_reason"] == "customer is extremely upset"


def test_staff_notification_is_honestly_reported_as_pending(booking_ctx):
    """No WhatsApp transport exists yet. The flag must not claim otherwise."""
    result = execute_tool(booking_ctx, "escalate_conversation", {"reason": "accident"})
    assert result["staff_notified"] is False


def test_a_repeated_escalation_does_not_duplicate_the_ticket(booking_ctx):
    args = {"reason": "breakdown", "detail": "engine warning light"}
    execute_tool(booking_ctx, "escalate_conversation", args)
    second = execute_tool(booking_ctx, "escalate_conversation", args)
    assert second["idempotent_replay"] is True
    assert len(booking_ctx.escalations.open_escalations()) == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("accident", "accident"),
        ("Accident", "accident"),
        ("police involvement", "police_involvement"),
        ("suspected fraud", "suspected_fraud"),
        ("explicit request for human", "explicit_request_for_human"),
        ("i want to speak to a manager", "other"),
    ],
)
def test_reasons_map_onto_the_configured_vocabulary(raw, expected, engine):
    assert classify_reason(raw, engine.rules.escalation_triggers) == expected


@pytest.mark.parametrize(
    "reason, urgent",
    [
        ("accident", True),
        ("injury", True),
        ("medical_emergency", True),
        ("discount_above_approval_threshold", False),
        ("abusive_language", False),
    ],
)
def test_urgency_separates_safety_from_commercial_issues(booking_ctx, reason, urgent):
    result = execute_tool(booking_ctx, "escalate_conversation", {"reason": reason})
    assert result["urgent"] is urgent
