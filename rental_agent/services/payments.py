"""Turning a stored quote into somewhere the customer can pay.

The rule this is built around: **no caller supplies an amount.** The tool takes a
reservation and a purpose, and the figure comes from what the engine calculated
and wrote down. There is no parameter a model could fill in, which is the only
version of this feature that is safe to have at all.

What the link should charge is genuinely undecided. The operator's terms say
payment happens at collection, and in thirteen conversations their team never
sent a link — what they take up front is a holding payment against a specific
car. So `purpose` is explicit, the default is the rental total because the engine
always knows it, and a purpose whose figure nobody has confirmed is refused
rather than guessed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from ..store.models import Reservation
from ..payments.stripe import minor_units, ZERO_DECIMAL

from ..context import ToolContext
from ..payments import PaymentError, build_provider

RENTAL_TOTAL = "rental_total"
HOLDING = "holding"
DEPOSIT = "deposit"


def _config(ctx: ToolContext) -> dict[str, Any]:
    return ctx.engine.rules.get("payment", {}).get("links", {})


def _amount_for(ctx: ToolContext, reservation: Any, purpose: str) -> Decimal | str:
    """The figure, or the reason there isn't one."""
    if purpose == RENTAL_TOTAL:
        return Decimal(str(reservation.total_charge))

    if purpose == DEPOSIT:
        if reservation.deposit is None:
            return (
                "The deposit for this vehicle has not been confirmed by the operator, so "
                "there is no figure to charge. Ask a colleague."
            )
        return Decimal(str(reservation.deposit))

    if purpose == HOLDING:
        configured = _config(ctx).get("holding_amount")
        if configured is None:
            return (
                "The holding payment amount has not been confirmed by the operator. Ask a "
                "colleague rather than naming one."
            )
        return Decimal(str(configured))

    return f"Unknown payment purpose {purpose!r}."


def create_payment_link(
    ctx: ToolContext, *, reservation_id: str, purpose: str | None = None
) -> dict[str, Any]:
    from .booking import _error, _live_reservation, _reservation_dict

    config = _config(ctx)
    if not config.get("enabled", False):
        return _error("payment_links_disabled", "This operator does not use payment links.")

    # Checked before the generic liveness test, which would refuse a held
    # booking as "not live" and tell the agent nothing it can act on. Taking
    # money for a car nobody has confirmed is free is worse than taking it late.
    existing = ctx.reservations.get(reservation_id)
    if existing is not None and existing.status == "held":
        return _error(
            "reservation_not_confirmed",
            "This booking is still held while a colleague confirms the car is free. "
            "Do not ask for payment until it is confirmed — tell them you will send "
            "the link as soon as it is.",
        )

    reservation = _live_reservation(ctx, reservation_id)
    if isinstance(reservation, dict):
        return reservation

    reservation = _locked(ctx, reservation_id)
    if reservation.status != "confirmed":
        return _error("reservation_not_confirmed", "This booking is not confirmed. A colleague must check it before payment.")
    if reservation.payment_status in {"paid", "payment_review", "refund_pending", "refunded", "partially_refunded", "holding_paid", "deposit_paid"}:
        return _error("payment_already_received", "A payment is already recorded. Do not request another payment; a colleague must check any balance or refund.")

    from .checkout import reservation_gate
    blocked = reservation_gate(ctx,reservation)
    if blocked:
        return blocked

    purpose = (purpose or config.get("default_purpose") or RENTAL_TOTAL).lower()
    if purpose not in config.get("purposes", [RENTAL_TOTAL]):
        return _error("unknown_purpose", f"{purpose!r} is not something this operator charges for.")

    amount = _amount_for(ctx, reservation, purpose)
    if isinstance(amount, str):
        return _error("amount_unconfirmed", amount, purpose=purpose)
    if amount <= 0:
        return _error("nothing_to_charge", f"The {purpose.replace('_', ' ')} is zero.")

    now = ctx.now()
    reference = f"PAY-{ctx.counters.next('payment')}"
    try:
        provider = build_provider()
        if reservation.is_demo and getattr(provider, "is_live", False):
            return _error("live_payment_for_demo", "A simulated reservation cannot collect real money. Use Stripe test mode.")
        if getattr(provider, "name", "") == "stripe" and (reservation.payment_reference or "").startswith("cs_"):
            if not provider.expire_checkout(reservation.payment_reference):
                return _error("checkout_already_completed", "The previous checkout has completed. Please do not pay again; return from Stripe or ask a colleague to verify the payment.")
        link = provider.create_link(
            amount=amount,
            currency=reservation.currency,
            reference=reference,
            description=f"{reservation.reservation_id} — {purpose.replace('_', ' ')}",
            metadata={
                "reservation_id": reservation.reservation_id,
                "purpose": purpose,
                "is_demo": str(reservation.is_demo).lower(),
            },
        )
    except PaymentError as exc:
        # The customer is waiting to pay. Say something useful.
        return _error("payment_provider_unavailable", str(exc))

    reservation.payment_reference = link.reference
    reservation.payment_status = "link_sent"
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {"event": "payment_link_created", "purpose": purpose,
         "amount": str(amount), "reference": link.reference,
         "currency": link.currency, "url": link.url, "is_demo": link.is_demo,
         "expires_at": getattr(link, "expires_at", None)},
        now,
    )

    result = _reservation_dict(reservation, ctx)
    result.update(
        {
            "payment_url": link.url,
            "payment_reference": link.reference,
            "amount": str(amount),
            "currency": link.currency,
            "purpose": purpose,
            "is_demo": link.is_demo,
            "guidance": (
                "Send them the link and say plainly what it is for and how much it is. "
                "The amount came from their stored quote — do not restate it as a "
                "different number, and do not offer to change it."
            ),
        }
    )
    ctx.queue_card(payment_receipt(result), tag="payment_link")
    return result


