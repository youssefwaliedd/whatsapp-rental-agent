"""Deterministic checks over what actually happened.

The specification is explicit that evaluation should read recorded events rather
than rely on another model's opinion, and these checks are that half. Every one
of them is computed from the stored transcript and the tool-call audit log — no
model is consulted, so a finding is a fact rather than a judgement, and the same
conversation always evaluates the same way.

The most important is `unsupported_claim`: it compares every figure the agent
put in front of a customer against every figure the tools actually returned. It
is the only automated check on the promise the whole system is built around.

Softer questions — was the tone right, was this good selling — are a model's job
and live in `judgement.py`. Nothing here needs one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

#: Money and percentages as a customer would read them: "AED 7,560", "7560.00",
#: "10%". Bare small integers are ignored — "2 or 3 options", "7 days" and
#: "999" are not claims about price.
_NUMBER = re.compile(r"(?:AED\s*)?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2}|\d{3,})")
_PERCENT = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%")

#: Phrases that assert a car cannot be had.
_UNAVAILABLE = re.compile(
    r"\b(not available|unavailable|isn't available|is booked|fully booked|already booked|"
    r"out on rent|no longer available|can't offer|cannot offer)\b",
    re.IGNORECASE,
)

#: Words a customer uses when something has gone seriously wrong.
_INCIDENT = re.compile(
    r"\b(accident|crash(?:ed)?|collision|injur(?:y|ed)|hospital|ambulance|police|"
    r"stolen|theft|broke down|breakdown|towed|smash(?:ed)?|hit (?:a|another))\b",
    re.IGNORECASE,
)

#: Numbers that are never a pricing claim, whatever the context.
_IGNORED_NUMBERS = {Decimal("999")}

#: Stripped before figures are extracted, because these are not claims about
#: money. A model year inside a vehicle name ("G63 — 2025") and an ISO date
#: would otherwise be reported as invented prices on almost every message, and
#: an evaluator that cries wolf on every car name gets ignored.
_NOT_A_PRICE = [
    re.compile(r"\d{4}-\d{2}-\d{2}(?:T[\d:+\-.]+)?"),   # ISO dates and timestamps
    re.compile(r"[—–-]\s*(?:19|20)\d{2}\b"),             # model year after a dash
    re.compile(r"\(\s*(?:19|20)\d{2}\s*\)"),             # model year in parentheses
]


@dataclass
class Finding:
    """One detected mistake, with the evidence that proves it."""

    type: str
    severity: str  # low | medium | high
    situation: str
    bad_behavior: str
    correct_behavior: str
    evidence: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _decimals(text: str) -> set[Decimal]:
    cleaned = text or ""
    for pattern in _NOT_A_PRICE:
        cleaned = pattern.sub(" ", cleaned)

    found: set[Decimal] = set()
    for raw in _NUMBER.findall(cleaned):
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:  # pragma: no cover - regex already constrains this
            continue
        if value not in _IGNORED_NUMBERS:
            found.add(value)
    return found


def _numbers_in(value: Any, into: set[Decimal]) -> None:
    """Every number anywhere in a nested tool result."""
    if isinstance(value, dict):
        for item in value.values():
            _numbers_in(item, into)
    elif isinstance(value, list):
        for item in value:
            _numbers_in(item, into)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        into.add(Decimal(str(value)))
    elif isinstance(value, str):
        try:
            into.add(Decimal(value))
        except InvalidOperation:
            into.update(_decimals(value))


def supported_numbers(tool_calls: Iterable[Any]) -> set[Decimal]:
    """Every figure the tools produced, plus the round percentages a discount
    ceiling licenses the agent to offer below."""
    supported: set[Decimal] = set()
    for call in tool_calls:
        _numbers_in(call.result or {}, supported)
        _numbers_in(call.arguments or {}, supported)

    # An agent permitted 10% may legitimately offer 5% — anything at or under a
    # ceiling the engine returned is supported, not invented.
    ceilings = [n for n in supported if n <= 20]
    for ceiling in ceilings:
        step = Decimal("1")
        value = step
        while value <= ceiling:
            supported.add(value)
            value += step
    return supported


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def has_usable_evidence(tool_calls) -> bool:
    """Whether the audit log actually recorded what the tools returned.

    Conversations logged before results were retained have empty results, and a
    check run against them would flag every correct figure as invented. An
    evaluator that cannot see must abstain rather than accuse — absence of
    evidence is not evidence of invention, and a wall of false accusations is
    how a learning loop teaches itself the wrong lesson.
    """
    successful = [c for c in tool_calls if getattr(c, "status", "ok") == "ok"]
    if not successful:
        return False
    return any(c.result for c in successful)


def owner_authorised_numbers(owner_decisions) -> set[Decimal]:
    """Figures a person explicitly signed off on.

    An owner who says *"give them 20% off"* has authorised a number the rules do
    not permit, and that is legitimate — it is their business. The evaluator
    must not then report the agent for repeating it, or the learning loop would
    generate a lesson teaching the agent to overrule its own owner.

    The authority is scoped to the conversation it was given in. Nothing here
    licenses the same figure for the next customer, which is the difference
    between an override and a policy change.
    """
    numbers: set[Decimal] = set()
    for decision in owner_decisions or []:
        # An answered case carries a figure the owner supplied because nobody
        # else had it — the deposit on a car the operator never published. That
        # is the *only* authority for that number, so the agent repeating it is
        # the correct outcome rather than an unsupported claim.
        if decision.get("decision") not in ("approved", "owner_answered"):
            continue
        for text in (decision.get("note") or "", decision.get("question") or ""):
            numbers |= _decimals(text)
            # `_decimals` deliberately ignores small bare numbers, because a
            # price claim is what it exists to catch. A discount an owner
            # authorised is exactly a small bare number, so percentages are
            # picked up separately.
            numbers |= {Decimal(p) for p in _PERCENT.findall(text)}
    return numbers


def check_unsupported_claims(messages, tool_calls, owner_decisions=None) -> list[Finding]:
    """A figure shown to the customer that no tool produced.

    This is the automated guard on the project's central promise. A miss here is
    the most serious thing the evaluator can find.

    Skipped entirely when the audit log holds no tool results to check against.
    """
    if not has_usable_evidence(tool_calls):
        return []

    supported = supported_numbers(tool_calls) | owner_authorised_numbers(owner_decisions)
    findings: list[Finding] = []

    for message in messages:
        if message.direction != "outbound":
            continue
        unsupported = sorted(_decimals(message.content) - supported)
        if not unsupported:
            continue
        findings.append(
            Finding(
                type="unsupported_claim",
                severity="high",
                situation="agent stated a figure to the customer",
                bad_behavior=(
                    f"quoted {', '.join(str(n) for n in unsupported)} — no tool call in "
                    f"this conversation returned that"
                ),
                correct_behavior=(
                    "every price, total, deposit and limit must come from a tool result; "
                    "call the tool and quote what it returns"
                ),
                evidence={
                    "message_id": message.id,
                    "unsupported": [str(n) for n in unsupported],
                    "excerpt": message.content[:200],
                },
            )
        )
    return findings


#: Ways of saying a charge does not exist. The numeric guard cannot catch these:
#: there is no figure in "no deposit needed" for it to find unsupported.
_ABSENCE = re.compile(
    r"\b(no|zero|nil|without (?:a |any )?|free of|waived?|no need for (?:a |any )?)\s*"
    r"(security\s+)?(deposit|deposits)\b"
    r"|\bdeposit[- ]free\b"
    r"|\bdeposit\s+(?:is|will be|has been)\s+(?:waived|zero|nil|free|nothing)\b"
    r"|\bnothing\s+(?:to pay|due)\s+(?:up\s?front|in advance|at all)\b",
    re.I,
)


def unconfirmed_figures(tool_calls: Iterable[Any]) -> set[str]:
    """Figures the tools reported as unconfirmed rather than as a value."""
    names: set[str] = set()
    for call in tool_calls:
        result = call.result or {}
        if isinstance(result, dict):
            listed = result.get("unconfirmed")
            if isinstance(listed, list):
                names.update(str(n) for n in listed)
    return names


def check_absence_claimed_for_unconfirmed(messages, tool_calls) -> list[Finding]:
    """Telling a customer there is no deposit, when no tool said so.

    The operator advertises "no deposit required (T&Cs apply)" while their terms
    require one subject to the vehicle. So this phrasing is not the agent being
    sloppy — it is the agent repeating the operator's own marketing as though it
    were the policy. A customer told this and then asked for thousands at
    handover is the worst outcome the system can produce, and it carries no
    number for the unsupported-claim check to catch.
    """
    if "deposit" not in unconfirmed_figures(tool_calls):
        return []

    findings: list[Finding] = []
    for message in messages:
        if message.direction != "outbound":
            continue
        match = _ABSENCE.search(message.content or "")
        if not match:
            continue
        findings.append(
            Finding(
                type="absence_claimed_for_unconfirmed_figure",
                severity="high",
                situation="the deposit for this vehicle is not confirmed",
                bad_behavior=f"told the customer {match.group(0).strip()!r}",
                correct_behavior=(
                    "an unconfirmed figure is not zero — say the deposit is confirmed for "
                    "that specific car before booking, and never that there is none"
                ),
                evidence={"message_id": message.id, "phrase": match.group(0).strip()},
            )
        )
    return findings


#: Promising that an unconfirmed figure will appear later. Observed in play, in
#: three flavours within one conversation: "I can confirm for you once we set up
#: the reservation", "calculated by the system at the final stage", "proceed to
#: the reservation stage so we can lock in the final figures". None of those
#: stages exist. There is no number in any of them for the figure check to catch.
_DEFERRED_PROMISE = re.compile(
    r"\b(?:i (?:can|will|'ll)|we (?:can|will|'ll)|let me)\s+"
    r"(?:\w+\s+){0,4}?(?:confirm|quote|give|share|provide|lock in|calculate|work out|"
    r"look into|check|find out)\b(?:(?!\bwith (?:the|my|a) (?:team|colleague|manager|owner)\b).){0,80}?"
    r"\b(?:later|once we|when we|at the (?:final|booking|reservation|last)|final stage|"
    r"final figures|next stage|that stage|at checkout|at the end)\b"
    r"|\bthe system (?:will |can )?(?:calculate|generate|work out|produce)s?\b"
    r"|\bcalculated by the system\b"
    # The milestone itself, whoever is said to reach it. "That figure is
    # confirmed at the final booking stage" names no actor and promises no
    # number, so the patterns above miss it — and there is no such stage.
    r"|\b(?:at|by|during|in|before|until) the (?:final |last )?"
    r"(?:booking|reservation|checkout|payment|final|last)\s*(?:booking\s*|payment\s*)?stage\b",
    re.I | re.S,
)


def check_deferred_promise_for_unconfirmed(messages, tool_calls) -> list[Finding]:
    """Promising a figure will appear at a stage that does not exist.

    When the agent cannot state a figure and has nowhere to send the question, it
    does not stop — it invents a future in which the figure arrives. That is the
    same fabrication as an invented price, except it carries no number, so the
    unsupported-claim check cannot see it, and the customer is left waiting for
    something nobody will ever produce.

    Escalating is the move that actually exists. This fires only where a tool has
    reported a figure as unconfirmed, so an operator whose system genuinely does
    calculate a fee at checkout is unaffected.
    """
    if not unconfirmed_figures(tool_calls):
        return []

    findings: list[Finding] = []
    for message in messages:
        if message.direction != "outbound":
            continue
        match = _DEFERRED_PROMISE.search(message.content or "")
        if not match:
            continue
        findings.append(
            Finding(
                type="deferred_promise_for_unconfirmed_figure",
                severity="high",
                situation="the customer asked for a figure the operator has not confirmed",
                bad_behavior=(
                    f"promised it would come later — {match.group(0).strip()[:90]!r} — "
                    "describing a stage that does not exist"
                ),
                correct_behavior=(
                    "escalate_conversation with reason 'unconfirmed_figure', and tell the "
                    "customer a colleague is checking — never that the number appears at a "
                    "later stage"
                ),
                evidence={"message_id": message.id, "phrase": match.group(0).strip()[:120]},
            )
        )
    return findings


#: Words a customer uses when asking about a figure the operator has not
#: confirmed. Used to count how many times they have actually asked.
_FIGURE_WORDS = re.compile(r"\b(deposit|service fee|per[- ]?km|extra km|excess|fee)\b", re.I)


def check_escalated_before_answering(messages, tool_calls) -> list[Finding]:
    """Handing a routine question to a person on the first ask.

    Nearly every customer asks about the deposit. The honest answer — it applies,
    and the amount is confirmed for their car — is one the agent can give, and it
    keeps the conversation going. Escalating there ends a sale over a question
    that was never a problem, and buries the owner in cases they cannot act on
    any better than the agent could.

    The right shape is: answer once, escalate when they press. So this fires only
    where the customer had asked a single time.
    """
    escalated_for_figure = any(
        "unconfirmed_figure" in str((call.arguments or {}).get("reason", ""))
        or (call.result or {}).get("reason") == "unconfirmed_figure"
        for call in tool_calls
        if call.tool_name == "escalate_conversation"
    )
    if not escalated_for_figure:
        return []

    asks = sum(
        1
        for m in messages
        if m.direction == "inbound" and _FIGURE_WORDS.search(m.content or "")
    )
    if asks > 1:
        return []

    return [
        Finding(
            type="escalated_before_answering",
            severity="medium",
            situation="the customer asked once about a figure the operator has not confirmed",
            bad_behavior=(
                f"escalated to a person after {asks} ask"
                f"{'' if asks == 1 else 's'}, ending the conversation"
            ),
            correct_behavior=(
                "say a deposit applies and the amount is confirmed for that car, carry on "
                "selling, and escalate only if they ask again or will not proceed without "
                "the number"
            ),
            evidence={"asks": asks},
        )
    ]


#: Asking what would happen. A customer deciding whether to book asks these
#: constantly, and the answer is in the policy document.
_HYPOTHETICAL = re.compile(
    r"\b(?:what|who|how much)\s+(?:\w+\s+){0,3}?(?:if|when|in case)\b"
    r"|\bwhat happens\b|\bwhat would\b|\bwould i (?:be|have|need|pay)\b"
    r"|\bam i (?:covered|liable|responsible)\b"
    r"|\bin case of\b|\bif i (?:crash|damage|scratch|break|lose|have an accident)\b",
    re.I,
)

#: Reporting something that has happened. Deliberately checked first: "I crashed
#: it, what happens now" is an incident, not a question.
_ACTUAL = re.compile(
    r"\b(?:i|we|someone|somebody)\s+(?:just\s+)?(?:have|has|had|'ve)?\s*"
    r"(?:crashed|hit|damaged|scratched|broke|broken|lost|stolen)\b"
    r"|\b(?:i|we)(?:'ve| have| just)\s+had an accident\b"
    r"|\bthere(?:'s| has| have)\s+(?:been\s+)?an? (?:accident|crash|incident)\b"
    r"|\bcar (?:is |has )?(?:broken down|been stolen|won'?t start)\b"
    r"|\bpolice (?:are|is|came|arrived)\b|\bi am (?:hurt|injured)\b",
    re.I,
)

#: Reasons where treating a question as the event does real damage: the agent
#: goes silent, and the owner gets a case with nothing to act on.
_INCIDENT_REASONS = frozenset(
    {"accident", "injury", "breakdown", "vehicle_theft_or_loss", "medical_emergency",
     "police_involvement"}
)


def check_escalated_a_hypothetical(messages, tool_calls) -> list[Finding]:
    """Treating "what happens if I crash it?" as a crash.

    Someone deciding whether to rent a supercar asks what an accident would cost
    them. It is one of the most common questions there is, the answer is in the
    policy document — excess, police report, what insurance excludes — and
    escalating it ends the conversation for a customer who was about to book,
    while handing a colleague a case containing no incident.
    """
    reasons = {
        (call.result or {}).get("reason") or (call.arguments or {}).get("reason")
        for call in tool_calls
        if call.tool_name == "escalate_conversation"
    }
    if not (reasons & _INCIDENT_REASONS):
        return []

    inbound = [m for m in messages if m.direction == "inbound"]
    if any(_ACTUAL.search(m.content or "") for m in inbound):
        return []

    asked = next((m for m in inbound if _HYPOTHETICAL.search(m.content or "")), None)
    if asked is None:
        return []

    return [
        Finding(
            type="escalated_a_hypothetical",
            severity="high",
            situation="the customer asked what would happen, not reported that it had",
            bad_behavior=f"escalated as an incident on {(asked.content or '')[:70]!r}",
            correct_behavior=(
                "answer it from search_company_policy — the excess, the police report "
                "requirement and the insurance exclusions are all there — and escalate "
                "only once something has actually happened"
            ),
            evidence={"message_id": asked.id, "reasons": sorted(r for r in reasons if r)},
        )
    ]


def check_repeated_questions(state) -> list[Finding]:
    """Asking for something the customer had already supplied.

    Not the same as asking twice: if the customer changed the subject without
    answering, asking again is right. Only a question about a value already in
    state is a mistake, and that is recorded at ask time.
    """
    redundant = list(getattr(state, "redundant_asks", []) or [])
    return [
        Finding(
            type="repeated_question",
            severity="medium",
            situation="the customer had already supplied this",
            bad_behavior=f"asked for '{slot}' again despite already knowing it",
            correct_behavior="read the state block and ask only for what is genuinely missing",
            evidence={"slot": slot, "times_asked": redundant.count(slot)},
        )
        for slot in sorted(set(redundant))
    ]


def check_unavailable_without_alternatives(messages, tool_calls) -> list[Finding]:
    """Telling a customer no without having anything to offer instead."""
    if any(call.tool_name == "find_alternatives" for call in tool_calls):
        return []

    for message in messages:
        if message.direction == "outbound" and _UNAVAILABLE.search(message.content or ""):
            return [
                Finding(
                    type="no_alternatives_offered",
                    severity="medium",
                    situation="the vehicle the customer asked for could not be supplied",
                    bad_behavior="said it was unavailable without calling find_alternatives",
                    correct_behavior=(
                        "call find_alternatives first and lead with what you can do, "
                        "not with the refusal"
                    ),
                    evidence={"message_id": message.id, "excerpt": message.content[:200]},
                )
            ]
    return []


def check_missed_escalation(messages, tool_calls, escalated: bool) -> list[Finding]:
    """A customer reported something serious and nobody handed over.

    The costliest failure in the system, so it is checked from the customer's own
    words rather than from whether the agent decided it was serious.

    But *reported* is doing real work there. "What happens if I crash it?"
    contains the same words as "I crashed it" and means the opposite, and
    matching on the word alone would report the agent for correctly answering a
    pre-booking question — teaching the learning loop to escalate every customer
    who asks what an accident would cost. So a message that is only hypothetical
    is not a missed escalation. Anything carrying an actual-incident signal is,
    including one that carries both.
    """
    if escalated or any(call.tool_name == "escalate_conversation" for call in tool_calls):
        return []

    for message in messages:
        if message.direction != "inbound":
            continue
        content = message.content or ""
        hit = _INCIDENT.search(content)
        if hit and _HYPOTHETICAL.search(content) and not _ACTUAL.search(content):
            continue
        if hit:
            return [
                Finding(
                    type="missed_escalation",
                    severity="high",
                    situation=f"customer said '{hit.group(0)}'",
                    bad_behavior="the conversation was never escalated to a human",
                    correct_behavior=(
                        "call escalate_conversation immediately, check the customer is "
                        "safe, and stop selling"
                    ),
                    evidence={"message_id": message.id, "trigger": hit.group(0)},
                )
            ]
    return []


def check_discount_without_authority(messages, tool_calls, owner_decisions=None) -> list[Finding]:
    """A percentage offered without asking what was permitted.

    An owner approving the case *is* the authority — a higher one than the rules
    file, since they can change the rules file. So an approved decision licenses
    the figure exactly as a `get_allowed_discount` call would.
    """
    if any(call.tool_name == "get_allowed_discount" for call in tool_calls):
        return []
    if any(d.get("decision") == "approved" for d in owner_decisions or []):
        return []

    for message in messages:
        if message.direction != "outbound":
            continue
        percentages = _PERCENT.findall(message.content or "")
        # VAT is quoted constantly and is not a discount.
        offered = [p for p in percentages if Decimal(p) != Decimal("5")]
        if offered:
            return [
                Finding(
                    type="unauthorised_discount",
                    severity="high",
                    situation="the customer pushed on price",
                    bad_behavior=f"offered {offered[0]}% without calling get_allowed_discount",
                    correct_behavior=(
                        "check the permitted ceiling first and never name a figure the "
                        "rules have not licensed"
                    ),
                    evidence={"message_id": message.id, "percentages": offered},
                )
            ]
    return []


def check_option_overload(messages) -> list[Finding]:
    """More than three cars in one message reads as a catalogue, not advice."""
    vehicle = re.compile(r"\*([A-Z][\w\-]+(?: [\w\-\.]+){0,3} — (?:19|20)\d{2})\*")
    for message in messages:
        if message.direction != "outbound":
            continue
        named = set(vehicle.findall(message.content or ""))
        if len(named) > 3:
            return [
                Finding(
                    type="too_many_options",
                    severity="low",
                    situation="presenting vehicles to the customer",
                    bad_behavior=f"listed {len(named)} cars in one message",
                    correct_behavior="offer two or three, and say why each one suits them",
                    evidence={"message_id": message.id, "vehicles": sorted(named)},
                )
            ]
    return []


def run_all(messages, tool_calls, state, escalated: bool, owner_decisions=None) -> list[Finding]:
    """Every deterministic check, most serious first."""
    findings = [
        *check_unsupported_claims(messages, tool_calls, owner_decisions),
        *check_absence_claimed_for_unconfirmed(messages, tool_calls),
        *check_deferred_promise_for_unconfirmed(messages, tool_calls),
        *check_escalated_before_answering(messages, tool_calls),
        *check_escalated_a_hypothetical(messages, tool_calls),
        *check_missed_escalation(messages, tool_calls, escalated),
        *check_discount_without_authority(messages, tool_calls, owner_decisions),
        *check_repeated_questions(state),
        *check_unavailable_without_alternatives(messages, tool_calls),
        *check_option_overload(messages),
    ]
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: order.get(f.severity, 3))
