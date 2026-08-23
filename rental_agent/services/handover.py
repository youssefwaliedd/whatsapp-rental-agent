"""The case lifecycle after the agent hands a customer to a person.

Escalating is the easy half and already worked: the agent recognises it is out
of its depth and stops. This module is the other half — asking the owner a
question they can actually answer, routing their answer back to the one customer
it belongs to, and recording it in a way that resolves that case without
quietly becoming policy.

Three properties hold everything else up:

**A decision is a fact, not a suggestion.** The owner's answer is written to the
case as a decision plus whatever they typed alongside it. The model is told what
was decided; it never gets to work out what the owner probably meant.

**Authority is real but bounded in scope.** An owner may waive a fee or exceed
the discount ceiling — it is their business. What must not happen is that
becoming the rule: the override lives on this case, and the next customer is
quoted the standard policy. `excluded_from_learning` keeps it out of the
strategy loop, so one "fine, waive it this once" cannot be generalised.

**A decision nobody relayed has resolved nothing.** `relayed_at` is separate
from `decided_at` for that reason — an owner tapping Approve is not the same
event as the customer being told.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..context import ToolContext
from ..domain.enums import Stage
from ..store.models import Escalation

#: A case the owner has been asked about and has not yet answered.
AWAITING = "awaiting_decision"
#: Answered, but the customer has not been told yet.
DECIDED = "decided"
#: Answered and relayed. The case is closed.
RELAYED = "relayed"
#: Overdue. Deliberately still answerable — a late decision is worth having, so
#: this is a branch off AWAITING rather than a terminal state.
TIMED_OUT = "timed_out"

OPEN_STATUSES = ("open", AWAITING, TIMED_OUT, DECIDED)

#: Outcomes an owner may return. Anything else is refused rather than guessed at.
OUTCOMES = ("approved", "declined", "owner_calling", "owner_handled", "owner_answered")


def _config(ctx: ToolContext) -> dict[str, Any]:
    return ctx.engine.rules.human_in_the_loop


def needs_a_decision(ctx: ToolContext, reason: str) -> bool:
    """Whether this escalation has answers to choose between.

    A fee dispute does: the owner picks one and the customer is told. A crash
    does not — there is nothing to approve, only someone to take over. Getting
    this wrong produces "⚠️ Accident — [Approve] [Decline]", which is the same
    failure as reacting to one with a thumbs-up.
    """
    return reason in _config(ctx).get("decision_reasons", [])


def needs_an_answer(ctx: ToolContext, reason: str) -> bool:
    """Whether this escalation wants a value rather than a choice.

    A figure the operator has never published — the deposit on a given car, the
    fee for their no-deposit option — has no two sides to pick between. The owner
    simply knows it and nobody else does. Treating that as a decision puts
    Approve and Decline in front of "what is the deposit?", and then throws away
    the owner's actual answer for not being a yes or a no.
    """
    return reason in _config(ctx).get("answer_reasons", [])


def decision_options(ctx: ToolContext, reason: str | None = None) -> list[dict[str, str]]:
    """The buttons put in front of the owner, which depend on what is being asked.

    `reason` is optional so an existing case can be re-asked without knowing it;
    passing None gives the decision buttons, which is the older behaviour.
    """
    block = "owner_decision"
    if reason is not None and needs_an_answer(ctx, reason):
        block = "owner_answer"
    elif reason is not None and not needs_a_decision(ctx, reason):
        block = "owner_handover"
    return _config(ctx).get(block, {}).get("options", [])


def answer_prompt(ctx: ToolContext) -> str:
    """What the owner is asked when the case wants a value."""
    return _config(ctx).get("owner_answer", {}).get(
        "ask", "*Reply with the figure* and I'll pass it straight on."
    )


def customer_message(ctx: ToolContext, key: str, default: str = "") -> str:
    return _config(ctx).get("customer_messages", {}).get(key, default)


# --------------------------------------------------------------------------
# Opening a case
# --------------------------------------------------------------------------


def case_code(escalation_id: int, conversation_id: str) -> str:
    """A short code the owner can quote when they type instead of replying.

    Derived rather than random so it is stable across a retry, and short enough
    that a busy person will actually include it.
    """
    digest = hashlib.sha1(f"{conversation_id}:{escalation_id}".encode()).hexdigest()
    return digest[:3].upper()


def open_case(ctx: ToolContext, escalation: Escalation, question: str) -> Escalation:
    """Turn a recorded escalation into a question waiting on an answer."""
    escalation.question = question
    escalation.case_code = case_code(escalation.id, escalation.conversation_id)
    escalation.status = AWAITING
    escalation.notified_at = ctx.now()
    ctx.session.flush()
    return escalation


def record_notification(ctx: ToolContext, escalation: Escalation, message_id: str) -> None:
    """Remember which of our messages the owner will be replying to."""
    escalation.notification_message_id = message_id or None
    ctx.session.flush()


# --------------------------------------------------------------------------
# Finding the case an owner is answering
# --------------------------------------------------------------------------


def open_cases(ctx: ToolContext) -> list[Escalation]:
    """Every case waiting on an owner decision, newest first."""
    return _open_cases(ctx)


def _open_cases(ctx: ToolContext) -> list[Escalation]:
    return list(
        ctx.session.scalars(
            select(Escalation)
            .where(Escalation.status.in_((AWAITING, TIMED_OUT)))
            .order_by(Escalation.created_at.desc())
        )
    )


def find_case(
    ctx: ToolContext,
    *,
    reply_to_message_id: str | None = None,
    text: str | None = None,
    button_id: str | None = None,
) -> Escalation | None:
    """Which case this owner reply belongs to, or None if it is unclear.

    Three routes, most precise first:

    1. **A button** — the case code is encoded in the id we generated, so this
       is exact.
    2. **A swipe-to-reply** — WhatsApp hands us the quoted message id, which we
       stored when we sent the question.
    3. **A quoted case code in free text** — the fallback for an owner who
       starts a new message.

    If none of them match and exactly one case is open, that is it. With two
    open and no route, this returns None rather than guessing: resolving the
    wrong customer's case is worse than asking the owner which one they meant.
    """
    cases = _open_cases(ctx)
    if not cases:
        return None

    if button_id:
        _, _, code = button_id.partition(":")
        code = code.split(":")[0]
        for case in cases:
            if case.case_code and case.case_code == code:
                return case

    if reply_to_message_id:
        for case in cases:
            if case.notification_message_id == reply_to_message_id:
                return case

    if text:
        upper = text.upper()
        for case in cases:
            if case.case_code and case.case_code in upper:
                return case

    return cases[0] if len(cases) == 1 else None


def outcome_of(
    ctx: ToolContext, *, button_id: str | None, text: str | None, reason: str | None = None
) -> str | None:
    """The decision an owner reply carries, or None if it is not decidable.

    A button is unambiguous. Free text is only accepted when it is unambiguous
    too — "no" and "no problem" mean opposite things, so a reply that could be
    either is treated as undecidable and the owner is asked again. Guessing here
    resolves a real customer's case wrongly.
    """
    if button_id:
        chosen = button_id.split(":")[0]
        config = _config(ctx)
        for block in ("owner_decision", "owner_handover", "owner_answer"):
            for option in config.get(block, {}).get("options", []):
                if option["id"] == chosen:
                    return option.get("outcome")

    words = (text or "").strip().lower()
    if not words:
        return None

    if reason is not None and needs_an_answer(ctx, reason):
        # The reply *is* the answer. Only the two ways of stepping out of the
        # case are read as anything else — everything else is the figure, and
        # refusing it because it is not a yes or a no discards the one thing
        # nobody but the owner could supply.
        for name, options in (
            ("owner_handled", ("handled", "done", "sorted", "dealt with", "taken care of")),
            ("owner_calling", ("call", "i'll call", "ill call", "phone them", "ring them")),
        ):
            if any(re.search(rf"\b{re.escape(o)}\b", words) for o in options):
                return name
        return "owner_answered"

    affirmative = ("approve", "approved", "yes", "yep", "yeah", "ok", "okay",
                   "go ahead", "do it", "fine", "agreed", "sure",
                   # Idiomatic agreement that opens with a negative word. Listed
                   # so it registers as a *conflicting* signal rather than being
                   # read as the decline it superficially resembles.
                   "no problem", "no worries", "not a problem", "no issue")
    negative = ("decline", "declined", "no", "nope", "reject", "refuse", "deny")
    calling = ("call", "i'll call", "ill call", "phone them", "ring them")
    handled = ("handled", "done", "sorted", "dealt with", "taken care of", "resolved")

    def mentions(options: tuple[str, ...]) -> bool:
        return any(re.search(rf"\b{re.escape(option)}\b", words) for option in options)

    hits = [
        name
        for name, options in (
            ("owner_handled", handled),
            ("owner_calling", calling),
            ("approved", affirmative),
            ("declined", negative),
        )
        if mentions(options)
    ]
    # More than one signal means the reply pulls in two directions — "no
    # problem, waive it" reads as both. Refusing costs the owner one tap;
    # guessing costs a customer the wrong answer.
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# Recording and relaying
# --------------------------------------------------------------------------


def record_decision(
    ctx: ToolContext, case: Escalation, *, outcome: str, note: str | None = None
) -> Escalation:
    """Write the owner's answer to the case. Refuses an outcome it does not know."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown decision outcome: {outcome!r}")

    case.decision = outcome
    case.decision_note = (note or "").strip() or None
    case.decided_at = ctx.now()
    case.status = DECIDED
    ctx.session.flush()
    return case


