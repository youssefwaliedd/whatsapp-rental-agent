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

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..context import ToolContext
from ..domain import consent
from ..domain.enums import ReservationStatus, Stage
from . import escalation, idempotency, outcomes
from ..booking_provider import Outcome
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


def _owned_by_this_customer(ctx: ToolContext, reservation: Reservation) -> bool:
    """Whether this conversation's customer is the one who has this booking.

    There was no such check. A reference is a short guessable string that gets
    quoted back in messages, and every customer-facing operation looked one up
    by reference alone — so the wrong reference in the wrong conversation would
    have modified, cancelled or paid for a stranger's booking.

    The owner's own path does not come through here: a colleague acting on a
    case is acting for the customer, not as them.
    """
    return bool(ctx.customer_id) and reservation.customer_id == ctx.customer_id


def _live_reservation(ctx: ToolContext, reservation_id: str) -> Reservation | dict[str, Any]:
    reservation = ctx.reservations.get(reservation_id)
    if reservation is None:
        return _error("reservation_not_found", f"No demo reservation {reservation_id}")
    if not _owned_by_this_customer(ctx, reservation):
        # Deliberately the same answer as a reference that does not exist.
        # Saying "that booking is not yours" confirms that it is somebody's.
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

    # The engine renders the figures; the model writes the sentence around them.
    # Without this the customer only ever sees the model's prose summary of a
    # JSON payload — which on 23 Aug quoted a total and never mentioned the
    # deposit at all, one message before the customer would have booked.
    from ..formatting import quote_message

    ctx.queue_card(quote_message(quote, ctx.engine.rules), tag="quote")

    return {
        "quote_id": quote.quote_id,
        "is_demo": True,
        "vehicle_id": quote.vehicle_id,
        "vehicle_display_name": quote.vehicle_display_name,
        "billable_days": quote.billable_days,
        "total_charge": str(quote.total_charge),
        "deposit": str(quote.deposit) if quote.deposit is not None else None,
        "total_due_at_delivery": (
            str(quote.total_due_at_delivery)
            if quote.total_due_at_delivery is not None
            else None
        ),
        "card_sent": True,
        "card_note": (
            "The figures have already been sent to the customer as a separate "
            "message, deposit line included. Write the sentence around it — do not "
            "repeat the breakdown, and do not restate the total as if it were the "
            "only amount."
        ),
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
    """Turn an accepted quote into a booking, through the booking provider.

    Quote-only by design: there is no path to a booked total the engine did not
    calculate and the customer did not see.

    The provider decides whether a booking exists. Nothing here writes a
    reservation of its own, and nothing here treats local state as evidence that
    one was made — which is what makes swapping in Delta's system a new class
    rather than a rewrite of this function.
    """
    customer_id = customer_id or ctx.customer_id
    if not customer_id:
        return _error("no_customer", "No customer is associated with this conversation")

    # The customer's own words, before anything is written down. "First show me
    # the pictures" created a booking in testing; the address in the same
    # message was an answer to a question, not consent to book.
    if consent.defers_booking(_last_customer_message(ctx)):
        return _error("not_asked_for_yet", consent.GUIDANCE)

    stored = ctx.quotes.get(quote_id)
    if stored is None:
        return _error(
            "quote_not_found",
            f"No demo quote {quote_id}. Create one with create_demo_quote first.",
        )
    quote = QuoteModel.model_validate(stored.payload)

    # Ours to check, not the provider's: it is a rule about who may drive, and
    # it comes from configuration rather than from inventory.
    customer = ctx.customers.get(customer_id)
    vehicle = ctx.engine.get_vehicle(quote.vehicle_id)
    minimum_age = ctx.engine.minimum_age_for(vehicle.category)
    if customer is not None and customer.driver_age is not None and customer.driver_age < minimum_age:
        return _error(
            "driver_too_young",
            f"The minimum age for a {vehicle.category.value.replace('_', ' ')} is {minimum_age}",
            minimum_age=minimum_age,
        )

    provider = ctx.provider
    key = idempotency.derive_key(ctx, "provider.reserve", {"quote_id": quote_id})
    answer = provider.reserve(
        quote_id=quote_id, customer_ref=customer_id, idempotency_key=key
    )

    if answer.outcome is Outcome.REVISED_QUOTE and answer.reason == "expired":
        return _error(
            "quote_expired",
            f"Quote {quote_id} has expired. Re-quote before booking.",
            expired_at=quote.expires_at.isoformat(),
        )
    if answer.outcome is Outcome.REVISED_QUOTE:
        revised = answer.revised_quote
        return _error(
            "price_changed",
            "The price for those dates has changed since the quote. Show the customer "
            "the new total and ask whether to go ahead — do not book at the old one.",
            previous_total=str(revised.previous_total) if revised else None,
            total_charge=str(revised.total_charge) if revised else None,
            quote_id=revised.quote_id if revised else None,
        )
    if answer.outcome is Outcome.UNAVAILABLE:
        return _error(
            "vehicle_unavailable",
            answer.message or "That vehicle was taken while we were talking",
            vehicle_id=quote.vehicle_id,
            reason=answer.reason,
            hint="Call find_alternatives and offer the customer a substitute.",
        )
    if answer.outcome is Outcome.PROVIDER_UNAVAILABLE:
        return _error(
            "booking_system_unreachable",
            "The booking system could not be reached, so nothing was booked. Tell the "
            "customer you are having trouble completing it and will come back to them. "
            "Do not say it is booked and do not say it failed for good.",
        )
    if answer.outcome is Outcome.UNKNOWN:
        return _error(
            "booking_outcome_unknown",
            "The booking system did not answer, so it is not known whether the booking "
            "exists. Tell the customer it is still being confirmed and that you will "
            "come back to them. Do not claim success and do not try again — a second "
            "attempt could give them two cars.",
            idempotency_key=answer.idempotency_key,
        )

    reservation = ctx.reservations.get(answer.reference or "")
    if reservation is None:
        return _error(
            "booking_outcome_unknown",
            "The booking system reported a booking that cannot be read back.",
        )

    _update_state(
        ctx,
        reservation_id=reservation.reservation_id,
        selected_vehicle_id=reservation.vehicle_id,
        stage=Stage.RESERVED,
    )

    # A provider that cannot settle availability leaves every booking a request,
    # and an operator may want a person to confirm even one that can. Either way
    # the reservation exists and is downgraded here rather than never made, so
    # the audit trail shows what the provider actually did.
    wants_person = not getattr(provider, "authoritative", True) or holds_require_confirmation(ctx)
    if not wants_person:
        outcomes.mark_booked(ctx)
        payload = _reservation_dict(reservation, ctx)
        payload["provider"] = getattr(provider, "name", "unknown")
        return payload

    reservation.status = ReservationStatus.HELD.value
    ctx.reservations.append_history(
        reservation, {"event": "awaiting_confirmation", "provider": provider.name}, ctx.now()
    )
    escalation.escalate_conversation(
        ctx,
        reason="booking_hold",
        detail=(
            f"{reservation.reservation_id}: {vehicle.display_name} "
            f"{reservation.pickup_at:%a %d %b %H:%M} to {reservation.return_at:%a %d %b %H:%M}"
            + (f", delivery {reservation.delivery_location}" if reservation.delivery_location else "")
        ),
    )
    payload = _reservation_dict(reservation, ctx)
    payload["provider"] = getattr(provider, "name", "unknown")
    payload["status"] = ReservationStatus.HELD.value
    payload["awaiting_confirmation"] = True
    payload["guidance"] = (
        "This is a REQUEST, not a confirmed booking. Tell them it is awaiting "
        "confirmation and that you will come straight back. Do NOT say it is booked, "
        "confirmed, reserved, secured or theirs, and do not say the car is being held "
        "or kept — nothing is holding it off the market. Give them the reference so "
        "they have something to refer to."
    )
    return payload


def holds_require_confirmation(ctx: ToolContext) -> bool:
    """Whether a booking has to be confirmed by a person before it is one."""
    return bool(ctx.engine.rules.get("booking", {}).get("holds_require_confirmation", False))


def _last_customer_message(ctx: ToolContext) -> str | None:
    if ctx.session is None or not ctx.conversation_id:
        return None
    inbound = [
        m.content for m in ctx.messages.for_conversation(ctx.conversation_id)
        if m.direction == "inbound"
    ]
    return inbound[-1] if inbound else None


def hold_expires_after(ctx: ToolContext) -> timedelta | None:
    """How long a hold stays the customer's booking without an answer."""
    minutes = ctx.engine.rules.get("booking", {}).get("hold_expires_after_minutes")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    return timedelta(minutes=minutes) if minutes > 0 else None


def live_hold(ctx: ToolContext, customer_id: str | None = None) -> Reservation | None:
    """The customer's hold, if one is still inside its window.

    A hold that ran out of time is not the customer's booking any more, whether
    or not a sweep has got to it yet. Every reader goes through here so that is
    true in the simulator and the chat harness too, which have no webhook to run
    the sweep on.
    """
    customer_id = customer_id or ctx.customer_id
    if ctx.session is None or not customer_id:
        return None
    window = hold_expires_after(ctx)
    cutoff = ctx.now() - window if window else None
    return ctx.reservations.held_for_customer(customer_id, taken_after=cutoff)


def expire_holds(ctx: ToolContext, now: datetime | None = None) -> list[str]:
    """Release holds nobody answered in time.

    Runs on inbound webhooks beside the dropped-conversation sweep, for the same
    reason that one does: there is no scheduler, and every message is a chance
    to notice something that became true while nothing was happening.

    Cancelled rather than left held, because a hold the owner never answered is
    not a booking and must not read as one to the outcome sweep, to the agent,
    or to the guard that stops the agent calling it confirmed.

    The owner answering late is still honoured — `apply_hold_decision` revives a
    hold the clock released. Expiry exists so the agent stops treating an
    unanswered request as current, not to overrule the one person who can
    actually see the car.

    Nothing is said to the customer here. They were told at the case timeout,
    which is far shorter, and a second message two hours later about a booking
    they may already have given up on is noise — and may need a template.
    """
    if ctx.session is None:
        return []
    window = hold_expires_after(ctx)
    if window is None:
        return []

    now = now or ctx.now()
    released: list[str] = []
    for reservation in ctx.reservations.holds_taken_before(now - window):
        reservation.status = ReservationStatus.CANCELLED.value
        reservation.history = list(reservation.history or []) + [
            {"event": "hold_expired", "at": now.isoformat()}
        ]
        reservation.updated_at = now
        released.append(reservation.reservation_id)
    if released:
        ctx.session.flush()
    return released


#: What backs a claim that the booking is done.
CONFIRMED = "confirmed"
#: Taken, but nobody who can see the fleet has said the car is free.
AWAITING = "awaiting"
#: No booking at all. The worse of the two, and the easier one to say by accident.
NOTHING = "nothing"


def confirmation_backing(ctx: ToolContext, references: frozenset[str] = frozenset()) -> str:
    """Whether anything supports telling this customer their booking is done.

    Answers about the booking the reply names, when it names one. A customer can
    have a confirmed booking and a held one at the same time, and a sentence
    about the confirmed one is true — refusing it because a different car is
    still being checked would be the guard doing harm of its own.

    A reference that resolves to nothing is ignored rather than trusted: quote
    codes have the same shape, and an invented code should fall back to what the
    customer actually has, not license a claim about a booking nobody made.
    """
    if ctx.session is None:
        return NOTHING

    named = [r for r in (ctx.reservations.get(ref) for ref in references) if r is not None]
    if named:
        if all(r.status == ReservationStatus.CONFIRMED.value for r in named):
            return CONFIRMED
        return AWAITING if any(r.status == ReservationStatus.HELD.value for r in named) else NOTHING

    if not ctx.customer_id:
        return NOTHING

    confirmed = ctx.reservations.confirmed_for_customer(ctx.customer_id)
    waiting = live_hold(ctx)
    if confirmed is not None:
        # One car confirmed and another still being checked, and a sentence that
        # names neither. "You're all set" is true of one booking and false of the
        # other, and the customer cannot tell which they were told — which is the
        # harm the whole guard exists for. Naming the reference resolves it.
        return AWAITING if waiting is not None else CONFIRMED
    return AWAITING if waiting is not None else NOTHING


def _hold_awaiting(ctx: ToolContext, reservation_id: str) -> Reservation | None:
    """The booking under this reference, if it is a hold still inside its window."""
    reservation = ctx.reservations.get(reservation_id)
    if reservation is None or reservation.status != ReservationStatus.HELD.value:
        return None
    if not _owned_by_this_customer(ctx, reservation):
        return None
    window = hold_expires_after(ctx)
    if window is not None and ctx.now() - reservation.created_at >= window:
        return None
    return reservation


def _note_change_on_open_case(ctx: ToolContext, reservation: Reservation, wanted: str) -> None:
    """Put the new request in front of whoever is being asked about the old one.

    Without this the owner confirms the window they were sent, which is no
    longer the one the customer wants.
    """
    from . import handover

    case = ctx.escalations.latest_for_conversation(reservation.conversation_id or "")
    if case is None or case.reason != "booking_hold" or case.status not in handover.OPEN_STATUSES:
        return
    case.question = f"{case.question or ''}\n↻ Customer has since asked for: {wanted}".strip()
    ctx.session.flush()


def _hold_change_request(
    ctx: ToolContext, reservation: Reservation, requested: dict[str, Any]
) -> dict[str, Any]:
    """Record a change to a booking nobody has confirmed, without applying it.

    The customer says "make it 8 instead" and the agent has to do something
    other than fail: the request is real, and the booking it is about is real,
    but there is nothing to change yet because there is nothing confirmed. So it
    is written down, the person being asked about the car is told, and the
    customer is told the truth — the request stands, unconfirmed, at the new
    time.

    Deliberately not an edit. Applying it would quietly grant a held booking the
    one right it does not have, and the owner would then confirm a window nobody
    checked.
    """
    wanted = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
              for k, v in requested.items() if v is not None}
    if not wanted:
        return _error("invalid_request", "No change was requested")

    now = ctx.now()
    reservation.history = list(reservation.history or []) + [
        {"event": "hold_change_requested", "at": now.isoformat(), **wanted}
    ]
    reservation.updated_at = now
    _note_change_on_open_case(
        ctx, reservation, ", ".join(f"{k} {v}" for k, v in wanted.items())
    )
    ctx.session.flush()
    return {
        "applied": False,
        "reservation_id": reservation.reservation_id,
        "status": reservation.status,
        "awaiting_confirmation": True,
        "requested_change": wanted,
        "guidance": (
            "Recorded against the booking request, not applied — this booking is still "
            "awaiting confirmation, so there is nothing confirmed to change. The person "
            "checking the car has been told about the new request. Tell the customer you "
            "have noted the change and that the booking request is still awaiting "
            "confirmation. Do not say the new time is booked, held or secured."
        ),
    }


