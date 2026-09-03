"""Versioned strategies and the gate that activates them.

A strategy is a numbered set of behavioural lessons. It is created as a
*candidate*, replayed against every regression case, and becomes *active* only
if none of those cases regress. A candidate that fails is rejected with its
reason recorded, and the previously active version keeps serving customers.

That gate is the whole point. Without it "learning" is just an unreviewed edit
to the agent's instructions, and a correction for one problem is free to create
another one nobody notices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..context import ToolContext
from ..store.models import Mistake as MistakeRow
from ..store.models import Strategy as StrategyRow
from .corrections import Lesson, lessons_from, reject_unsafe_lesson

#: The starting point: no learned lessons, just the system prompt as written.
BASELINE_VERSION = "strategy_1.0"


@dataclass
class ActivationResult:
    version: str
    activated: bool
    passed: int
    failed: int
    reason: str | None = None


def _next_version(ctx: ToolContext) -> str:
    session = ctx._require_session()
    count = len(list(session.scalars(select(StrategyRow))))
    return f"strategy_1.{count + 1}"


def active_strategy(ctx: ToolContext) -> StrategyRow | None:
    session = ctx._require_session()
    return session.scalar(select(StrategyRow).where(StrategyRow.status == "active"))


def active_lessons(ctx: ToolContext) -> list[str]:
    """The lessons the agent should be running with right now.

    Read by the agent on every turn, so a newly activated strategy takes effect
    in the next conversation without a restart.
    """
    strategy = active_strategy(ctx)
    return list(strategy.lessons) if strategy else []


def ensure_baseline(ctx: ToolContext) -> StrategyRow:
    """The unlearned starting version, so every later one has a parent."""
    session = ctx._require_session()
    existing = session.scalar(
        select(StrategyRow).where(StrategyRow.version == BASELINE_VERSION)
    )
    if existing is not None:
        return existing

    now = ctx.now()
    baseline = StrategyRow(
        version=BASELINE_VERSION,
        status="active",
        lessons=[],
        derived_from=[],
        created_at=now,
        activated_at=now,
    )
    session.add(baseline)
    session.flush()
    return baseline


def propose(ctx: ToolContext, mistakes: list[MistakeRow] | None = None) -> StrategyRow | None:
    """Build a candidate strategy from the mistakes on record.

    Returns None when there is nothing new to learn — a loop that proposes a
    version every time it runs teaches nothing and buries the versions that
    matter.
    """
    from .evaluator import open_mistakes

    session = ctx._require_session()
    ensure_baseline(ctx)
    current = active_strategy(ctx)
    mistakes = mistakes if mistakes is not None else open_mistakes(ctx)
    if not mistakes:
        return None

    lessons: list[Lesson] = lessons_from(mistakes)
    texts = [lesson.text for lesson in lessons]
    for text in texts:
        reject_unsafe_lesson(text)  # belt and braces before anything is stored

    existing = list(current.lessons) if current else []
    merged = existing + [t for t in texts if t not in existing]
    if merged == existing:
        return None  # nothing new

    # A candidate already waiting with exactly these lessons is the answer, not
    # a reason to mint another one. Otherwise every run of the loop stacks up an
    # identical version and buries the one somebody was going to read — which is
    # what "nothing new to learn" is supposed to prevent, and only prevented it
    # against the *active* strategy.
    pending = session.scalars(
        select(StrategyRow).where(StrategyRow.status == "candidate").order_by(StrategyRow.id)
    )
    for waiting in pending:
        if list(waiting.lessons) == merged:
            return waiting

    now = ctx.now()
    candidate = StrategyRow(
        version=_next_version(ctx),
        status="candidate",
        lessons=merged,
        derived_from=[m.id for m in mistakes if m.type in {l.mistake_type for l in lessons}],
        parent_version=current.version if current else None,
        created_at=now,
    )
    session.add(candidate)
    session.flush()
    return candidate


def record_replay(
    ctx: ToolContext, candidate: StrategyRow, passed: int, failed: int
) -> str | None:
    """Write what the replay found onto the candidate, and reject it if it failed.

    Separate from activation so a run can prove a candidate without promoting
    it. A regression is disqualifying whether or not anybody is about to
    promote anything, so the rejection happens here.
    """
    session = ctx._require_session()
    candidate.replay_passed = passed
    candidate.replay_failed = failed
    if failed:
        candidate.status = "rejected"
        candidate.rejection_reason = f"{failed} regression case(s) failed on replay"
    session.flush()
    return candidate.rejection_reason if failed else None


def promote(ctx: ToolContext, version: str) -> ActivationResult:
    """Put a proven candidate in front of customers, deliberately.

    The last step is a person's, and it is separate on purpose. A clean replay
    says the candidate breaks nothing that used to work; it does not say the
    lessons are ones this operator wants their salesperson taught. Only somebody
    who has read them can say that, which is the difference between a loop that
    is reviewable and one that merely reports afterwards.
    """
    session = ctx._require_session()
    candidate = session.scalar(select(StrategyRow).where(StrategyRow.version == version))
    if candidate is None:
        return ActivationResult(version, False, 0, 0, f"no strategy called {version}")
    if candidate.status == "active":
        return ActivationResult(version, False, 0, 0, "already active")
    if candidate.status != "candidate":
        return ActivationResult(
            version, False, candidate.replay_passed, candidate.replay_failed,
            f"{version} is {candidate.status}"
            + (f" — {candidate.rejection_reason}" if candidate.rejection_reason else ""),
        )
    if not candidate.replay_passed and not candidate.replay_failed:
        return ActivationResult(
            version, False, 0, 0,
            "not replayed yet — nothing has checked whether it breaks what already works",
        )
    return activate(ctx, candidate, candidate.replay_passed, candidate.replay_failed)


def activate(ctx: ToolContext, candidate: StrategyRow, passed: int, failed: int) -> ActivationResult:
    """Promote a candidate — but only if nothing regressed."""
    session = ctx._require_session()
    now = ctx.now()

    rejected = record_replay(ctx, candidate, passed, failed)
    if rejected:
        return ActivationResult(candidate.version, False, passed, failed, rejected)

    for previous in session.scalars(select(StrategyRow).where(StrategyRow.status == "active")):
        previous.status = "superseded"

    candidate.status = "active"
    candidate.activated_at = now

    # The mistakes this version corrects are now addressed rather than open.
    for mistake in session.scalars(
        select(MistakeRow).where(MistakeRow.id.in_(candidate.derived_from or [-1]))
    ):
        mistake.status = "active"
        mistake.agent_version = candidate.version
        mistake.updated_at = now

    session.flush()
    return ActivationResult(candidate.version, True, passed, failed)


def history(ctx: ToolContext) -> list[StrategyRow]:
    session = ctx._require_session()
    return list(session.scalars(select(StrategyRow).order_by(StrategyRow.id)))