def relay_directive(case: Escalation) -> str:
    """What to tell the agent so it can pass the decision on.

    Lives here rather than in the transport because it is not a WhatsApp
    concern: any surface carrying the conversation needs the same instruction.

    The wording matters more than it looks. Three things are stated explicitly
    because a model asked to deliver bad news will otherwise soften it, invent a
    consolation, or generalise a one-off into a policy — and the third of those
    is how an override quietly becomes the price list.
    """
    if case.decision == "owner_handled":
        # Nothing was decided — a person stepped in and dealt with it, usually
        # by phone. The agent's job is to close the loop lightly and get out of
        # the way, not to restate an outcome it does not know.
        return "\n".join([
            "A colleague has taken this over personally and dealt with it directly "
            "with the customer.",
            f"What happened: {case.question or case.detail or 'the escalated issue'}",
            "Acknowledge briefly and warmly that a colleague has looked after it, "
            "and ask if there is anything else you can help with.",
            "Do not restate what was decided or agreed — you were not part of that "
            "conversation and do not know what was said.",
        ])

    if case.decision == "owner_answered":
        # The colleague supplied a figure nobody else had. It is now the only
        # authority for that number, so it goes across exactly as given — a
        # rounded or "approximately" version of an owner's figure is an invented
        # figure wearing their authority.
        return "\n".join([
            "A colleague has supplied the figure the customer asked for.",
            f"What was asked: {case.question or case.detail or 'the escalated question'}",
            f"Their answer, exactly: {case.decision_note or '(no answer recorded)'}",
            "Give the customer this figure exactly as written. Do not round it, do not "
            "convert it, do not add 'approximately', and do not add conditions they did "
            "not state.",
            "Then carry on with the conversation where it left off — this was a question "
            "you could not answer, not a problem someone else has taken over.",
            "It applies to this customer and this car. It is not a price list, so do not "
            "describe it as what you normally charge.",
        ])

    outcome = {
        "approved": "approved it",
        "declined": "declined it",
        "owner_calling": "is going to call the customer directly about it",
    }.get(case.decision or "", case.decision or "answered")

    lines = [
        "A colleague has answered the question you escalated. Tell the customer "
        "the outcome now, warmly and without hedging.",
        f"What was asked: {case.question or case.detail or 'the escalated issue'}",
        f"Their answer: the colleague {outcome}.",
    ]
    if case.decision_note:
        lines.append(f"They added: {case.decision_note}")
    lines += [
        "State exactly this outcome. Do not add conditions they did not give, "
        "and do not soften a decline into a maybe.",
        "This decision applies to this customer only. It is not a change to "
        "policy, so do not describe it as what you normally do.",
    ]
    if case.decision == "owner_calling":
        lines.append("Do not attempt to resolve the issue yourself — a person is calling.")
    return "\n".join(lines)