def payment_receipt(result: dict[str, Any], *, arabic: bool = False) -> str:
    if arabic:
        purpose = {RENTAL_TOTAL: "إجمالي الإيجار", DEPOSIT: "التأمين", HOLDING: "دفعة الحجز"}.get(result["purpose"], "الإيجار")
        text = f"رابط دفع {purpose}: {Decimal(result['amount']):,.2f} {result['currency']}\n{result['payment_url']}"
        if result.get("is_demo"):
            text += "\nهذه دفعة تجريبية فقط، ولن يتم خصم أموال حقيقية."
        return text
    text = (f"Payment for {result['purpose'].replace('_', ' ')}: "
            f"{result['currency']} {Decimal(result['amount']):,.2f}\n{result['payment_url']}")
    if result.get("is_demo"):
        text += "\nTest payment only. No real money is collected."
    return text


def _locked(ctx, reservation_id):
    return ctx.session.scalar(select(Reservation).where(
        Reservation.reservation_id == reservation_id).with_for_update().execution_options(populate_existing=True))


def mark_paid(
    ctx: ToolContext,
    *,
    reservation_id: str,
    amount_minor: int | None,
    currency: str,
    reference: str,
    event_id: str = "",
    payment_intent: str = "",
    livemode: bool | None = None,
) -> dict[str, Any]:
    """Reconcile an authenticated processor receipt against an issued checkout."""
    reservation = _locked(ctx, reservation_id)
    if reservation is None:
        return {"recorded": False, "reason": "unknown_reservation", "reservation_id": reservation_id}
    history = reservation.history or []
    if any(h.get("event") == "payment_received" and
           (h.get("reference") == reference or (event_id and h.get("event_id") == event_id)) for h in history):
        return {"recorded": False, "reason": "already_recorded", "reservation_id": reservation_id,
                "payment_status": reservation.payment_status}
    issued = next((h for h in reversed(history) if h.get("event") == "payment_link_created"
                   and h.get("reference") == reference), None)
    if not issued:
        return {"recorded": False, "reason": "unknown_checkout", "reservation_id": reservation_id}
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0 or not currency:
        return {"recorded": False, "reason": "invalid_payment_amount", "reservation_id": reservation_id}
    currency = currency.upper()
    amount = Decimal(amount_minor) / (1 if currency in ZERO_DECIMAL else 100)
    expected = Decimal(str(reservation.total_charge))
    purpose = issued.get("purpose", RENTAL_TOTAL)
    problems = []
    if currency != issued.get("currency", reservation.currency).upper():
        problems.append("currency_mismatch")
    if amount != Decimal(issued["amount"]):
        problems.append("amount_mismatch")
    if purpose == RENTAL_TOTAL and amount != expected:
        problems.append("booking_amount_changed")
    if reference != reservation.payment_reference:
        problems.append("superseded_checkout")
    if reservation.status != "confirmed":
        problems.append("booking_not_confirmed")
    if any(h.get("event") == "payment_received" for h in history):
        problems.append("additional_payment")
    if livemode is not None and livemode != (not issued.get("is_demo", reservation.is_demo)):
        problems.append("payment_mode_mismatch")
    status = "payment_review" if problems else {
        RENTAL_TOTAL: "paid", HOLDING: "holding_paid", DEPOSIT: "deposit_paid"
    }.get(purpose, "payment_review")
    reservation.payment_status = status
    reservation.version += 1
    ctx.reservations.append_history(reservation, {
        "event": "payment_received", "event_id": event_id, "reference": reference,
        "amount": str(amount), "currency": currency, "purpose": purpose,
        "payment_intent": payment_intent, "issues": problems,
    }, ctx.now())
    result = {"recorded": True, "reservation_id": reservation_id, "amount": str(amount),
              "currency": currency, "payment_status": status, "issues": problems}
    if amount != expected:
        result.update(amount_differs=True, expected=str(expected))
    if problems:
        queue_payment_review(ctx, reservation, "Payment reconciliation requires review: " + ", ".join(problems))
    record_payment_message(ctx, reservation, result)
    return result


