"""The learning cycle, end to end.

    evaluate every conversation
      → capture the failures as regression cases
      → generate safe corrections
      → propose a candidate strategy
      → replay every case against it
      → activate only if nothing regressed

Each step is separately tested; this is the orchestration that runs them in
order and reports what happened. It is deliberately explicit rather than
automatic — a learning loop that fires unattended is one nobody is auditing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..context import ToolContext
from . import replay as replay_mod
from . import strategies
from .evaluator import EvaluationResult, evaluate_all, open_mistakes


@dataclass
class CycleReport:
    evaluated: int = 0
    clean: int = 0
    findings: int = 0
    new_cases: int = 0
    #: Every case now guarding the agent, not just the ones this run added.
    total_cases: int = 0
    candidate_version: str | None = None
    lessons: list[str] = field(default_factory=list)
    replay_passed: int = 0
    replay_failed: int = 0
    replay_results: list[Any] = field(default_factory=list)
    activated: bool = False
    reason: str | None = None
    results: list[EvaluationResult] = field(default_factory=list)


def run(
    ctx: ToolContext, agent: Any | None = None, *, activate: bool = True,
    limit: int | None = None,
) -> CycleReport:
    """Run one learning cycle.

    Without an `agent` the cycle stops at proposing a candidate: activation
    requires replay, and replay requires something that can hold a conversation.
    Refusing to activate an untested candidate is the point of the gate.

    With `activate=False` it does the expensive part — the replay — and stops
    before promoting anything, leaving a proven candidate for a person to read
    and promote with `strategies.promote`. That is the difference between a loop
    that reports what it changed and one somebody actually reviews.
    """
    report = CycleReport()
    strategies.ensure_baseline(ctx)

    # 1. Evaluate what actually happened.
    results = evaluate_all(ctx)
    report.results = results
    report.evaluated = len(results)
    report.clean = sum(1 for r in results if r.passed)
    report.findings = sum(len(r.findings) for r in results)

    # 2. Keep each failure as a test.
    #
    #    Counted by identity rather than by call: capture is idempotent, so a
    #    second run over the same conversations returns the same rows and
    #    "30 new cases" every time would be a lie the report told on itself.
    before = {case.id for case in replay_mod.cases(ctx)}
    captured: set[int] = set()
    for result in results:
        captured |= {c.id for c in replay_mod.capture_from_evaluation(ctx, result)}
    report.new_cases = len(captured - before)
    report.total_cases = len(replay_mod.cases(ctx))

    # 3. Propose a correction.
    mistakes = open_mistakes(ctx)
    candidate = strategies.propose(ctx, mistakes) if mistakes else None
    if candidate is None:
        report.reason = "nothing new to learn"
        return report

    report.candidate_version = candidate.version
    report.lessons = list(candidate.lessons)

    if agent is None:
        report.reason = "candidate proposed but not activated — replay needs an agent"
        return report

    # 4. Prove it does not break anything already working.
    summary = replay_mod.replay_all(ctx, agent, candidate.lessons, limit=limit)
    report.replay_passed = summary.passed
    report.replay_failed = summary.failed
    report.replay_results = list(summary.results)

    if summary.inconclusive:
        # The model went away mid-run. The candidate is untested, which is not
        # the same as untrustworthy, so it is left exactly as it was.
        report.reason = (
            f"replay could not finish — the model was unreachable after "
            f"{summary.passed} case(s). Nothing was concluded and nothing was rejected."
        )
        return report

    if limit:
        # A sample proves nothing about the whole set, so it must not be able to
        # settle the candidate either way.
        report.reason = (
            f"sampled {summary.passed + summary.failed} case(s) — diagnosis only, "
            "nothing recorded against the candidate"
        )
        return report

    if not activate:
        # 5a. Proven, and left for a person. A regression still disqualifies it
        #     here — that is not a judgement call anybody needs to make.
        rejected = strategies.record_replay(
            ctx, candidate, summary.passed, summary.failed, summary.results
        )
        report.reason = rejected or "replayed clean — awaiting review"
        return report

    # 5. Activate only on a clean replay.
    outcome = strategies.activate(
        ctx, candidate, summary.passed, summary.failed, summary.results
    )
    report.activated = outcome.activated
    report.reason = outcome.reason or ("activated" if outcome.activated else None)
    return report