def mark_relayed(ctx: ToolContext, case: Escalation) -> None:
    """Close the case and give the conversation back to the agent.

    Clearing the conversation flag is not enough on its own: the agent reads
    `ConversationState`, and a stage left at ESCALATED means it keeps promising
    that a colleague will be in touch about a question already answered.
    """
    case.relayed_at = ctx.now()
    case.status = RELAYED
    ctx.session.flush()

    conversation = ctx.conversations.get(case.conversation_id)
    if conversation is not None:
        conversation.escalated = False
        conversation.escalation_reason = None

    state = ctx.load_state()
    state.escalated = False
    state.escalation_reason = None
    state.stage = state.stage_before_escalation or Stage.QUALIFYING
    state.stage_before_escalation = None
    ctx.save_state(state)


def reopen_template(ctx: ToolContext) -> dict[str, str]:
    """The template used to reopen a closed 24-hour window."""
    configured = _config(ctx).get("reopen_template", {})
    return {
        "name": configured.get("name", "case_update"),
        "language": configured.get("language", "en"),
    }


def mark_reopen_requested(ctx: ToolContext, case: Escalation) -> None:
    """Record that the customer was asked to come back.

    The case stays DECIDED rather than RELAYED, because it is not: the answer
    exists and the customer has not heard it. It is delivered the moment they
    reply and the window opens again.
    """
    case.reopen_requested_at = ctx.now()
    ctx.session.flush()