def record_payment_message(ctx, reservation, result):
    """Persist the same factual receipt for the browser and WhatsApp."""
    status = result["payment_status"]
    if status == "paid":
        text = f"Payment received: {result['currency']} {result['amount']} for {reservation.reservation_id}. Your rental payment is recorded."
    elif status in {"holding_paid", "deposit_paid"}:
        text = f"Your {status.replace('_paid', '')} payment is recorded for {reservation.reservation_id}. This does not mean the rental balance is paid."
    elif status in {"refunded", "partially_refunded"}:
        text = f"Stripe reports a {status.replace('_', ' ')} payment for {reservation.reservation_id}: {result['currency']} {result['amount']}."
    else:
        text = f"A payment was received for {reservation.reservation_id}, but it needs a colleague's review before we can confirm the rental payment. Please do not pay again."
    result["message"] = text
    if reservation.conversation_id:
        ctx.messages.record(conversation_id=reservation.conversation_id, direction="outbound", content=text, now=ctx.now())


def mark_checkout_status(ctx, *, reservation_id, reference, status, event_id=""):
    reservation = _locked(ctx, reservation_id)
    if reservation is None:
        return {"recorded": False, "reason": "unknown_reservation"}
    if reference != reservation.payment_reference or reservation.payment_status not in {"link_sent", "failed", "expired"}:
        return {"recorded": False, "reason": "stale_checkout_event"}
    if reservation.payment_status == status:
        return {"recorded": False, "reason": "already_recorded"}
    reservation.payment_status = status
    ctx.reservations.append_history(reservation, {"event": "checkout_" + status,
        "reference": reference, "event_id": event_id}, ctx.now())
    if reservation.conversation_id:
        ctx.messages.record(conversation_id=reservation.conversation_id, direction="outbound",
            content=f"The payment checkout for {reservation_id} has {status}. The rental is not marked paid. You can request a new link.", now=ctx.now())
    return {"recorded": True, "payment_status": status, "reservation_id": reservation_id}


