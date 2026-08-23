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
        link = build_provider().create_link(
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
         "amount": str(amount), "reference": link.reference},
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
    return result