#: Events that end a request one way or another. The last one wins.
_TERMINAL_HOLD_EVENTS = ("hold_confirmed", "hold_released", "hold_expired", "hold_withdrawn")


def _last_hold_event(reservation: Reservation) -> str | None:
    for entry in reversed(list(reservation.history or [])):
        if entry.get("event") in _TERMINAL_HOLD_EVENTS:
            return entry.get("event")
    return None


def expired_by_clock(reservation: Reservation) -> bool:
    """Whether the clock released this request, rather than a person.

    Three things end in `cancelled` and must not be treated alike: a request the
    owner declined is a car that is not free, one the customer withdrew is a
    question nobody needs answered, and one that merely ran out of time is still
    worth an answer if the owner gets to it.
    """
    return (
        reservation.status == ReservationStatus.CANCELLED.value
        and _last_hold_event(reservation) == "hold_expired"
    )


def withdrawn_by_customer(reservation: Reservation) -> bool:
    """Whether the customer pulled this request before anyone confirmed it."""
    return _last_hold_event(reservation) == "hold_withdrawn"


def pending_change(reservation: Reservation) -> dict[str, Any]:
    """What the customer has asked to change since the request went to a person.

    Accumulated rather than taken from the newest entry alone: a customer who
    moves the time and then the pickup point has asked for both.
    """
    wanted: dict[str, Any] = {}
    for entry in reservation.history or []:
        event = entry.get("event")
        if event == "hold_change_requested":
            for field in ("pickup_at", "return_at", "delivery_location", "vehicle_id"):
                if entry.get(field) is not None:
                    wanted[field] = entry[field]
        elif event in _TERMINAL_HOLD_EVENTS:
            wanted = {}
    return wanted


