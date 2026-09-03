"""Learn from the conversations that already happened.

    .venv/bin/python run_learning.py              # what it would learn. Free.
    .venv/bin/python run_learning.py --replay     # prove it breaks nothing. Costs quota.
    .venv/bin/python run_learning.py --promote strategy_1.3
    .venv/bin/python run_learning.py --lost
    .venv/bin/python run_learning.py --status

Three commands because there are three decisions, and only one of them is a
machine's.

**Read** evaluates every real conversation against the checks, keeps each
failure as a regression case, and proposes a candidate set of lessons. It calls
no model and costs nothing, so run it as often as you like.

**Replay** puts every regression case back through the agent under the candidate
lessons and reports whether the old mistakes stay gone. This is the expensive
step — one model call per turn per case — and it is the one that earns the word
"verified". Gemini's free tier is the right tier for it: latency does not matter
to a batch job, and it is the model actually serving customers.

**Promote** is yours. A clean replay says the candidate breaks nothing that used
to work. It does not say these are lessons this operator wants their salesperson
taught, and no amount of automation can say that.

Nothing here fires on a schedule. A learning loop that runs unattended is one
nobody is auditing, and the whole claim being made to the operator is that this
one is auditable.
"""

from __future__ import annotations

import argparse
import logging
import sys

from rental_agent.context import ToolContext
from rental_agent.evaluation import cycle, strategies
from rental_agent.evaluation.evaluator import evaluate_by_outcome, open_mistakes
from rental_agent.store.db import create_db_engine, database_url, init_db

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GOOD, WARN, BAD = "\033[38;5;114m", "\033[38;5;179m", "\033[38;5;167m"


def rule(title: str) -> None:
    print(f"\n{BOLD}══ {title} {'═' * max(0, 58 - len(title))}{OFF}")


def show_status(ctx: ToolContext) -> None:
    rule("strategies")
    history = strategies.history(ctx)
    if not history:
        print("  none yet — nothing has run.")
    for row in history:
        mark = {"active": GOOD, "rejected": BAD, "candidate": WARN}.get(row.status, DIM)
        print(f"  {mark}{row.version:<16}{row.status:<11}{OFF}"
              f"{len(row.lessons)} lesson(s)   "
              f"replay {row.replay_passed}/{row.replay_passed + row.replay_failed}")
        if row.rejection_reason:
            print(f"    {BAD}{row.rejection_reason}{OFF}")
        for failure in (row.replay_detail or [])[:8]:
            caused = failure.get("introduced") or []
            why = (
                f"caused {', '.join(caused)}" if caused
                else f"repeated {failure['must_not_happen']}" if failure["must_not_happen"] in (failure.get("found") or [])
                else failure.get("error") or "unclear"
            )
            print(f"      {DIM}✗ {failure['case'][:56]} — {why}{OFF}")

    mistakes = open_mistakes(ctx)
    rule(f"open mistakes ({len(mistakes)})")
    for mistake in mistakes:
        print(f"  [{mistake.severity}] {mistake.type}  ×{mistake.occurrences}")
        print(f"    {DIM}was:{OFF}    {mistake.bad_behavior[:88]}")
        print(f"    {DIM}should:{OFF} {mistake.correct_behavior[:88]}")


def show_lost(ctx: ToolContext) -> None:
    """The conversations that did not end in a sale, and what went wrong in each.

    Their section 4 asks for exactly this. Reading only — nothing is recorded,
    so it can be run as often as you like without moving the counts that decide
    which habits are worth correcting.
    """
    results = evaluate_by_outcome(ctx)
    dropped = [r for r in results if r.sales_outcome == "dropped"]
    escalated = [r for r in results if r.sales_outcome == "escalated"]

    rule(f"lost or handed over ({len(dropped)} dropped · {len(escalated)} escalated)")
    if not results:
        print("  none — no conversation has been quoted and gone quiet.")
        return

    for result in results:
        mark = BAD if result.lost else WARN
        print(f"\n  {mark}{result.sales_outcome:<10}{OFF}{result.conversation_id}"
              f"   {result.message_count} messages")
        if not result.findings:
            print(f"    {DIM}nothing the checks can see — worth reading yourself{OFF}")
        for finding in result.findings:
            print(f"    [{finding.severity}] {finding.type}")
            print(f"      {DIM}{finding.bad_behavior[:86]}{OFF}")


