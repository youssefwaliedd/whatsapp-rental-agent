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
    candidate_version: str | None = None
    lessons: list[str] = field(default_factory=list)
    replay_passed: int = 0
    replay_failed: int = 0
    activated: bool = False
    reason: str | None = None
    results: list[EvaluationResult] = field(default_factory=list)


def run(ctx: ToolContext, agent: Any | None = None) -> CycleReport:
    """Run one learning cycle.

    Without an `agent` the cycle stops at proposing a candidate: activation
    requires replay, and replay requires something that can hold a conversation.
    Refusing to activate an untested candidate is the point of the gate.
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
    for result in results:
        report.new_cases += len(replay_mod.capture_from_evaluation(ctx, result))

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
    summary = replay_mod.replay_all(ctx, agent, candidate.lessons)
    report.replay_passed = summary.passed
    report.replay_failed = summary.failed

    # 5. Activate only on a clean replay.
    outcome = strategies.activate(ctx, candidate, summary.passed, summary.failed)
    report.activated = outcome.activated
    report.reason = outcome.reason or ("activated" if outcome.activated else None)
    return report
