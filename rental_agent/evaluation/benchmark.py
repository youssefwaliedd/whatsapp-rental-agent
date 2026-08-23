"""Answering the same questions a real salesperson already answered.

Delta sent thirteen of their best conversations. This runs the agent through the
customer's half of one and puts the two replies side by side: what their
salesperson said, and what ours says to the same words.

It is not a pass/fail test, and it must not pretend to be one. Different is not
worse — ours may be more honest and less persuasive, or simply phrased another
way, and which of those is better is a judgement for a person reading them. What
*can* be judged automatically is whether ours invented a figure, missed an
escalation, promised a stage that does not exist, or confirmed something it never
checked. Those come back as findings.

The value is measuring a change instead of guessing at it. Today a prompt change
is verified by typing a few messages into the simulator and reading the reply,
which tests whatever the tester happened to type. This tests thirteen real
conversations, the same way, every time.

    .venv/bin/python run_benchmark.py                # every conversation
    .venv/bin/python run_benchmark.py 11 --turns 6   # one, first six exchanges
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..context import ToolContext
from .evaluator import evaluate_conversation
from .replay import _scratch_context

#: `[07/15/2026 13:29:16] Customer: text`
TURN = re.compile(r"^\[(?P<when>[^\]]+)\]\s+(?P<who>Customer|Agent(?: \d+)?|Bot):\s?(?P<text>.*)$")

CORPUS = Path(__file__).resolve().parents[2] / "reference" / "conversations"

#: Attachments and read receipts carry no question to answer.
EMPTY = re.compile(r"^\s*(\[(photo|document|video|link|phone|email)\]\s*)*$", re.I)


@dataclass
class Exchange:
    """One thing the customer said, and what the operator said back."""

    customer: str
    theirs: str


@dataclass
class TurnResult:
    exchange: Exchange
    ours: str
    error: str | None = None


@dataclass
class BenchmarkResult:
    conversation: str
    turns: list[TurnResult] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


def parse(path: Path) -> list[Exchange]:
    """The conversation as alternating sides.

    Consecutive messages from the same side are joined, because a salesperson
    sending three lines in a row is one reply, and answering each separately
    would be answering a conversation nobody had.
    """
    blocks: list[tuple[str, list[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = TURN.match(line)
        if match:
            side = "customer" if match.group("who") == "Customer" else "them"
            text = match.group("text")
            if blocks and blocks[-1][0] == side:
                blocks[-1][1].append(text)
            else:
                blocks.append((side, [text]))
        elif blocks:
            blocks[-1][1].append(line)

    joined = [(side, "\n".join(parts).strip()) for side, parts in blocks]

    exchanges: list[Exchange] = []
    for index, (side, text) in enumerate(joined):
        if side != "customer":
            continue
        # A customer who sends a photo and then says "looks great" made one
        # turn, but the placeholder is not something the agent can answer.
        # Their side keeps its markers: sending photographs is how they sell.
        text = "\n".join(l for l in text.splitlines() if not EMPTY.match(l)).strip()
        if not text:
            continue
        reply = ""
        if index + 1 < len(joined) and joined[index + 1][0] == "them":
            reply = joined[index + 1][1]
        exchanges.append(Exchange(customer=text, theirs=reply))
    return exchanges


def transcripts() -> list[Path]:
    return sorted(CORPUS.glob("chat-*.txt"))


def run(
    ctx: ToolContext, agent: Any, path: Path, *, limit: int | None = None
) -> BenchmarkResult:
    """Put the customer's half of one conversation to the agent.

    Runs in a scratch conversation marked as a replay, so a benchmark never
    reaches a real customer's history or shows up in the reports.
    """
    exchanges = parse(path)[: limit or None]
    scratch = _scratch_context(ctx)
    result = BenchmarkResult(conversation=path.stem)

    for exchange in exchanges:
        try:
            turn = agent.respond(scratch, exchange.customer)
            result.turns.append(TurnResult(exchange=exchange, ours=turn.reply))
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the run
            result.turns.append(
                TurnResult(exchange=exchange, ours="", error=f"{type(exc).__name__}: {exc}"[:160])
            )

    try:
        evaluation = evaluate_conversation(scratch, scratch.conversation_id)
        result.findings = [f.type for f in evaluation.findings]
    except Exception as exc:  # noqa: BLE001 - the transcript is still worth reading
        result.findings = [f"evaluation failed: {type(exc).__name__}"]
    return result
