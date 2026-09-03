"""Regression cases and the replay harness.

A conversation that once went wrong is kept as a test: the customer's turns, and
the finding that must not happen again. Before a candidate strategy can be
activated it is replayed against every case — the agent runs those same turns
under the candidate's lessons, and the case passes only if the mistake stays
gone.

Replays run in their own scratch conversation against a throwaway customer, so
they never touch real bookings or pollute the reports. The scratch conversations
are marked `replay` and excluded from evaluation runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ..agent.providers.errors import ProviderUnavailable
from ..context import ToolContext
from ..store.models import RegressionCase as CaseRow
from .evaluator import evaluate_conversation

#: Anything that means "we could not reach the model", rather than "the lessons
#: broke something". Belt and braces behind the adapter's own classification:
#: the diagnosis that found this scored two DNS failures as regressions, and a
#: verdict about the network recorded as a verdict about the lessons is the one
#: mistake this gate must not make.
try:  # pragma: no cover - httpx is a transitive dependency, not a declared one
    import httpx

    TRANSPORT_FAILURES: tuple[type[BaseException], ...] = (
        ProviderUnavailable, httpx.TransportError, ConnectionError, TimeoutError, OSError,
    )
except ImportError:  # pragma: no cover
    TRANSPORT_FAILURES = (ProviderUnavailable, ConnectionError, TimeoutError, OSError)

#: Marks a conversation as a replay artefact rather than a real customer.
REPLAY_OUTCOME = "replay"


@dataclass
class ReplayResult:
    case_id: int
    case_name: str
    forbidden_finding: str
    passed: bool
    #: The model could not be reached, so this case was never actually run.
    #: Not the same as failing it — see `replay_all`.
    inconclusive: bool = False
    findings: list[str] = field(default_factory=list)
    #: Serious findings this lesson caused that the case was not watching for.
    introduced: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReplaySummary:
    passed: int
    failed: int
    results: list[ReplayResult] = field(default_factory=list)
    #: True when the run stopped early because the model could not be reached.
    #: Nothing may be concluded from it — least of all a rejection.
    inconclusive: bool = False

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and not self.inconclusive


# --------------------------------------------------------------------------
# Capturing cases
# --------------------------------------------------------------------------


def capture_case(
    ctx: ToolContext,
    conversation_id: str,
    forbidden_finding: str,
    *,
    name: str | None = None,
    mistake_id: int | None = None,
) -> CaseRow | None:
    """Freeze a real conversation into a regression test.

    Only the customer's turns are kept — the agent's replies are what we are
    trying to change, so replaying them back would defeat the point.
    """
    messages = ctx.messages.for_conversation(conversation_id)
    turns = [m.content for m in messages if m.direction == "inbound" and m.content]
    if not turns:
        return None

    session = ctx._require_session()
    duplicate = session.scalar(
        select(CaseRow).where(
            CaseRow.source_conversation_id == conversation_id,
            CaseRow.forbidden_finding == forbidden_finding,
        )
    )
    if duplicate is not None:
        return duplicate

    case = CaseRow(
        name=name or f"{forbidden_finding} — {turns[0][:48]}",
        customer_turns=turns,
        forbidden_finding=forbidden_finding,
        source_conversation_id=conversation_id,
        source_mistake_id=mistake_id,
        created_at=ctx.now(),
    )
    session.add(case)
    session.flush()
    return case


def capture_from_evaluation(ctx: ToolContext, result: Any) -> list[CaseRow]:
    """Turn every finding in an evaluation into a regression case."""
    cases = []
    for finding in result.findings:
        case = capture_case(ctx, result.conversation_id, finding.type)
        if case is not None:
            cases.append(case)
    return cases


def cases(ctx: ToolContext) -> list[CaseRow]:
    session = ctx._require_session()
    return list(session.scalars(select(CaseRow).where(CaseRow.enabled.is_(True))))


# --------------------------------------------------------------------------
# Replaying
# --------------------------------------------------------------------------


def _scratch_context(ctx: ToolContext) -> ToolContext:
    """A throwaway customer and conversation for one replay."""
    scratch = ToolContext(
        session=ctx.session,
        now_fn=ctx.now_fn,
        reference_date=ctx.reference_date,
    )
    handle = f"+replay-{uuid.uuid4().hex[:10]}"
    customer, _ = scratch.customers.get_or_create(handle, scratch.now())
    conversation, _ = scratch.conversations.get_or_create(customer.customer_id, scratch.now())
    conversation.outcome = REPLAY_OUTCOME  # keeps it out of reports
    scratch.customer_id = customer.customer_id
    scratch.conversation_id = conversation.conversation_id
    scratch.session.flush()
    return scratch


def replay_case(ctx: ToolContext, case: CaseRow, agent: Any, lessons: list[str]) -> ReplayResult:
    """Run one case under a candidate's lessons and see if the mistake returns."""
    scratch = _scratch_context(ctx)
    previous = getattr(agent, "lessons_override", None)
    agent.lessons_override = lessons

    try:
        for turn in case.customer_turns:
            agent.respond(scratch, turn)
        result = evaluate_conversation(scratch, scratch.conversation_id)
        found = [f.type for f in result.findings]

        # Two ways to fail, not one. The obvious way is the old mistake coming
        # back. The other is a lesson that cures it and causes something worse —
        # checking only the named finding would wave that straight through, and
        # a candidate is meant to leave the agent better than it found it.
        introduced = sorted(
            {f.type for f in result.findings if f.severity == "high"}
            - {case.forbidden_finding}
        )
        return ReplayResult(
            case_id=case.id,
            case_name=case.name,
            forbidden_finding=case.forbidden_finding,
            passed=case.forbidden_finding not in found and not introduced,
            findings=found,
            introduced=introduced,
        )
    except TRANSPORT_FAILURES as exc:
        # The model was unreachable, so this case was never run. Scoring it as a
        # failure would reject a candidate for the free tier's quota running out
        # — a verdict about the weather, recorded permanently as a verdict about
        # the lessons.
        return ReplayResult(
            case_id=case.id,
            case_name=case.name,
            forbidden_finding=case.forbidden_finding,
            passed=False,
            inconclusive=True,
            error=f"{type(exc).__name__}: {str(exc)[:160]}",
        )
    except Exception as exc:  # noqa: BLE001 - a broken replay must not pass silently
        return ReplayResult(
            case_id=case.id,
            case_name=case.name,
            forbidden_finding=case.forbidden_finding,
            passed=False,
            error=f"{type(exc).__name__}: {str(exc)[:160]}",
        )
    finally:
        agent.lessons_override = previous


def replay_all(
    ctx: ToolContext, agent: Any, lessons: list[str], limit: int | None = None
) -> ReplaySummary:
    """Every stored case under a candidate's lessons.

    Stops at the first case the model could not be reached for. Carrying on
    would spend an hour turning one outage into twenty failures and reject a
    candidate that was never tested.
    """
    results: list[ReplayResult] = []
    chosen = cases(ctx)
    if limit:
        # Enough to test a hypothesis without spending the day's quota proving
        # it twenty times. A partial run can never activate anything.
        chosen = chosen[:limit]
    for case in chosen:
        result = replay_case(ctx, case, agent, lessons)
        results.append(result)
        if result.inconclusive:
            break
    return ReplaySummary(
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed and not r.inconclusive),
        results=results,
        inconclusive=any(r.inconclusive for r in results),
    )