def _as_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw) if isinstance(raw, str) else raw


def confirm_hold(ctx: ToolContext, reservation: Reservation) -> dict[str, Any]:
    """Turn a request into a booking, at the details it stands at now.

    Everything is rechecked here rather than trusted from when the request was
    taken. Three things can have moved in between, and an approval that skipped
    any of them would confirm a booking nobody priced for a window nobody
    checked:

    * the customer may have asked for a different time, car or pickup point —
      recorded against the request but deliberately not applied until now;
    * another booking may have been confirmed on the same car, which is the only
      thing standing between two approvals and a double booking, since a
      request holds nothing off the market;
    * the price of the new window is not the price of the old one, and the
      stored quote may have expired.
    """
    wanted = pending_change(reservation)
    pickup = _as_datetime(wanted.get("pickup_at")) or reservation.pickup_at
    ret = _as_datetime(wanted.get("return_at")) or reservation.return_at
    vehicle_id = wanted.get("vehicle_id") or reservation.vehicle_id
    location = wanted.get("delivery_location", reservation.delivery_location)

    if ret <= pickup:
        return {
            "outcome": "conflict",
            "reason": "invalid_window",
            "message": "The requested return time is not after the pickup time.",
        }

    # Through the provider: it is the thing that decides whether a car is free,
    # and confirming is the moment that matters most for that to be true.
    try:
        answer = ctx.provider.check_availability(vehicle_id, pickup, ret)
    except (VehicleNotFound, ValueError) as exc:
        return {"outcome": "conflict", "reason": "vehicle_unknown", "message": str(exc)}
    if not answer.usable:
        return {
            "outcome": "conflict", "reason": "provider_unavailable",
            "message": "The booking system could not be reached to check the car.",
        }

    availability = answer
    if not availability.available:
        return {
            "outcome": "conflict",
            "reason": availability.reason or "unavailable",
            "message": (
                "That car is already committed for part of this period — confirming it "
                "would double-book it."
            ),
        }

    try:
        quote = _price(
            ctx,
            vehicle_id=vehicle_id,
            pickup_at=pickup,
            return_at=ret,
            delivery_location=location,
            discount_percent=Decimal("0"),
            quote_id=ctx.counters.next_quote_reference(),
            exclude_reservation_id=reservation.reservation_id,
        )
    except (ValueError, VehicleNotFound, VehicleUnavailable) as exc:
        return {"outcome": "conflict", "reason": "cannot_price", "message": str(exc)}

    _persist_quote(ctx, quote)
    now = ctx.now()
    previous_total = reservation.total_charge

    reservation.pickup_at = pickup
    reservation.return_at = ret
    reservation.delivery_location = location
    reservation.vehicle_id = vehicle_id
    reservation.total_charge = quote.total_charge
    reservation.deposit = quote.deposit
    reservation.quote_id = quote.quote_id
    reservation.status = ReservationStatus.CONFIRMED.value
    reservation.version += 1
    ctx.reservations.append_history(
        reservation,
        {
            "event": "hold_confirmed",
            "applied": wanted or None,
            "total_charge": str(quote.total_charge),
        },
        now,
    )
    outcomes.record(ctx, outcomes.BOOKED, conversation_id=reservation.conversation_id)
    _update_state(ctx, pickup_at=pickup, return_at=ret, delivery_location=location)

    vehicle = ctx.engine.get_vehicle(vehicle_id)
    return {
        "outcome": "confirmed",
        "status": reservation.status,
        "applied_change": wanted,
        "price_changed": quote.total_charge != previous_total,
        "previous_total": str(previous_total),
        "total_charge": str(quote.total_charge),
        "summary": (
            f"{reservation.reservation_id}: {vehicle.display_name} — confirmed for "
            f"{pickup:%a %d %b %H:%M} to {ret:%a %d %b %H:%M}"
            + (f", delivery {location}" if location else "")
            + f", {quote.currency} {quote.total_charge}"
        ),
    }


