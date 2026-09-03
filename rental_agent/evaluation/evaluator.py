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

import logging

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..context import ToolContext
from ..store.models import Conversation
from ..store.models import Evaluation as EvaluationRow
from ..store.models import Mistake as MistakeRow
from .checks import Finding, run_all

_log = logging.getLogger("rental_agent.evaluation")


@dataclass
class EvaluationResult:
    conversation_id: str
    findings: list[Finding]
    message_count: int
    tool_call_count: int
    tool_failure_count: int
    question_count: int
    #: The conversation's lifecycle marker — null while it is still open, and
    #: what `replay` stamps to keep its own scratch traffic out of the reports.
    outcome: str | None
    #: How the sale ended: booked, dropped or escalated. A different column from
    #: `outcome` on purpose — writing this one there would close the thread and
    #: greet a returning customer as a stranger.
    sales_outcome: str | None = None

    @property
    def lost(self) -> bool:
        """A conversation that reached a quote and went quiet. The interesting one."""
        return self.sales_outcome == "dropped"

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def worst_severity(self) -> str | None:
        for severity in ("high", "medium", "low"):
            if any(f.severity == severity for f in self.findings):
                return severity
        return None


def owner_decisions(ctx: ToolContext, conversation_id: str) -> list[dict[str, Any]]:
    """Decisions a person made on this conversation.

    Read as evidence rather than as behaviour: an owner who authorised something
    the rules do not permit has not made the agent wrong for saying so, and an
    evaluator that reported it would generate a lesson teaching the agent to
    overrule its own owner.
    """
    from ..store.models import Escalation

    rows = ctx.session.scalars(
        select(Escalation).where(
            Escalation.conversation_id == conversation_id,
            Escalation.decision.is_not(None),
        )
    ) if ctx.session is not None else []
    return [
        {"decision": row.decision, "note": row.decision_note, "question": row.question}
        for row in rows
    ]


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
        owner_decisions=owner_decisions(ctx, conversation_id),
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
        sales_outcome=conversation.sales_outcome,
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


def review_enabled() -> bool:
    """Whether the reading pass runs.

    Off unless a key exists. Everything else in the loop works without it, and a
    review that silently did not happen would be worse than one that plainly
    never ran.
    """
    import os

    return bool(os.getenv("ANTHROPIC_API_KEY")) and os.getenv(
        "RENTAL_AGENT_REVIEW", "1"
    ) not in ("0", "false", "no")


def review_and_record(ctx: ToolContext, conversation_id: str) -> list[Finding]:
    """Read one conversation for what the checks cannot see, and keep it.

    Runs after the deterministic pass, never instead of it. A failure here loses
    nothing: the findings that matter were produced without a model.
    """
    if not review_enabled():
        return []

    from ..agent.providers import build_client
    from .review import review_conversation

    try:
        client = build_client("anthropic")
    except Exception as exc:  # noqa: BLE001 - no reviewer is not an error
        _log.warning("no review model available: %s", exc)
        return []

    messages = ctx.messages.for_conversation(conversation_id)
    result = review_conversation(client, messages)
    if not result.ran:
        return []

    now = ctx.now()
    for finding in result.findings:
        _promote(ctx, finding, conversation_id, now)

    # What they asked and what they pushed back on. Not mistakes — an agent is
    # not wrong for being asked what the deposit is — so they are kept apart
    # from the ledger that decides what the agent is taught.
    from ..store.models import Observation

    for seen in result.observations:
        ctx.session.add(Observation(
            kind=seen["kind"],
            summary=seen["summary"],
            quote=seen["quote"],
            conversation_id=conversation_id,
            created_at=now,
        ))
    ctx.session.flush()

    if result.findings or result.observations:
        _log.info(
            "review of %s: %d finding(s), %d observation(s)",
            conversation_id, len(result.findings), len(result.observations),
        )
    return result.findings


