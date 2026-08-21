"""State-changing tools.

Thin argument-parsing wrappers over `services/`. Every one of them is routed
through the idempotency ledger by `registry.py`; none of them may be called
without a database session.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from ..context import ToolContext
from ..services import booking, escalation
from .rental_tools import _parse_dt, _parse_money


def create_demo_quote(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.create_demo_quote(
        ctx,
        vehicle_id=args["vehicle_id"],
        pickup_at=_parse_dt(args["pickup_at"], "pickup_at", ctx.engine.tz),
        return_at=_parse_dt(args["return_at"], "return_at", ctx.engine.tz),
        delivery_location=args.get("delivery_location"),
        discount_percent=_parse_money(args.get("discount_percent"), "discount_percent")
        or Decimal("0"),
        excess_reduction=bool(args.get("excess_reduction", False)),
    )


def create_demo_reservation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.create_demo_reservation(ctx, quote_id=args["quote_id"])


def modify_demo_reservation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    tz = ctx.engine.tz
    return booking.modify_demo_reservation(
        ctx,
        reservation_id=args["reservation_id"],
        pickup_at=_parse_dt(args["pickup_at"], "pickup_at", tz) if args.get("pickup_at") else None,
        return_at=_parse_dt(args["return_at"], "return_at", tz) if args.get("return_at") else None,
        delivery_location=args.get("delivery_location"),
        vehicle_id=args.get("vehicle_id"),
    )


def extend_demo_rental(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.extend_demo_rental(
        ctx,
        reservation_id=args["reservation_id"],
        new_return_at=_parse_dt(args["new_return_at"], "new_return_at", ctx.engine.tz),
    )


def cancel_demo_reservation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.cancel_demo_reservation(
        ctx, reservation_id=args["reservation_id"], reason=args.get("reason")
    )


def record_demo_documents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Record paperwork the customer has actually sent.

    Refused when nothing has arrived. A customer typing "here you go" is not a
    licence, and an agent that files it as one has produced a compliance record
    with nothing behind it — the same class of mistake as quoting a price no
    tool returned, on a subject where being wrong is worse.
    """
    if ctx.documents_received() == 0:
        return {
            "error": "no_documents_received",
            "message": (
                "Nothing has been sent in this conversation. Documents are recorded "
                "from what actually arrives, not from the customer saying they sent it."
            ),
            "hint": (
                "Ask them to attach a photo of each document. Do not tell them "
                "anything is on file until it is."
            ),
        }

    documents = args["documents"]
    if isinstance(documents, str):
        documents = [documents]
    return booking.record_demo_documents(
        ctx, documents=list(documents), reservation_id=args.get("reservation_id")
    )


def simulate_payment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.simulate_payment(
        ctx,
        reservation_id=args["reservation_id"],
        method=args.get("method", "credit_card"),
    )


def schedule_demo_delivery(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.schedule_demo_delivery(
        ctx,
        reservation_id=args["reservation_id"],
        delivery_at=_parse_dt(args["delivery_at"], "delivery_at", ctx.engine.tz)
        if args.get("delivery_at")
        else None,
        delivery_location=args.get("delivery_location"),
    )


def save_customer_preference(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.save_customer_preference(ctx, key=args["key"], value=args["value"])


def escalate_conversation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return escalation.escalate_conversation(
        ctx, reason=args["reason"], detail=args.get("detail")
    )


# --------------------------------------------------------------------------
# Reads that need the database but change nothing
# --------------------------------------------------------------------------


def get_customer(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.get_customer(ctx)


def get_active_reservation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return booking.get_active_reservation(ctx)


#: Routed through the idempotency ledger.
STATE_HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], dict[str, Any]]] = {
    "create_demo_quote": create_demo_quote,
    "create_demo_reservation": create_demo_reservation,
    "modify_demo_reservation": modify_demo_reservation,
    "extend_demo_rental": extend_demo_rental,
    "cancel_demo_reservation": cancel_demo_reservation,
    "record_demo_documents": record_demo_documents,
    "simulate_payment": simulate_payment,
    "schedule_demo_delivery": schedule_demo_delivery,
    "save_customer_preference": save_customer_preference,
    "escalate_conversation": escalate_conversation,
}

#: Need a session, but are safe to repeat, so they bypass the ledger.
PERSISTED_READ_HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], dict[str, Any]]] = {
    "get_customer": get_customer,
    "get_active_reservation": get_active_reservation,
}
