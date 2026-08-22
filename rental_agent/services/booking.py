"""Booking operations.

Rules enforced here rather than trusted to the model:

* A reservation can only be created from a stored quote, so every booked total
  traces back to a figure the engine calculated and the customer was shown.
* Availability is always re-checked at write time. The car may have gone in the
  minutes between "here are your options" and "yes, book it".
* Modifications exclude the booking itself from the availability check, so
  moving a delivery by an hour does not conflict with itself.
* Cancellation and extension fees come from rules.json, never from a judgement
  call in the conversation.

Every function returns a tool-shaped dict and raises nothing: the agent has to
be able to explain a refusal, not crash on it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..context import ToolContext
from ..domain.enums import Stage
from ..domain.models import Quote as QuoteModel
from ..engine.engine import VehicleNotFound, VehicleUnavailable
from ..engine.locations import normalise_location
from ..store.models import Reservation

LIVE_STATUSES = ("pending", "confirmed")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, **extra}


def _update_state(ctx: ToolContext, **changes: Any) -> None:
    """Best-effort state sync. Absent a conversation (console use) this no-ops."""
    if not ctx.conversation_id or ctx.session is None:
        return
    if ctx.conversations.get(ctx.conversation_id) is None:
        return
    state = ctx.load_state()
    for key, value in changes.items():
        setattr(state, key, value)
    ctx.save_state(state)


def _reservation_dict(reservation: Reservation, ctx: ToolContext) -> dict[str, Any]:
    vehicle = ctx.engine.get_vehicle(reservation.vehicle_id)
    return {
        "reservation_id": reservation.reservation_id,
        "is_demo": True,
        "status": reservation.status,
        "vehicle_id": reservation.vehicle_id,
        "vehicle_display_name": vehicle.display_name,
        "customer_id": reservation.customer_id,
        "quote_id": reservation.quote_id,
        "pickup_at": reservation.pickup_at.isoformat(),
        "return_at": reservation.return_at.isoformat(),
        "delivery_location": reservation.delivery_location,
        "total_charge": str(reservation.total_charge),
        "deposit": str(reservation.deposit) if reservation.deposit is not None else None,
        "currency": reservation.currency,
        "payment_status": reservation.payment_status,
        "documents_recorded": list(reservation.documents or []),
        "delivery_scheduled_at": (
            reservation.delivery_scheduled_at.isoformat()
            if reservation.delivery_scheduled_at
            else None
        ),
        "version": reservation.version,
        "demo_notice": ctx.engine.rules.demo_disclosure["required_footer"],
    }


def _persist_quote(ctx: ToolContext, quote: QuoteModel) -> None:
    ctx.quotes.save(
        quote_id=quote.quote_id,
        vehicle_id=quote.vehicle_id,
        payload=quote.model_dump(mode="json"),
        total_charge=quote.total_charge,
        deposit=quote.deposit,
        created_at=quote.created_at,
        expires_at=quote.expires_at,
        conversation_id=ctx.conversation_id,
        customer_id=ctx.customer_id,
    )


def _price(
    ctx: ToolContext,
    *,
    vehicle_id: str,
    pickup_at: datetime,
    return_at: datetime,
    delivery_location: str | None,
    discount_percent: Decimal,
    quote_id: str,
    exclude_reservation_id: str | None = None,
) -> QuoteModel:
    engine = ctx.engine_excluding(exclude_reservation_id)
    return engine.calculate_quote(
        vehicle_id=vehicle_id,
        pickup_at=pickup_at,
        return_at=return_at,
        delivery_location=delivery_location,
        discount_percent=discount_percent,
        quote_id=quote_id,
    )


def _live_reservation(ctx: ToolContext, reservation_id: str) -> Reservation | dict[str, Any]:
    reservation = ctx.reservations.get(reservation_id)
    if reservation is None:
        return _error("reservation_not_found", f"No demo reservation {reservation_id}")
    if reservation.status not in LIVE_STATUSES:
        return _error(
            "reservation_not_live",
            f"Reservation {reservation_id} is {reservation.status} and cannot be changed",
            status=reservation.status,
        )
    return reservation


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


def create_demo_quote(
    ctx: ToolContext,
    *,
    vehicle_id: str,
    pickup_at: datetime,
    return_at: datetime,
    delivery_location: str | None = None,
    discount_percent: Decimal = Decimal("0"),
    excess_reduction: bool = False,
) -> dict[str, Any]:
    quote_id = ctx.counters.next_quote_reference()
    try:
        engine = ctx.engine
        quote = engine.calculate_quote(
            vehicle_id=vehicle_id,
            pickup_at=pickup_at,
            return_at=return_at,
            delivery_location=delivery_location,
            discount_percent=discount_percent,
            excess_reduction=excess_reduction,
            quote_id=quote_id,
        )
    except VehicleNotFound as exc:
        return _error("vehicle_not_found", str(exc))
    except VehicleUnavailable as exc:
        return _error(
            "vehicle_unavailable",
            str(exc),
            vehicle_id=exc.vehicle_id,
            hint="Call find_alternatives before telling the customer it is unavailable.",
        )
    except ValueError as exc:
        return _error("invalid_request", str(exc))

    _persist_quote(ctx, quote)
    _update_state(
        ctx,
        quote_id=quote.quote_id,
        selected_vehicle_id=vehicle_id,
        stage=Stage.QUOTED,
    )

    return {
        "quote_id": quote.quote_id,
        "is_demo": True,
        "vehicle_id": quote.vehicle_id,
        "vehicle_display_name": quote.vehicle_display_name,
        "billable_days": quote.billable_days,
        "total_charge": str(quote.total_charge),
        "deposit": str(quote.deposit) if quote.deposit is not None else None,
        "total_due_at_delivery": str(quote.total_due_at_delivery),
        "currency": quote.currency,
        "expires_at": quote.expires_at.isoformat(),
        "demo_notice": ctx.engine.rules.demo_disclosure["required_footer"],
    }


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------


def create_demo_reservation(
    ctx: ToolContext, *, quote_id: str, customer_id: str | None = None
) -> dict[str, Any]:
    """Convert a stored quote into a demo reservation.

    Quote-only by design: there is no path to a booked total that the engine did
    not calculate and the customer did not see.
    """
    customer_id = customer_id or ctx.customer_id
    if not customer_id:
        return _error("no_customer", "No customer is associated with this conversation")

    stored = ctx.quotes.get(quote_id)
    if stored is None:
        return _error(
            "quote_not_found",
            f"No demo quote {quote_id}. Create one with create_demo_quote first.",
        )

    quote = QuoteModel.model_validate(stored.payload)

    if quote.expires_at <= ctx.now():
        return _error(
            "quote_expired",
            f"Quote {quote_id} expired at {quote.expires_at.isoformat()}. Re-quote before booking.",
            expired_at=quote.expires_at.isoformat(),
        )

    # Re-check at write time: the car may have gone since the quote was shown.
    availability = ctx.engine.check_availability(
        quote.vehicle_id, quote.pickup_at, quote.return_at
    )
    if not availability.available:
        return _error(
            "vehicle_unavailable",
            "That vehicle was taken while we were talking",
            vehicle_id=quote.vehicle_id,
            reason=availability.reason.value if availability.reason else None,
            hint="Call find_alternatives and offer the customer a substitute.",
        )

    customer = ctx.customers.get(customer_id)
    vehicle = ctx.engine.get_vehicle(quote.vehicle_id)
    minimum_age = ctx.engine.minimum_age_for(vehicle.category)
    if customer is not None and customer.driver_age is not None and customer.driver_age < minimum_age:
        return _error(
            "driver_too_young",
            f"The minimum age for a {vehicle.category.value.replace('_', ' ')} is {minimum_age}",
            minimum_age=minimum_age,
        )

    now = ctx.now()
    reservation = ctx.reservations.create(
        reservation_id=ctx.counters.next_reservation_reference(),
        is_demo=True,
        customer_id=customer_id,
        conversation_id=ctx.conversation_id,
        vehicle_id=quote.vehicle_id,
        quote_id=quote.quote_id,
        status="confirmed",
        pickup_at=quote.pickup_at,
        return_at=quote.return_at,
        delivery_location=quote.delivery_location,
        total_charge=quote.total_charge,
        deposit=quote.deposit,
        currency=quote.currency,
        payment_status="none",
        documents=[],
        created_at=now,
        updated_at=now,
        version=1,
        history=[{"event": "created", "quote_id": quote.quote_id, "at": now.isoformat()}],
    )
    ctx.quotes.mark_converted(quote.quote_id)
    _update_state(
        ctx,
        reservation_id=reservation.reservation_id,
        selected_vehicle_id=reservation.vehicle_id,
        stage=Stage.RESERVED,
    )

    return _reservation_dict(reservation, ctx)


def modify_demo_reservation(
    ctx: ToolContext,
    *,
    reservation_id: str,
    pickup_at: datetime | None = None,
    return_at: datetime | None = None,
    delivery_location: str | None = None,
    vehicle_id: str | None = None,
) -> dict[str, Any]:
    reservation = _live_reservation(ctx, reservation_id)
    if isinstance(reservation, dict):
        return reservation

    before = {
        "pickup_at": reservation.pickup_at.isoformat(),
        "return_at": reservation.return_at.isoformat(),
        "delivery_location": reservation.delivery_location,
        "vehicle_id": reservation.vehicle_id,
        "total_charge": str(reservation.total_charge),
    }

    new_pickup = pickup_at or reservation.pickup_at
    new_return = return_at or reservation.return_at
    new_location = (
        normalise_location(delivery_location) or delivery_location
        if delivery_location is not None
        else reservation.delivery_location
    )
    new_vehicle = vehicle_id or reservation.vehicle_id

    if new_return <= new_pickup:
        return _error("invalid_request", "The return time must be after the pickup time")

    # Nothing actually changes. Return the booking as it stands rather than
    # appending a second identical history entry: the ledger catches same-turn
    # retries, and this catches the customer (or the model) repeating a change
    # that has already been applied.
    if (
        new_pickup == reservation.pickup_at
        and new_return == reservation.return_at
        and new_location == reservation.delivery_location
        and new_vehicle == reservation.vehicle_id
    ):
        unchanged = _reservation_dict(reservation, ctx)
        unchanged.update({"changed": [], "no_change": True, "price_difference": "0.00"})
        return unchanged

    # Excluding this reservation is the whole trick: without it, moving the
    # delivery from 7 PM to 8 PM collides with the booking being modified.
    engine = ctx.engine_excluding(reservation_id)
    availability = engine.check_availability(new_vehicle, new_pickup, new_return)
    if not availability.available:
        return _error(
            "vehicle_unavailable",
            "That change is not possible — the vehicle is committed for part of the new period",
            vehicle_id=new_vehicle,
            reason=availability.reason.value if availability.reason else None,
            next_available_from=(
                availability.next_available_from.isoformat()
                if availability.next_available_from
                else None
            ),
        )

    try:
        quote = _price(
            ctx,
            vehicle_id=new_vehicle,
            pickup_at=new_pickup,
            return_at=new_return,
            delivery_location=new_location,
            discount_percent=Decimal("0"),
            quote_id=ctx.counters.next_quote_reference(),
            exclude_reservation_id=reservation_id,
        )
    except (ValueError, VehicleNotFound) as exc:
        return _error("invalid_request", str(exc))

    _persist_quote(ctx, quote)

    now = ctx.now()
    difference = quote.total_charge - reservation.total_charge

    reservation.pickup_at = new_pickup
    reservation.return_at = new_return
    reservation.delivery_location = new_location
    reservation.vehicle_id = new_vehicle
    reservation.total_charge = quote.total_charge
    reservation.deposit = quote.deposit
    reservation.quote_id = quote.quote_id
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {"event": "modified", "before": before, "after": {
            "pickup_at": new_pickup.isoformat(),
            "return_at": new_return.isoformat(),
            "delivery_location": new_location,
            "vehicle_id": new_vehicle,
            "total_charge": str(quote.total_charge),
        }},
        now,
    )

    _update_state(ctx, pickup_at=new_pickup, return_at=new_return, delivery_location=new_location)

    result = _reservation_dict(reservation, ctx)
    result["price_difference"] = str(difference)
    result["changed"] = [
        field
        for field, was in (
            ("pickup_at", before["pickup_at"]),
            ("return_at", before["return_at"]),
            ("delivery_location", before["delivery_location"]),
            ("vehicle_id", before["vehicle_id"]),
        )
        if str(result[field]) != str(was)
    ]
    return result


def extend_demo_rental(
    ctx: ToolContext, *, reservation_id: str, new_return_at: datetime
) -> dict[str, Any]:
    reservation = _live_reservation(ctx, reservation_id)
    if isinstance(reservation, dict):
        return reservation

    if new_return_at == reservation.return_at:
        unchanged = _reservation_dict(reservation, ctx)
        unchanged.update({"no_change": True, "additional_charge": "0.00"})
        return unchanged

    if new_return_at < reservation.return_at:
        return _error(
            "invalid_request",
            "An extension must end after the current return time",
            current_return_at=reservation.return_at.isoformat(),
        )

    rules = ctx.engine.rules
    notice_hours = int(rules["extension"]["must_request_hours_before_return"])
    hours_left = (reservation.return_at - ctx.now()).total_seconds() / 3600
    if hours_left < notice_hours:
        return _error(
            "policy_notice_period",
            f"Extensions need {notice_hours} hours' notice before the return time; "
            f"this request has {max(hours_left, 0):.1f}",
            requires_human_approval=True,
        )

    engine = ctx.engine_excluding(reservation_id)
    availability = engine.check_availability(
        reservation.vehicle_id, reservation.pickup_at, new_return_at
    )
    if not availability.available:
        return _error(
            "vehicle_unavailable",
            "The vehicle is booked for part of the extended period",
            vehicle_id=reservation.vehicle_id,
            next_available_from=(
                availability.next_available_from.isoformat()
                if availability.next_available_from
                else None
            ),
            hint="Offer to swap to an available vehicle for the extra days, or escalate.",
        )

    try:
        quote = _price(
            ctx,
            vehicle_id=reservation.vehicle_id,
            pickup_at=reservation.pickup_at,
            return_at=new_return_at,
            delivery_location=reservation.delivery_location,
            discount_percent=Decimal("0"),
            quote_id=ctx.counters.next_quote_reference(),
            exclude_reservation_id=reservation_id,
        )
    except ValueError as exc:
        return _error("invalid_request", str(exc))

    _persist_quote(ctx, quote)

    now = ctx.now()
    previous_total = reservation.total_charge
    previous_return = reservation.return_at

    reservation.return_at = new_return_at
    reservation.total_charge = quote.total_charge
    reservation.quote_id = quote.quote_id
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {
            "event": "extended",
            "from": previous_return.isoformat(),
            "to": new_return_at.isoformat(),
            "additional_charge": str(quote.total_charge - previous_total),
        },
        now,
    )
    _update_state(ctx, return_at=new_return_at, stage=Stage.ACTIVE_RENTAL)

    result = _reservation_dict(reservation, ctx)
    result["additional_charge"] = str(quote.total_charge - previous_total)
    result["previous_return_at"] = previous_return.isoformat()
    # The whole rental is re-rated, so a long extension can reach the weekly
    # rate and cost less per day than the original booking.
    result["rate_basis"] = quote.rate_basis
    return result


def cancel_demo_reservation(
    ctx: ToolContext, *, reservation_id: str, reason: str | None = None
) -> dict[str, Any]:
    already = ctx.reservations.get(reservation_id)
    if already is not None and already.status == "cancelled":
        # Telling the customer "that cannot be cancelled" about a booking they
        # already cancelled is worse than telling them it is already done.
        previous = next(
            (e for e in reversed(already.history or []) if e.get("event") == "cancelled"), {}
        )
        result = _reservation_dict(already, ctx)
        result.update(
            {
                "already_cancelled": True,
                "cancellation_fee": previous.get("fee", "0.00"),
                "cancellation_band": previous.get("band"),
                "vehicle_released": True,
            }
        )
        return result

    reservation = _live_reservation(ctx, reservation_id)
    if isinstance(reservation, dict):
        return reservation

    rules = ctx.engine.rules
    policy = rules["cancellation"]
    now = ctx.now()
    hours_before = (reservation.pickup_at - now).total_seconds() / 3600

    if hours_before >= float(policy["free_cancellation_hours_before_pickup"]):
        fee_percent = Decimal("0")
        band = "free"
    elif hours_before >= 0:
        fee_percent = Decimal(str(policy["late_cancellation_fee_percent"]))
        band = "late"
    else:
        fee_percent = Decimal(str(policy["no_show_fee_percent"]))
        band = "no_show"

    fee = (reservation.total_charge * fee_percent / 100).quantize(Decimal("0.01"))

    reservation.status = "cancelled"
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {"event": "cancelled", "reason": reason, "fee": str(fee), "band": band},
        now,
    )
    _update_state(ctx, reservation_id=None, stage=Stage.LOST_LEAD)

    result = _reservation_dict(reservation, ctx)
    result.update(
        {
            "cancellation_fee": str(fee),
            "cancellation_fee_percent": str(fee_percent),
            "cancellation_band": band,
            "hours_before_pickup": round(hours_before, 1),
            "vehicle_released": True,
        }
    )
    return result


# --------------------------------------------------------------------------
# Documents, payment, delivery
# --------------------------------------------------------------------------


def record_demo_documents(
    ctx: ToolContext, *, documents: list[str], reservation_id: str | None = None
) -> dict[str, Any]:
    """Simulated only. No identity document is verified, requested or stored."""
    if not ctx.customer_id:
        return _error("no_customer", "No customer is associated with this conversation")
    customer = ctx.customers.get(ctx.customer_id)
    if customer is None:
        return _error("no_customer", f"No customer {ctx.customer_id}")

    rules = ctx.engine.rules
    required_by_residency = rules["required_documents"]
    residency = customer.residency if customer.residency in required_by_residency else "tourist"
    required = list(required_by_residency[residency])

    reservation = ctx.reservations.get(reservation_id) if reservation_id else None
    if reservation is not None:
        vehicle = ctx.engine.get_vehicle(reservation.vehicle_id)
        extra = required_by_residency["additional_for_categories"].get(vehicle.category.value, [])
        required.extend(extra)

    on_file = sorted(set(customer.documents_on_file or []) | set(documents))
    customer.documents_on_file = on_file
    if reservation is not None:
        reservation.documents = on_file
        ctx.reservations.append_history(
            reservation, {"event": "documents_recorded", "documents": documents}, ctx.now()
        )

    missing = [doc for doc in required if doc not in on_file]
    _update_state(
        ctx, stage=Stage.DOCUMENTS_PENDING if missing else Stage.PAYMENT_PENDING
    )

    return {
        "is_demo": True,
        "residency": residency,
        "recorded": documents,
        "documents_on_file": on_file,
        "required": required,
        "still_missing": missing,
        "complete": not missing,
        "demo_notice": "Documents are simulated. Nothing is verified or stored.",
    }


def simulate_payment(
    ctx: ToolContext, *, reservation_id: str, method: str = "credit_card"
) -> dict[str, Any]:
    """No real payment is ever taken. This only marks the demo record."""
    reservation = _live_reservation(ctx, reservation_id)
    if isinstance(reservation, dict):
        return reservation

    accepted = ctx.engine.rules["payment"]["accepted"]
    if method not in accepted:
        return _error(
            "payment_method_not_accepted",
            f"Accepted methods are: {', '.join(accepted)}",
            accepted=accepted,
        )

    now = ctx.now()
    reference = f"DEMOPAY-{ctx.counters.next('payment')}"
    reservation.payment_status = "authorised"
    reservation.payment_reference = reference
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {"event": "payment_simulated", "method": method, "reference": reference},
        now,
    )
    _update_state(ctx, stage=Stage.RESERVED)

    result = _reservation_dict(reservation, ctx)
    result.update(
        {
            "payment_reference": reference,
            "payment_method": method,
            "amount_authorised": (
                str(reservation.deposit) if reservation.deposit is not None else None
            ),
            "simulated": True,
            "demo_notice": "SIMULATED PAYMENT — no card was charged and no money moved.",
        }
    )
    return result


def schedule_demo_delivery(
    ctx: ToolContext,
    *,
    reservation_id: str,
    delivery_at: datetime | None = None,
    delivery_location: str | None = None,
) -> dict[str, Any]:
    reservation = _live_reservation(ctx, reservation_id)
    if isinstance(reservation, dict):
        return reservation

    scheduled = delivery_at or reservation.pickup_at
    if delivery_location:
        reservation.delivery_location = (
            normalise_location(delivery_location) or delivery_location
        )

    reservation.delivery_scheduled_at = scheduled
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {"event": "delivery_scheduled", "at": scheduled.isoformat()},
        ctx.now(),
    )
    _update_state(ctx, stage=Stage.DELIVERY_SCHEDULED)

    result = _reservation_dict(reservation, ctx)
    hours = ctx.engine.rules.delivery["operating_hours"]
    result["within_operating_hours"] = (
        hours["from"] <= scheduled.strftime("%H:%M") < hours["to"]
    )
    return result


# --------------------------------------------------------------------------
# Customer memory
# --------------------------------------------------------------------------


def get_customer(ctx: ToolContext) -> dict[str, Any]:
    if not ctx.customer_id:
        return _error("no_customer", "No customer is associated with this conversation")
    customer = ctx.customers.get(ctx.customer_id)
    if customer is None:
        return _error("no_customer", f"No customer {ctx.customer_id}")

    reservations = ctx.reservations.for_customer(customer.customer_id)
    return {
        "customer_id": customer.customer_id,
        "name": customer.name,
        "residency": customer.residency,
        "driver_age": customer.driver_age,
        "documents_on_file": list(customer.documents_on_file or []),
        "preferences": dict(customer.preferences or {}),
        "is_returning_customer": len(reservations) > 0,
        "previous_demo_bookings": [
            {
                "reservation_id": r.reservation_id,
                "vehicle_id": r.vehicle_id,
                "status": r.status,
                "pickup_at": r.pickup_at.isoformat(),
            }
            for r in reservations
        ],
        "note": (
            "Preferences are conversational memory only. They never determine "
            "price, availability or policy."
        ),
    }


def save_customer_preference(ctx: ToolContext, *, key: str, value: Any) -> dict[str, Any]:
    """Conversational memory. Deliberately cannot touch business facts.

    This is the boundary the specification calls out: a customer saying "you
    always give me 20% off" must never become a rule. Keys that look like
    pricing or policy are refused outright.
    """
    forbidden = {
        "price", "prices", "daily_price", "discount", "discount_percent", "deposit",
        "availability", "policy", "insurance", "mileage", "rate", "total", "fee",
    }
    normalised = key.strip().lower().replace(" ", "_")
    if normalised in forbidden or any(word in normalised for word in ("price", "discount", "deposit", "policy")):
        return _error(
            "preference_not_allowed",
            f"'{key}' is a business fact, not a preference. Prices, discounts, "
            f"deposits and policies come from configuration only.",
            allowed_examples=["preferred_color", "favourite_model", "preferred_pickup_time", "language"],
        )

    if not ctx.customer_id:
        return _error("no_customer", "No customer is associated with this conversation")
    customer = ctx.customers.get(ctx.customer_id)
    if customer is None:
        return _error("no_customer", f"No customer {ctx.customer_id}")

    ctx.customers.save_preference(customer, normalised, value)
    return {
        "saved": True,
        "key": normalised,
        "value": value,
        "preferences": dict(customer.preferences or {}),
    }


def get_active_reservation(ctx: ToolContext) -> dict[str, Any]:
    """What "can you make it 8 instead?" refers to."""
    if not ctx.customer_id:
        return _error("no_customer", "No customer is associated with this conversation")
    reservation = ctx.reservations.active_for_customer(ctx.customer_id)
    if reservation is None:
        return {"has_active_reservation": False, "reservation": None}
    return {"has_active_reservation": True, "reservation": _reservation_dict(reservation, ctx)}
