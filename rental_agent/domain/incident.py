"""Telling a report of an incident apart from a question about one.

"What happens if I crash it?" and "I crashed it" share their vocabulary and mean
opposite things. Getting that wrong in either direction is expensive: escalating
the question ends a sale, buries the owner in cases with nothing in them, and
leaves the customer stuck behind one — an escalated conversation stops answering,
so every message afterwards gets "I'm still waiting to hear back". Missing the
report is worse, because somebody is standing at a roadside.

So the rule lives here rather than in a prompt, and both the agent's gate and the
evaluator's check read the same patterns. Asked the same question twice on 23 Aug,
the model answered it correctly once and escalated it as an accident the other
time — which is what a instruction buys you, and why this is a gate instead.
"""

from __future__ import annotations

import re

#: Reasons where treating a question as the event does real damage.
INCIDENT_REASONS = frozenset({
    "accident", "injury", "breakdown", "vehicle_theft_or_loss",
    "medical_emergency", "police_involvement",
})

#: Asking about something rather than reporting it. Two shapes: what WOULD
#: happen, and how something WORKS. Both were escalated as live incidents.
QUESTION = re.compile(
    r"\b(?:what|who|how much)\s+(?:\w+\s+){0,3}?(?:if|when|in case)\b"
    r"|\bwhat happens\b|\bwhat would\b|\bwould i (?:be|have|need|pay)\b"
    r"|\bam i (?:covered|liable|responsible)\b"
    r"|\bin case of\b|\bif i (?:crash|damage|scratch|break|lose|have an accident)\b"
    r"|\bhow (?:do|would|can|should) (?:i|we|you)\b"
    r"|\bhow does .{0,30}\bwork\b"
    r"|\bwhat(?:'s| is| are) the (?:process|procedure|steps|rules?|policy)\b"
    r"|\bdo i (?:need|have) to\b|\bwho (?:do|should) i (?:call|contact|tell)\b"
    r"|\bis (?:a|the) police report (?:needed|required|mandatory)\b",
    re.I,
)

#: Reporting something that has happened. Always checked first — a message
#: carrying both signals is a report.
ACTUAL = re.compile(
    r"\b(?:i|we|someone|somebody)\s+(?:just\s+)?(?:have|has|had|'ve)?\s*"
    r"(?:crashed|hit|damaged|scratched|broke|broken|lost|stolen)\b"
    r"|\b(?:i|we)(?:'ve| have| just)\s+had an accident\b"
    r"|\bthere(?:'s| has| have)\s+(?:been\s+)?an? (?:accident|crash|incident)\b"
    r"|\bcar (?:is |has )?(?:broken down|been stolen|won'?t start)\b"
    r"|\bpolice (?:are|is|came|arrived)\b|\bi am (?:hurt|injured)\b",
    re.I,
)


def asks_rather_than_reports(text: str | None) -> bool:
    """True only when the message is a question and carries no report.

    Deliberately asymmetric. A message with both signals — "I crashed it, how do
    I do a police report?" — is a report, because the cost of being wrong that
    way round is somebody waiting at the scene of an accident.
    """
    content = text or ""
    if ACTUAL.search(content) or urgent_report(content):
        return False
    return bool(QUESTION.search(content))


def urgent_report(text: str) -> str | None:
    """Recognise clear roadside emergencies even when the model is offline."""
    # Evaluate clauses separately: a report followed by "who should I call?"
    # remains an emergency, while hypothetical and negated claims do not.
    for clause in re.split(r"[.!?؟\n]|\b(?:but|however)\b", text, flags=re.I):
        if re.search(r"\b(?:if|would|could|not|never|no)\b|(?:لو|إذا|مش|غير)\s", clause, re.I):
            continue
        if re.search(r"(?:someone|somebody|i|passenger)\s+(?:is |am |was )?(?:hurt|injured|bleeding)|حد مصاب|شخص مصاب", clause, re.I):
            return "injury"
        if re.search(r"(?:engine|car|vehicle).{0,40}(?:smok(?:e|ing)|fire)|(?:smok(?:e|ing)|fire).{0,40}(?:engine|car)|السيارة تحترق", clause, re.I):
            return "accident"
    return None