def show_report(report: cycle.CycleReport, *, replayed: bool) -> None:
    rule("what happened in the conversations")
    print(f"  {report.evaluated} evaluated · {report.clean} clean · "
          f"{report.findings} finding(s)")
    print(f"  {report.new_cases} new regression case(s) kept · "
          f"{report.total_cases} now guarding the agent")

    if not report.candidate_version:
        print(f"\n  {DIM}{report.reason}{OFF}")
        return

    rule(f"candidate {report.candidate_version}")
    for lesson in report.lessons:
        print(f"  · {lesson}")

    if not replayed:
        print(f"\n  {WARN}Not replayed.{OFF} Nothing has checked whether these break "
              f"anything that already works.")
        print(f"  {DIM}Run again with --replay when you can spare the quota.{OFF}")
        return

    rule("replay")
    total = report.replay_passed + report.replay_failed
    colour = GOOD if not report.replay_failed else BAD
    print(f"  {colour}{report.replay_passed}/{total} regression case(s) still pass{OFF}")

    # A rejection nobody can read is a dead end: the run that would explain it
    # costs an hour to repeat, so it has to explain itself the first time.
    for result in [r for r in report.replay_results if not r.passed]:
        print(f"\n  {BAD}✗ {result.case_name[:70]}{OFF}")
        if result.error:
            print(f"      {DIM}{result.error}{OFF}")
            continue
        if result.forbidden_finding in result.findings:
            print(f"      {DIM}the old mistake came back: {result.forbidden_finding}{OFF}")
        introduced = list(getattr(result, "introduced", []))
        if introduced:
            print(f"      {DIM}new problem the lessons caused: {', '.join(introduced)}{OFF}")

    print(f"\n  {report.reason}")
    if not report.replay_failed:
        print(f"\n  Read the lessons above. If you want them serving customers:")
        print(f"    {BOLD}.venv/bin/python run_learning.py "
              f"--promote {report.candidate_version}{OFF}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true",
                        help="replay every regression case against the candidate (uses the model)")
    parser.add_argument("--promote", metavar="VERSION",
                        help="put a replayed candidate in front of customers")
    parser.add_argument("--cases", type=int, metavar="N",
                        help="replay only the first N cases — diagnosis, settles nothing")
    parser.add_argument("--lost", action="store_true",
                        help="review the conversations that did not end in a sale")
    parser.add_argument("--status", action="store_true",
                        help="what is active, what is pending, what is still open")
    args = parser.parse_args()

    session_factory = init_db(create_db_engine())
    print(f"{DIM}database: {database_url()}{OFF}")

    with session_factory() as session:
        ctx = ToolContext(session=session)

        if args.lost:
            show_lost(ctx)
            return 0

        if args.status:
            show_status(ctx)
            return 0

        if args.promote:
            outcome = strategies.promote(ctx, args.promote)
            session.commit()
            if outcome.activated:
                print(f"\n  {GOOD}{outcome.version} is now serving customers.{OFF}")
                print(f"  {DIM}It takes effect on the next message — no restart.{OFF}")
                return 0
            print(f"\n  {BAD}Not promoted:{OFF} {outcome.reason}")
            return 1

        agent = None
        if args.replay:
            from rental_agent.agent.loop import build_agent

            agent = build_agent()

        report = cycle.run(ctx, agent, activate=False, limit=args.cases)
        session.commit()
        show_report(report, replayed=bool(agent))
        return 0


if __name__ == "__main__":
    sys.exit(main())