def evaluate_finished(ctx: ToolContext, now: Any = None) -> list[str]:
    """Evaluate every conversation that has ended and has not been judged yet.

    Runs on inbound webhooks beside the other sweeps, so a conversation is
    assessed without anybody remembering to ask. This is the step that is safe
    to automate: it consults no model, costs nothing, and changes nothing a
    customer will ever see — it only writes down what happened. Proposing
    lessons from it, proving them, and putting them in front of customers stay
    deliberate acts.

    "Ended" means tagged *and* gone quiet for the same window that decides a
    dropped conversation. Evaluating at the moment of booking would judge a
    conversation that is still going — the customer usually keeps talking about
    delivery — and an escalated thread can still turn into a sale.

    Evaluated once, ever. Mistake occurrences count distinct conversations, so
    judging one twice would inflate the number that decides which habits are
    worth correcting.
    """
    from ..services.outcomes import _dropped_after
    from .replay import REPLAY_OUTCOME

    session = ctx._require_session()
    cutoff = (now or ctx.now()) - _dropped_after(ctx)

    judged = set(session.scalars(select(EvaluationRow.conversation_id)))
    ended = session.scalars(
        select(Conversation).where(
            Conversation.sales_outcome.is_not(None),
            Conversation.last_message_at.is_not(None),
            Conversation.last_message_at < cutoff,
        )
    )

    evaluated: list[str] = []
    for conversation in ended:
        if conversation.conversation_id in judged:
            continue
        if conversation.outcome == REPLAY_OUTCOME:
            continue
        result = evaluate_conversation(ctx, conversation.conversation_id)
        if not result.message_count:
            continue
        record(ctx, result)

        # Every failure becomes a permanent test, here rather than later. A case
        # captured now is one a future lesson has to keep passing; a case nobody
        # captured is a mistake the loop is free to make again.
        from .replay import capture_from_evaluation

        capture_from_evaluation(ctx, result)

        # And the reading pass, for what a fixed check cannot see. Its findings
        # join the same ledger at medium severity and go through the same gate
        # that refuses a lesson carrying a figure.
        try:
            review_and_record(ctx, conversation.conversation_id)
        except Exception:  # noqa: BLE001 - never lose the deterministic findings
            _log.exception("review pass failed on %s", conversation.conversation_id)

        evaluated.append(conversation.conversation_id)

    if evaluated:
        # And the proposal is kept current. Free — it reads what is already
        # recorded and calls no model — so the standing answer to "what has it
        # learned from these conversations?" is never older than the last one
        # that ended. Proving it and putting it in front of customers stay
        # deliberate, which is the whole distinction their brief is testing for.
        from . import strategies

        try:
            strategies.propose(ctx)
        except Exception:  # noqa: BLE001 - a bad lesson must not lose the evaluation
            _log.exception("could not refresh the candidate strategy")

    session.flush()
    return evaluated


def evaluate_by_outcome(
    ctx: ToolContext, outcomes: tuple[str, ...] = ("dropped", "escalated")
) -> list[EvaluationResult]:
    """Review the conversations that did not end in a sale.

    Their section 4 asks for a structured way to review *flagged or dropped*
    conversations, and until now the outcome was written and never read: every
    conversation was evaluated together and a lost sale looked exactly like a
    completed one in the report.

    Nothing is recorded here. This is for reading — the loop's own pass already
    records, and evaluating twice would inflate the occurrence counts that
    decide which habits are worth correcting.
    """
    from .replay import REPLAY_OUTCOME

    results = []
    for conversation in ctx.conversations.all():
        if conversation.outcome == REPLAY_OUTCOME:
            continue
        if conversation.sales_outcome not in outcomes:
            continue
        result = evaluate_conversation(ctx, conversation.conversation_id)
        if result.message_count:
            results.append(result)
    return results


def open_mistakes(ctx: ToolContext) -> list[MistakeRow]:
    """Detected mistakes not yet corrected, worst and most frequent first."""
    session = ctx._require_session()
    rows = list(session.scalars(select(MistakeRow).where(MistakeRow.status == "detected")))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(rows, key=lambda m: (order.get(m.severity, 3), -m.occurrences))
