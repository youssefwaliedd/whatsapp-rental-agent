"""Reading a conversation for the things a fixed check cannot see.

Twelve deterministic checks catch what is *wrong*: a figure no tool produced, a
booking called confirmed when it is not, a question asked twice. They are exact,
they are cheap, and between them they cannot tell a good sales conversation from
a bad one. An objection brushed aside, a customer talked past, a close never
attempted, the same question arriving for the fifth week running — none of it is
a rule violation, and all of it is why a lead was lost.

That is what this reads for. It is the operator's Stage 2 list: frequently asked
questions, customer objections, weak responses, and conversations where the
agent failed to close.

**Two things keep it from being the weak link.**

It is not the model reviewing itself. A reviewer that shares the agent's blind
spots agrees with the agent, so this runs on its own model — a stronger one,
configured separately, on a task with no latency requirement and one call per
finished conversation rather than one per turn.

And nothing it produces is trusted more than the deterministic checks. Its
findings carry `source="review"`, they are medium severity at most, and every
lesson derived from them goes through the same gate that refuses a figure or a
policy claim. A reviewer that decided the agent should have quoted AED 500 gets
the same refusal as anything else, because the gate does not care where a lesson
came from.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from .checks import Finding

_log = logging.getLogger("rental_agent.evaluation.review")

#: The model that reads conversations. Deliberately not the one that holds them:
#: a reviewer with the agent's blind spots will agree with the agent.
REVIEW_MODEL = os.getenv("RENTAL_AGENT_REVIEW_MODEL", "claude-sonnet-5")
REVIEW_MAX_TOKENS = int(os.getenv("RENTAL_AGENT_REVIEW_MAX_TOKENS", "2000"))

#: What it is allowed to report. A closed list, because an open one produces a
#: new category every run and nothing ever accumulates into a habit worth
#: correcting.
KINDS = {
    "weak_objection_handling": "an objection was answered thinly, or not at all",
    "no_close_attempted": "the customer was ready and was never asked",
    "question_left_unanswered": "they asked something and never got an answer",
    "tone_mismatch": "the register was wrong for the customer",
    "lost_the_thread": "the agent followed its own agenda past what they said",
}

#: Reported for their own sake rather than as mistakes — what customers keep
#: asking, and what they push back on. These become the operator's reading, not
#: the agent's lessons.
OBSERVATIONS = ("faq", "objection")

REVIEW_PROMPT = """You are reviewing one finished conversation between a car rental \
company's WhatsApp sales assistant and a customer.

You are NOT checking for factual errors. Separate deterministic checks already \
find invented prices, unconfirmed bookings and missed escalations, and they are \
better at it than you are. Do not report those.

You are reading for what makes a sales conversation good or bad:

- an objection answered thinly or ignored
- a customer who was ready to book and was never asked
- a question they asked that never got an answer
- a tone that did not suit them
- the agent following its own agenda past what the customer actually said

Also record, separately from any criticism:
- questions this customer asked that a rental company should have a ready answer to (faq)
- objections they raised (objection)

Rules you must follow:

1. Quote the customer or the agent. A finding with no quote is not a finding.
2. Never state or suggest a price, deposit, percentage, fee or policy. If your \
observation requires a number, describe the situation and omit the number.
3. If the conversation was handled well, return an empty list. Saying nothing is \
a valid and common answer.
4. Report at most four items. The most important ones.

Return JSON only, no prose:
{"findings": [{"kind": "<one of the kinds above, or faq, or objection>",
               "summary": "<one sentence>",
               "quote": "<what was actually said>",
               "better": "<what should have happened — behaviour, never a figure>"}]}"""


@dataclass
class ReviewResult:
    findings: list[Finding]
    observations: list[dict[str, str]]
    error: str | None = None

    @property
    def ran(self) -> bool:
        return self.error is None


def _transcript(messages: Any, limit: int = 60) -> str:
    lines = []
    for message in list(messages)[-limit:]:
        who = "CUSTOMER" if message.direction == "inbound" else "AGENT"
        body = " ".join((message.content or "").split())
        if body:
            lines.append(f"{who}: {body}")
    return "\n".join(lines)


def review_conversation(client: Any, messages: Any, *, model: str | None = None) -> ReviewResult:
    """Read one conversation. Returns nothing rather than raising on failure.

    A review that could not run must not lose the evaluation it was part of: the
    deterministic findings are the ones that matter, and they were produced
    without a model.
    """
    transcript = _transcript(messages)
    if not transcript:
        return ReviewResult(findings=[], observations=[])

    try:
        response = client.messages.create(
            model=model or REVIEW_MODEL,
            max_tokens=REVIEW_MAX_TOKENS,
            system=REVIEW_PROMPT,
            messages=[{"role": "user", "content": transcript}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
    except Exception as exc:  # noqa: BLE001 - never lose the deterministic findings
        _log.warning("conversation review did not run: %s", exc)
        return ReviewResult(findings=[], observations=[], error=str(exc)[:200])

    return _parse(text)


def _parse(text: str) -> ReviewResult:
    """Turn the model's answer into findings, discarding anything unusable."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return ReviewResult(findings=[], observations=[], error="no JSON in the reply")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return ReviewResult(findings=[], observations=[], error=f"unreadable JSON: {exc}")

    findings: list[Finding] = []
    observations: list[dict[str, str]] = []

    for item in payload.get("findings", [])[:4]:
        kind = str(item.get("kind", "")).strip()
        quote = str(item.get("quote", "")).strip()
        summary = str(item.get("summary", "")).strip()
        better = str(item.get("better", "")).strip()
        if not quote or not summary:
            # No evidence, no finding. The rule that separates this from a
            # reviewer inventing criticism it cannot point at.
            continue

        if kind in OBSERVATIONS:
            observations.append({"kind": kind, "summary": summary, "quote": quote[:300]})
            continue
        if kind not in KINDS:
            continue

        findings.append(
            Finding(
                type=kind,
                # Never high. A deterministic check proves what it found; this
                # is a judgement, and it must not outrank one.
                severity="medium",
                situation=KINDS[kind],
                bad_behavior=summary,
                correct_behavior=better or "handle it the way a good salesperson would",
                evidence={"quote": quote[:300], "source": "review", "model": REVIEW_MODEL},
            )
        )

    return ReviewResult(findings=findings, observations=observations)