def withdraw_hold(
    ctx: ToolContext, reservation: Reservation, reason: str | None = None
) -> dict[str, Any]:
    """The customer pulling a request before anybody confirmed it.

    No cancellation fee and no band: the fee ladder prices a booking somebody
    had, and nobody had this. It also closes the question in front of the owner,
    so they are not asked about a car the customer no longer wants — and so an
    approval arriving afterwards cannot bring it back.
    """
    from . import handover

    now = ctx.now()
    reservation.status = ReservationStatus.CANCELLED.value
    reservation.version += 1
    ctx.reservations.append_history(
        reservation, {"event": "hold_withdrawn", "reason": reason}, now
    )

    case = ctx.escalations.latest_for_conversation(reservation.conversation_id or "")
    closed = None
    if case is not None and case.reason == "booking_hold" and case.status in handover.OPEN_STATUSES:
        handover.close_case(ctx, case, "the customer withdrew the request")
        closed = case.case_code

    _update_state(ctx, reservation_id=None)
    ctx.session.flush()

    result = _reservation_dict(reservation, ctx)
    result.update(
        {
            "withdrawn": True,
            "was_confirmed": False,
            "cancellation_fee": "0.00",
            "cancellation_band": "not_confirmed",
            "owner_case_closed": closed,
            "guidance": (
                "The request is withdrawn and nothing was charged — there is no "
                "cancellation fee because nothing was ever confirmed. Say so plainly, "
                "and offer to help with anything else."
            ),
        }
    )
    return result


