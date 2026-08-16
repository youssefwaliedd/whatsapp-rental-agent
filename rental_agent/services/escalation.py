"""Escalation.

Escalating is cheap; failing to escalate an accident is not. The reason is
matched against the configured trigger list, but an unrecognised reason still
escalates — it is recorded as `other` rather than refused. A tool that can
reject an escalation is a tool that will eventually reject the wrong one.
"""

from __future__ import annotations

import difflib
from typing import Any

from ..context import ToolContext
from ..domain.enums import Stage

#: Escalations that must reach a human immediately rather than at the next
#: staff check of the queue.
URGENT_REASONS = {
    "accident",
    "injury",
    "police_involvement",
    "vehicle_theft_or_loss",
    "breakdown",
    "medical_emergency",
}


def classify_reason(reason: str, triggers: list[str]) -> str:
    """Map a free-text reason onto the configured trigger vocabulary."""
    normalised = reason.strip().lower().replace(" ", "_")
    if normalised in triggers:
        return normalised
    close = difflib.get_close_matches(normalised, triggers, n=1, cutoff=0.75)
    return close[0] if close else "other"


def escalate_conversation(
    ctx: ToolContext, *, reason: str, detail: str | None = None
) -> dict[str, Any]:
    if not ctx.conversation_id:
        return {"error": "no_conversation", "message": "No conversation to escalate"}

    triggers = ctx.engine.rules.escalation_triggers
    classified = classify_reason(reason, triggers)
    urgent = classified in URGENT_REASONS

    escalation = ctx.escalations.create(
        conversation_id=ctx.conversation_id,
        customer_id=ctx.customer_id,
        reason=classified,
        detail=detail or reason,
        now=ctx.now(),
    )

    conversation = ctx.conversations.get(ctx.conversation_id)
    if conversation is not None:
        conversation.escalated = True
        conversation.escalation_reason = classified

    state = ctx.load_state()
    state.escalated = True
    state.escalation_reason = classified
    state.stage = Stage.ESCALATED
    ctx.save_state(state)

    return {
        "escalated": True,
        "escalation_id": escalation.id,
        "reason": classified,
        "raw_reason": reason,
        "urgent": urgent,
        "recognised_trigger": classified != "other",
        # Milestone 4 turns this into an actual WhatsApp message to staff.
        "staff_notified": False,
        "guidance": (
            "Tell the customer a colleague is taking over, and do not attempt "
            "to resolve it yourself. For an accident, remind them of safety "
            "first and a police report."
            if urgent
            else "Tell the customer a colleague will follow up shortly."
        ),
    }
