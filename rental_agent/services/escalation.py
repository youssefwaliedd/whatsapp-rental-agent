"""Escalation.

Escalating is cheap; failing to escalate an accident is not. The reason is
matched against the configured trigger list, but an unrecognised reason still
escalates — it is recorded as `other` rather than refused. A tool that can
reject an escalation is a tool that will eventually reject the wrong one.

Which is why the one gate here downgrades rather than refuses. Asked "what
happens if I crash it?" the model escalated it as an accident on one run and
answered it correctly on the next, so the instruction not to is unreliable and
the cost is real: the agent stops replying, the owner gets a case with no
incident in it, and the customer sits behind it hearing "I'm still waiting to
hear back" to every message. When the customer's own words ask about an incident
rather than report one, the escalation is still recorded — nothing is refused,
and it is there to audit — but nobody is paged and the conversation keeps going,
because the answer is in the policy document.
"""

from __future__ import annotations

import difflib
from typing import Any

from ..context import ToolContext
from ..domain.enums import Stage
from ..domain.incident import INCIDENT_REASONS, asks_rather_than_reports

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


def _last_customer_message(ctx: ToolContext) -> str | None:
    """What the customer actually said, most recently."""
    if ctx.session is None or not ctx.conversation_id:
        return None
    inbound = [
        m for m in ctx.messages.for_conversation(ctx.conversation_id)
        if m.direction == "inbound"
    ]
    return inbound[-1].content if inbound else None


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

    # Their own words, not the model's reading of them.
    advisory = classified in INCIDENT_REASONS and asks_rather_than_reports(
        _last_customer_message(ctx)
    )

    escalation = ctx.escalations.create(
        conversation_id=ctx.conversation_id,
        customer_id=ctx.customer_id,
        reason=classified,
        detail=detail or reason,
        now=ctx.now(),
    )

    if advisory:
        # Recorded, not raised. The conversation is untouched, so the agent
        # answers the question and the customer is not stranded behind a case
        # that contains no incident.
        return {
            "escalated": False,
            "advisory": True,
            "escalation_id": escalation.id,
            "reason": classified,
            "guidance": (
                "This reads as a question about what would happen, or how something "
                "works — not a report that it has happened. Do not tell the customer "
                "a colleague is taking over. Answer it yourself from "
                "search_company_policy, which holds the excess, the police report "
                "requirement, what insurance excludes and what to do at the scene. A "
                "note has been recorded for the team. If they then tell you something "
                "HAS happened, escalate immediately."
            ),
        }

    conversation = ctx.conversations.get(ctx.conversation_id)
    if conversation is not None:
        conversation.escalated = True
        conversation.escalation_reason = classified

    state = ctx.load_state()
    if not state.escalated:
        # Only on the way in. Escalating twice must not overwrite this with
        # ESCALATED and strand the conversation there permanently.
        state.stage_before_escalation = state.stage
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