def unsent_change_notices(ctx: ToolContext) -> list[dict[str, Any]]:
    """Changes recorded against a live request that the owner has not been told.

    The owner was messaged when the request was taken, with the window it was
    taken for. If the customer then moves it, an approval against the old
    message would confirm a time nobody asked for any more — so the change has
    to reach them before their answer does.
    """
    from . import handover

    held = live_hold(ctx)
    if held is None:
        return []

    case = ctx.escalations.latest_for_conversation(held.conversation_id or "")
    if case is None or case.reason != "booking_hold" or case.status not in handover.OPEN_STATUSES:
        return []

    notices: list[dict[str, Any]] = []
    history = list(held.history or [])
    for entry in history:
        if entry.get("event") == "hold_change_requested" and not entry.get("notified"):
            entry["notified"] = True
            notices.append(
                {
                    "case_code": case.case_code,
                    "reservation_id": held.reservation_id,
                    "wanted": ", ".join(
                        f"{k.replace('_', ' ')} {v}"
                        for k, v in entry.items()
                        if k not in {"event", "at", "notified"} and v is not None
                    ),
                }
            )
    if notices:
        held.history = history
        ctx.session.flush()
    return notices


def apply_hold_decision(
    ctx: ToolContext, reservation_id: str, outcome: str
) -> dict[str, Any] | None:
    """Turn the owner's answer into the request's actual state.

    Returns what happened rather than just the new status, because approval is
    no longer a status change: it rechecks the car, applies whatever the
    customer asked for in the meantime, and can fail. The caller needs to know
    which of those it got so the customer is told the truth.

    `None` means there was nothing to decide — no such request, or an outcome
    that is not a decision about the car.
    """
    reservation = ctx.reservations.get(reservation_id)
    if reservation is None:
        return None

    if withdrawn_by_customer(reservation):
        # The customer pulled it while the owner was deciding. Their answer is
        # about a request that no longer exists, and must not resurrect it.
        return {
            "outcome": "withdrawn",
            "status": reservation.status,
            "message": "The customer withdrew this request before it was confirmed.",
        }

    revived = expired_by_clock(reservation)
    if reservation.status != ReservationStatus.HELD.value and not revived:
        return None

    if outcome == "approved":
        result = confirm_hold(ctx, reservation)
        result["revived"] = revived
        ctx.session.flush()
        return result

    if outcome == "declined":
        now = ctx.now()
        reservation.status = ReservationStatus.CANCELLED.value
        ctx.reservations.append_history(reservation, {"event": "hold_released"}, now)
        ctx.session.flush()
        return {
            "outcome": "released",
            "status": reservation.status,
            "message": "The car is not available.",
        }

    # "I'll call them" decides nothing about the car, and the request stays as
    # it is until somebody does.
    return None


