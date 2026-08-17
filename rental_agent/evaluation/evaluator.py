"""Evaluate a stored conversation.

Reads the transcript, the tool-call audit log and the final state, runs the
deterministic checks, and records what it found.

Findings become `Mistake` rows — deliberately a separate table from
`evaluations`. An evaluation is a snapshot of one conversation; a mistake is a
durable lesson that outlives it, accumulates occurrences when it recurs, and can
later be retrieved into future conversations. That separation is what turns
"this conversation went badly" into "the agent has a habit worth correcting".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..context import ToolContext
from ..store.models import Evaluation as EvaluationRow
from ..store.models import Mistake as MistakeRow
from .checks import Finding, run_all


@dataclass
class EvaluationResult:
    conversation_id: str
    findings: list[Finding]
    message_count: int
    tool_call_count: int
    tool_failure_count: int
    question_count: int
    outcome: str | None

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def worst_severity(self) -> str | None:
        for severity in ("high", "medium", "low"):
            if any(f.severity == severity for f in self.findings):
                return severity
        return None


def evaluate_conversation(ctx: ToolContext, conversation_id: str) -> EvaluationResult:
    """Assess one conversation from its recorded events. Consults no model."""
    conversation = ctx.conversations.get(conversation_id)
    if conversation is None:
        raise KeyError(f"No conversation {conversation_id}")

    messages = ctx.messages.for_conversation(conversation_id)
    tool_calls = ctx.tool_calls.for_conversation(conversation_id)

    from ..domain.models import ConversationState

    state = (
        ConversationState.model_validate(conversation.state)
        if conversation.state
        else ConversationState(conversation_id=conversation_id, customer_id=conversation.customer_id)
    )

    findings = run_all(
        messages=messages,
        tool_calls=tool_calls,
        state=state,
        escalated=bool(conversation.escalated),
    )

    return EvaluationResult(
        conversation_id=conversation_id,
        findings=findings,
        message_count=len(messages),
        tool_call_count=len(tool_calls),
        tool_failure_count=sum(1 for c in tool_calls if c.status == "error"),
        question_count=sum(
            1 for m in messages if m.direction == "outbound" and "?" in (m.content or "")
        ),
        outcome=conversation.outcome,
    )


def record(ctx: ToolContext, result: EvaluationResult) -> EvaluationRow:
    """Persist an evaluation and promote its findings to durable mistakes."""
    now = ctx.now()
    session = ctx._require_session()

    row = EvaluationRow(
        conversation_id=result.conversation_id,
        strategy_version=None,
        outcome=result.outcome,
        message_count=result.message_count,
        tool_call_count=result.tool_call_count,
        tool_failure_count=result.tool_failure_count,
        question_count=result.question_count,
        findings=[f.__dict__ for f in result.findings],
        passed=result.passed,
        created_at=now,
    )
    session.add(row)

    for finding in result.findings:
        _promote(ctx, finding, result.conversation_id, now)

    session.flush()
    return row


def _promote(ctx: ToolContext, finding: Finding, conversation_id: str, now: Any) -> MistakeRow:
    """Fold a finding into the mistake ledger.

    The same mistake seen in a second conversation increments its count rather
    than creating a duplicate — a habit repeated ten times is one lesson worth
    correcting, not ten.

    The count tracks *distinct conversations*, so re-evaluating one cannot
    inflate it and two findings of the same type in a single conversation are
    one occurrence of the habit.
    """
    session = ctx._require_session()
    existing = session.scalar(
        select(MistakeRow).where(
            MistakeRow.type == finding.type,
            MistakeRow.status.in_(("detected", "corrected")),
        )
    )
    if existing is not None:
        seen = list(existing.conversation_ids or [])
        if conversation_id not in seen:
            seen.append(conversation_id)
            existing.conversation_ids = seen
            existing.occurrences = len(seen)
            existing.updated_at = now
        return existing

    mistake = MistakeRow(
        type=finding.type,
        severity=finding.severity,
        situation=finding.situation,
        bad_behavior=finding.bad_behavior,
        correct_behavior=finding.correct_behavior,
        evidence=finding.evidence,
        conversation_id=conversation_id,
        status="detected",
        occurrences=1,
        conversation_ids=[conversation_id],
        created_at=now,
        updated_at=now,
    )
    session.add(mistake)
    session.flush()
    return mistake


def evaluate_all(ctx: ToolContext) -> list[EvaluationResult]:
    """Evaluate every conversation that has had a reply."""
    from .replay import REPLAY_OUTCOME

    results = []
    for conversation in ctx.conversations.all():
        if conversation.outcome == REPLAY_OUTCOME:
            continue  # a replay artefact, not a real customer
        result = evaluate_conversation(ctx, conversation.conversation_id)
        if result.message_count:
            record(ctx, result)
            results.append(result)
    return results


def open_mistakes(ctx: ToolContext) -> list[MistakeRow]:
    """Detected mistakes not yet corrected, worst and most frequent first."""
    session = ctx._require_session()
    rows = list(session.scalars(select(MistakeRow).where(MistakeRow.status == "detected")))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(rows, key=lambda m: (order.get(m.severity, 3), -m.occurrences))
