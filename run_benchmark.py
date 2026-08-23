"""Run the agent through Delta's own conversations and compare.

    .venv/bin/python run_benchmark.py                 # every conversation
    .venv/bin/python run_benchmark.py 11              # just chat-11
    .venv/bin/python run_benchmark.py 11 --turns 6    # the first six exchanges
    .venv/bin/python run_benchmark.py --out report.md # also write it down

Read it as a comparison, not a scoreboard. Their salesperson is persuasive and
knows figures ours has never been given; ours is careful and will not invent one.
Where the two differ, the interesting question is which you would rather send to
a customer — and that is yours to answer, not the tool's.

The findings underneath each conversation are the part that *is* objective: an
invented figure, a missed escalation, a promised stage that does not exist.

Every model call costs a turn of quota, so start with one conversation and a
handful of turns before running the lot.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from rental_agent.agent.loop import build_agent
from rental_agent.context import ToolContext
from rental_agent.evaluation.benchmark import BenchmarkResult, run, transcripts
from rental_agent.store.db import create_db_engine, init_db

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

TZ = ZoneInfo("Asia/Dubai")
REFERENCE_DATE = date(2026, 9, 1)
FROZEN_NOW = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)

DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"
THEM, US = "\033[38;5;110m", "\033[38;5;114m"


def show(result: BenchmarkResult) -> None:
    print(f"\n{BOLD}══ {result.conversation} ══{OFF}")
    for index, turn in enumerate(result.turns, start=1):
        print(f"\n{DIM}── exchange {index} {'─' * 46}{OFF}")
        print(f"{BOLD}CUSTOMER {OFF} {turn.exchange.customer.strip()[:300]}")
        print(f"{THEM}DELTA    {OFF} {_block(turn.exchange.theirs)}")
        print(f"{US}OURS     {OFF} {_block(turn.ours) if not turn.error else turn.error}")

    print()
    if result.findings:
        print(f"  findings: {', '.join(result.findings)}")
    else:
        print("  findings: none")


def _block(text: str) -> str:
    """Indented to line up under the label, and trimmed — these get long."""
    lines = (text or "—").strip().splitlines()
    kept = "\n".join(lines[:8])[:600]
    return kept.replace("\n", "\n          ")


def markdown(results: list[BenchmarkResult]) -> str:
    out = ["# Benchmark against Delta's own conversations", ""]
    for result in results:
        out += [f"## {result.conversation}", ""]
        for index, turn in enumerate(result.turns, start=1):
            out += [
                f"**{index}. Customer:** {turn.exchange.customer.strip()}",
                "",
                f"> **Delta:** {turn.exchange.theirs.strip() or '—'}",
                "",
                f"> **Ours:** {turn.ours.strip() or turn.error or '—'}",
                "",
            ]
        out += [f"_Findings: {', '.join(result.findings) or 'none'}_", ""]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("which", nargs="*", help="conversation numbers, e.g. 11 12")
    parser.add_argument("--turns", type=int, default=None, help="exchanges per conversation")
    parser.add_argument("--out", type=Path, default=None, help="also write a markdown report")
    args = parser.parse_args()

    paths = transcripts()
    if args.which:
        wanted = {f"chat-{int(n):02d}" for n in args.which}
        paths = [p for p in paths if p.stem in wanted]
    if not paths:
        sys.exit("no matching conversations in reference/conversations")

    session_factory = init_db(create_db_engine())
    agent = build_agent()
    print(f"{DIM}  warming the model…{OFF}", end="", flush=True)
    took = agent.warm_up()
    print(f"{DIM} {took:.1f}s{OFF}\n" if took else f"{DIM} unavailable{OFF}\n")

    results: list[BenchmarkResult] = []
    with session_factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)
        for path in paths:
            result = run(ctx, agent, path, limit=args.turns)
            session.commit()
            show(result)
            results.append(result)

    if args.out:
        args.out.write_text(markdown(results), encoding="utf-8")
        print(f"\n  written to {args.out}")

    total = sum(len(r.turns) for r in results)
    flagged = sum(1 for r in results if r.findings)
    print(f"\n{BOLD}  {len(results)} conversations · {total} exchanges · "
          f"{flagged} with findings{OFF}\n")


if __name__ == "__main__":
    main()