def pending_for_conversation(ctx: ToolContext, conversation_id: str) -> Escalation | None:
    """A case this customer is currently waiting on an answer for."""
    return ctx.session.scalars(
        select(Escalation)
        .where(
            Escalation.conversation_id == conversation_id,
            Escalation.status.in_((AWAITING, TIMED_OUT)),
        )
        .order_by(Escalation.created_at.desc())
    ).first()


def decided_awaiting_relay(ctx: ToolContext, conversation_id: str) -> Escalation | None:
    return ctx.session.scalars(
        select(Escalation)
        .where(
            Escalation.conversation_id == conversation_id,
            Escalation.status == DECIDED,
        )
        .order_by(Escalation.decided_at.desc())
    ).first()


# --------------------------------------------------------------------------
# The owner not answering
# --------------------------------------------------------------------------


def _minutes(ctx: ToolContext, key: str, default: int) -> int:
    return int(_config(ctx).get("owner_decision", {}).get(key, default))


def needs_reminder(ctx: ToolContext, case: Escalation, now: datetime | None = None) -> bool:
    """Whether the owner is overdue a nudge, and has not already had one."""
    if case.status != AWAITING or case.reminded_at is not None or case.notified_at is None:
        return False
    after = _minutes(ctx, "reminder_after_minutes", 15)
    return (now or ctx.now()) - case.notified_at >= timedelta(minutes=after)


def has_timed_out(ctx: ToolContext, case: Escalation, now: datetime | None = None) -> bool:
    if case.status != AWAITING or case.notified_at is None:
        return False
    after = _minutes(ctx, "timeout_after_minutes", 45)
    return (now or ctx.now()) - case.notified_at >= timedelta(minutes=after)


def mark_reminded(ctx: ToolContext, case: Escalation) -> None:
    case.reminded_at = ctx.now()
    ctx.session.flush()


def mark_timed_out(ctx: ToolContext, case: Escalation) -> None:
    """Overdue, but still answerable — a late decision is better than none."""
    case.status = TIMED_OUT
    ctx.session.flush()


def overdue_cases(ctx: ToolContext, now: datetime | None = None) -> list[Escalation]:
    """Every open case that has run past its reminder or timeout window."""
    moment = now or ctx.now()
    return [
        case
        for case in _open_cases(ctx)
        if needs_reminder(ctx, case, moment) or has_timed_out(ctx, case, moment)
    ]
