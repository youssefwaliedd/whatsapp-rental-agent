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

from ..domain.incident import ACTUAL, INCIDENT_REASONS, QUESTION

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
    re.compile(r"(?:ambulance|police|fire services|الإسعاف على|بالشرطة على|المدني على)\s+(?:997|998|999)\b", re.I),
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


def supported_numbers(tool_calls: Iterable[Any], monetary_only: bool = False) -> set[Decimal]:
    """Every figure the tools produced, plus the round percentages a discount
    ceiling licenses the agent to offer below."""
    tool_calls = list(tool_calls)
    supported: set[Decimal] = set()
    def monetary(value):
        if isinstance(value, list):
            for item in value:
                monetary(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    monetary(item)
                elif key in ("total_due_at_delivery", "amount_due") or re.search(r"(?:price|amount|total|subtotal|fee|deposit|excess|charge|refund|unit_price)$", key):
                    if item is not None and not isinstance(item, bool):
                        try:
                            supported.add(Decimal(str(item)))
                        except InvalidOperation:
                            pass
    for call in tool_calls:
        if (call.result or {}).get("error"):
            continue
        if monetary_only:
            monetary(call.result or {})
        else:
            _numbers_in(call.result or {}, supported)

    # An agent permitted 10% may legitimately offer 5% — anything at or under a
    # ceiling the engine returned is supported, not invented.
    ceilings = [] if monetary_only else [Decimal(str(call.result["max_percent"])) for call in tool_calls
                if call.tool_name == "get_allowed_discount"
                and (call.result or {}).get("max_percent") is not None]
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
    from ..agent.figures import budget_values, without_budget_echoes
    budgets = set()
    for message in messages:
        if message.direction == "inbound":
            budgets.update(budget_values(message.content))
        if message.direction != "outbound":
            continue
        unsupported = sorted(_decimals(without_budget_echoes(message.content, budgets)) - supported)
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
    # Asking for a human or an exact missing amount is a legitimate escalation.
    if any(m.direction == "inbound" and re.search(
        r"\b(?:colleague|human|manager|owner|exact|confirm.*charge)\b|زميل|موظف|بالضبط",
        m.content or "", re.I,
    ) for m in messages):
        return []
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
    if not (reasons & INCIDENT_REASONS):
        return []

    inbound = [m for m in messages if m.direction == "inbound"]
    if any(ACTUAL.search(m.content or "") for m in inbound):
        return []

    asked = next((m for m in inbound if QUESTION.search(m.content or "")), None)
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


#: Telling a customer a held car is theirs. No figure in it for the number check
#: to catch, and the most expensive sentence in the system to get wrong.
_CONFIRMED_IT = re.compile(
    r"\b(?:is|it'?s|you'?re|your booking is|all)\s+(?:now\s+)?"
    r"(?:booked|confirmed|reserved|secured|locked in)\b"
    r"|\bi(?:'ve| have)\s+(?:booked|confirmed|reserved|secured)\b"
    r"|\bbooking (?:is )?confirmed\b|\ball set\b|\byou'?re good to go\b",
    re.I,
)


def check_confirmed_a_hold(messages, tool_calls) -> list[Finding]:
    """Calling a hold a booking.

    Where the operator keeps availability somewhere the engine cannot read, a
    booking is a hold until a person who can see the fleet confirms it. Saying
    "you're all booked" before that sends someone to collect a car that may
    already be out — the worst outcome a rental business has, and the one thing
    this design exists to prevent.
    """
    held = [
        call
        for call in tool_calls
        if (call.result or {}).get("awaiting_confirmation")
        and (call.result or {}).get("status") == "held"
    ]
    if not held:
        return []

    confirmed_later = any(
        (call.result or {}).get("status") == "confirmed"
        for call in tool_calls
        if call.tool_name in {"create_demo_reservation", "get_active_reservation"}
    )
    if confirmed_later:
        return []

    findings: list[Finding] = []
    for message in messages:
        if message.direction != "outbound":
            continue
        match = _CONFIRMED_IT.search(message.content or "")
        if not match:
            continue
        findings.append(
            Finding(
                type="confirmed_a_hold",
                severity="high",
                situation="the car is held, not confirmed — a colleague is still checking",
                bad_behavior=f"told the customer it was done: {match.group(0).strip()!r}",
                correct_behavior=(
                    "say the request is awaiting confirmation and come back when it is "
                    "confirmed — never that it is booked, reserved or theirs, and never "
                    "that the car is being held, since nothing holds it off the market"
                ),
                evidence={"message_id": message.id, "phrase": match.group(0).strip()},
            )
        )
    return findings


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


#: Asked this many times with no answer, it is nagging rather than persistence.
NAG_LIMIT = 3


def check_nagging(state) -> list[Finding]:
    """Asking for the same thing over and over while they answer something else.

    Different from asking for what they already gave — that is
    check_repeated_questions and it is a memory failure. This one is a listening
    failure: the value genuinely is missing, and the customer genuinely is not
    providing it, because they are busy asking about the price.

    Seen against Delta's own conversations: the agent asked where to deliver the
    car in three consecutive replies while the customer asked twice whether the
    price was final. Their salesperson asked once and followed the customer.
    """
    asked = list(getattr(state, "asked_slots", []) or [])
    findings: list[Finding] = []
    for slot in sorted(set(asked)):
        # Still missing, and asked well past the point of a fair second try.
        if getattr(state, slot, None) is not None or asked.count(slot) < NAG_LIMIT:
            continue
        findings.append(
            Finding(
                type="nagging",
                severity="medium",
                situation="the customer kept talking about something else",
                bad_behavior=f"asked for '{slot}' {asked.count(slot)} times and never got it",
                correct_behavior=(
                    "ask twice at most, then answer what they are actually asking and "
                    "let them raise it — or say plainly why you cannot continue without it"
                ),
                evidence={"slot": slot, "times_asked": asked.count(slot)},
            )
        )
    return findings


def check_unavailable_without_alternatives(messages, tool_calls) -> list[Finding]:
    """Telling a customer no without having anything to offer instead."""
    if any(call.tool_name == "find_alternatives" for call in tool_calls):
        return []

    for message in messages:
        text = message.content or ""
        if re.search(r"nothing is booked|no booking|cancelled|canceled|cancellation|إلغاء", text, re.I):
            continue
        if message.direction == "outbound" and _UNAVAILABLE.search(text):
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
        if hit and QUESTION.search(content) and not ACTUAL.search(content):
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


#: Two replies are "the same answer" past this much overlap. Generous on
#: purpose: a salesperson who rephrases the same refusal is still refusing.
_SAME_REPLY = 0.75

#: Below this the message is too short to judge — "Of course." twice is not
#: stonewalling.
_ENOUGH_TO_JUDGE = 40


def _bag(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _overlap(first: str, second: str) -> float:
    a, b = _bag(first), _bag(second)
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def check_stonewalled(messages) -> list[Finding]:
    """The same answer given again while the customer asked for something else.

    Observed live, and no existing check could see it. The customer asked what
    the deposit was; the agent correctly asked a colleague and said so. Then it
    said the same thing to "scratch the deposit, that's another request", to "I
    want to book a Urus", and to "I want to book a BMW" — four times, while a
    sale was in progress.

    Every individual reply was true and polite, which is why nothing caught it.
    What was wrong was the sequence: the customer moved on three times and the
    agent did not.

    Only counts where the customer's own messages differ. A customer repeating
    themselves is a different situation, and one where repeating the answer is
    often the right thing to do.
    """
    exchanges: list[tuple[str, str]] = []
    pending: str | None = None
    for message in messages:
        text = (message.content or "").strip()
        if message.direction == "inbound":
            pending = text
        elif pending is not None and text:
            exchanges.append((pending, text))
            pending = None

    findings: list[Finding] = []
    for index in range(2, len(exchanges)):
        window = exchanges[index - 2 : index + 1]
        replies = [reply for _, reply in window]
        asks = [ask for ask, _ in window]
        if any(len(reply) < _ENOUGH_TO_JUDGE for reply in replies):
            continue
        # The agent said the same thing three times running...
        if not all(_overlap(replies[0], other) >= _SAME_REPLY for other in replies[1:]):
            continue
        # ...to three different things.
        if any(_overlap(asks[0], other) >= _SAME_REPLY for other in asks[1:]):
            continue

        findings.append(
            Finding(
                type="stonewalled",
                severity="high",
                situation="the customer moved on and the agent did not",
                bad_behavior=(
                    "gave substantially the same reply three times running while the "
                    f"customer asked about different things — last: {asks[-1][:70]!r}"
                ),
                correct_behavior=(
                    "answer what they actually asked. Waiting on a colleague for one "
                    "figure is not a reason to stop selling — say you are still waiting "
                    "on that number only if they ask about it again, and carry on with "
                    "everything else"
                ),
                evidence={"asked": asks[-1][:200], "replied": replies[-1][:200]},
            )
        )
        break  # one finding per conversation; it is one habit, not three

    return findings


def run_all(messages, tool_calls, state, escalated: bool, owner_decisions=None) -> list[Finding]:
    """Every deterministic check, most serious first."""
    findings = [Finding(
        type=item["type"], severity="high", situation="a proposed reply failed outbound validation",
        bad_behavior=item["reply"], correct_behavior=item["reason"], evidence=item,
    ) for item in getattr(state, "validation_findings", [])]
    findings += [
        *check_unsupported_claims(messages, tool_calls, owner_decisions),
        *check_absence_claimed_for_unconfirmed(messages, tool_calls),
        *check_confirmed_a_hold(messages, tool_calls),
        *check_deferred_promise_for_unconfirmed(messages, tool_calls),
        *check_escalated_before_answering(messages, tool_calls),
        *check_escalated_a_hypothetical(messages, tool_calls),
        *check_missed_escalation(messages, tool_calls, escalated),
        *check_discount_without_authority(messages, tool_calls, owner_decisions),
        *check_repeated_questions(state),
        *check_nagging(state),
        *check_unavailable_without_alternatives(messages, tool_calls),
        *check_option_overload(messages),
        *check_stonewalled(messages),
    ]
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: order.get(f.severity, 3))