def mark_refunded(ctx, *, payment_intent, amount_minor, currency, event_id=""):
    if not payment_intent:
        return {"recorded": False, "reason": "unknown_payment"}
    # Receipt history is also kept for cancelled bookings and superseded links.
    for row in ctx.session.scalars(select(Reservation)):
        receipt = next((h for h in row.history or [] if h.get("event") == "payment_received"
                        and h.get("payment_intent") == payment_intent), None)
        if receipt:
            break
    else:
        return {"recorded": False, "reason": "unknown_payment"}
    reservation = _locked(ctx, row.reservation_id)
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0 or currency.upper() != receipt['currency']:
        return {"recorded": False, "reason": "invalid_refund"}
    amount = Decimal(amount_minor) / (1 if currency.upper() in ZERO_DECIMAL else 100)
    previous = max((Decimal(h['amount']) for h in reservation.history or [] if h.get('event') == 'payment_refunded'
                    and h.get('payment_intent') == payment_intent), default=Decimal(0))
    if amount <= previous:
        return {"recorded": False, "reason": "already_recorded"}
    status = "refunded" if amount == Decimal(receipt['amount']) else "partially_refunded"
    if amount > Decimal(receipt['amount']) or len([h for h in reservation.history or [] if h.get('event') == 'payment_received']) > 1:
        status = "payment_review"
    reservation.payment_status = status
    ctx.reservations.append_history(reservation, {"event": "payment_refunded", "payment_intent": payment_intent,
        "amount": str(amount), "currency": currency.upper(), "event_id": event_id}, ctx.now())
    result = {"recorded": True, "reservation_id": reservation.reservation_id, "payment_status": status,
              "amount": str(amount), "currency": currency.upper()}
    record_payment_message(ctx, reservation, result)
    return result


def cancellation_payment_review(ctx, reservation):
    """Close unpaid checkout where possible; queue paid cancellations for staff."""
    received = [h for h in reservation.history or [] if h.get('event') == 'payment_received']
    if received and reservation.payment_status != 'refunded':
        reservation.payment_status = 'refund_pending'
        ctx.reservations.append_history(reservation, {'event': 'refund_review_requested',
            'reason': 'booking_cancelled'}, ctx.now())
        queue_payment_review(ctx, reservation, 'A paid booking was cancelled. Review the cancellation fee and approve any refund. No refund has been sent.')
        return 'A refund review has been recorded for a colleague. No refund has been issued yet.'
    if (reservation.payment_reference or '').startswith('cs_'):
        try:
            provider = build_provider()
            if provider.name == 'stripe':
                if not provider.expire_checkout(reservation.payment_reference):
                    queue_payment_review(ctx, reservation, 'The cancelled booking has a completed checkout. Reconcile its payment and any refund before proceeding.')
                    return 'The checkout needs a colleague to reconcile its payment. Please do not pay again.'
                if reservation.payment_status == 'link_sent':
                    reservation.payment_status = 'expired'
        except PaymentError:
            queue_payment_review(ctx, reservation, 'Booking cancelled, but its Stripe checkout could not be closed. Check the payment account before taking further action.')
            return 'A colleague needs to check the old payment link. Please do not pay it.'
    return ''


def queue_payment_review(ctx, reservation, detail):
    if not reservation.conversation_id:
        return
    from .escalation import escalate_conversation
    review_ctx = ToolContext(session=ctx.session, customer_id=reservation.customer_id,
        conversation_id=reservation.conversation_id, now_fn=ctx.now_fn, reference_date=ctx.reference_date)
    escalate_conversation(review_ctx, reason='refund_request', detail=f'{reservation.reservation_id}: {detail}')