def modify_demo_reservation(
    ctx: ToolContext,
    *,
    reservation_id: str,
    pickup_at: datetime | None = None,
    return_at: datetime | None = None,
    delivery_location: str | None = None,
    vehicle_id: str | None = None,
) -> dict[str, Any]:
    held = _hold_awaiting(ctx, reservation_id)
    if held is not None:
        return _hold_change_request(ctx, held, {
            "pickup_at": pickup_at,
            "return_at": return_at,
            "delivery_location": delivery_location,
            "vehicle_id": vehicle_id,
        })

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
    held = _hold_awaiting(ctx, reservation_id)
    if held is not None:
        return _hold_change_request(ctx, held, {"return_at": new_return_at})

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

    # A request nobody confirmed is withdrawn, not cancelled: there is no
    # booking to price a fee against, and the person being asked about the car
    # has to be told to stop.
    unconfirmed = _hold_awaiting(ctx, reservation_id)
    if unconfirmed is not None:
        return withdraw_hold(ctx, unconfirmed, reason)

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

    # A returning customer is greeted as one *once*. The flag was true on every
    # turn, so the agent welcomed them back in the middle of a conversation it
    # was already having — twice in one thread, observed in testing. Having
    # already replied here is what makes this no longer a greeting.
    spoken = any(
        m.direction == "outbound"
        for m in ctx.messages.for_conversation(ctx.conversation_id or "")
    ) if ctx.session is not None and ctx.conversation_id else False

    return {
        "customer_id": customer.customer_id,
        "name": customer.name,
        "residency": customer.residency,
        "driver_age": customer.driver_age,
        "documents_on_file": list(customer.documents_on_file or []),
        "preferences": dict(customer.preferences or {}),
        "is_returning_customer": bool(reservations) and not spoken,
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
    if reservation is not None:
        return {
            "has_active_reservation": True,
            "reservation": _reservation_dict(reservation, ctx),
        }

    # A hold is deliberately not a live booking — it cannot be paid for or given
    # a delivery slot until somebody confirms the car. It still has to be
    # visible, or "can you make it 8 instead?" reaches an agent that believes
    # the customer has nothing at all.
    held = live_hold(ctx)
    if held is not None:
        return {
            "has_active_reservation": False,
            "reservation": None,
            "held_reservation": _reservation_dict(held, ctx),
            "awaiting_confirmation": True,
            "guidance": (
                "This booking request is awaiting confirmation — nobody has confirmed the "
                "vehicle yet, and nothing is holding it off the market. Refer to it by its "
                "reference and never as booked, held, reserved or theirs. If they ask to "
                "change it, call modify_demo_reservation as normal: the change will be "
                "recorded against the request and passed to the person confirming it."
            ),
        }

    return {"has_active_reservation": False, "reservation": None}
