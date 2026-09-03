"""A booking system we control, standing in for one we cannot see.

Authoritative on purpose. Delta publishes availability nowhere, so the prototype
would otherwise have to treat every booking as a request somebody confirms by
hand — which is honest, and makes the whole booking journey impossible to
demonstrate. This provider *is* the inventory: it knows what is free because it
decides what is free, and it can therefore confirm a booking the moment a
customer accepts one.

What it must not do is make that look like more than it is. Every reservation it
creates is marked a demonstration, and `is_demonstration` travels on the answer
rather than being assumed by whatever reads it, so a real provider cannot
inherit the flag by accident.

Built on the engine and repositories that were already here. Pricing, blocked
windows, the fee ladder and the audit trail are the same ones the agent has been
using; this adds the one thing they lacked, which is a single place that decides
whether a booking exists.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ..context import ToolContext
from ..domain.enums import ReservationStatus
from ..domain.models import Quote as QuoteModel
from ..engine.engine import VehicleNotFound, VehicleUnavailable
from ..store.models import BookingOperation, Reservation
from .results import (
    AvailabilityAnswer,
    Outcome,
    QuoteAnswer,
    ReservationAnswer,
)

_log = logging.getLogger("rental_agent.booking_provider")

#: Statuses that hold a vehicle off the market here.
BLOCKING = (ReservationStatus.PENDING.value, ReservationStatus.CONFIRMED.value)

#: How a booking got here. `chat` is the agent; the rest stand in for the phone
#: calls and walk-ins that a real system would also be taking.
CHANNEL_CHAT = "chat"
CHANNEL_EXTERNAL = "external"


class SimulatedProvider:
    """The demo booking system. Authoritative for demo inventory."""

    name = "simulated"
    authoritative = True
    is_demonstration = True

    def __init__(self, ctx: ToolContext, *, fail_next: str | None = None):
        self.ctx = ctx
        #: Set by the demo scenarios to make the provider misbehave on the next
        #: write: "unavailable", "timeout", "down". Cleared once it has fired.
        self.fail_next = fail_next

    # -- reading -----------------------------------------------------------

    def check_availability(
        self, vehicle_id: str, pickup_at: datetime, return_at: datetime
    ) -> AvailabilityAnswer:
        try:
            result = self.ctx.engine.check_availability(vehicle_id, pickup_at, return_at)
        except VehicleNotFound:
            return AvailabilityAnswer(
                vehicle_id=vehicle_id, available=False, reason="unknown_vehicle",
                message=f"No vehicle {vehicle_id} in this fleet.",
            )
        return AvailabilityAnswer(
            vehicle_id=vehicle_id,
            available=result.available,
            reason=result.reason.value if result.reason else None,
            next_available_from=result.next_available_from,
        )

    def quote(
        self,
        *,
        vehicle_id: str,
        pickup_at: datetime,
        return_at: datetime,
        delivery_location: str | None = None,
        discount_percent: Decimal | None = None,
        excluding: str | None = None,
    ) -> QuoteAnswer:
        """Price a window.

        `excluding` leaves one booking out of the availability check — required
        when re-pricing a change, which would otherwise collide with the very
        booking being changed and be refused for dates the customer already has.
        """
        try:
            quote = self.ctx.engine_excluding(excluding).calculate_quote(
                vehicle_id=vehicle_id,
                pickup_at=pickup_at,
                return_at=return_at,
                delivery_location=delivery_location,
                discount_percent=discount_percent or Decimal("0"),
                quote_id=self.ctx.counters.next_quote_reference(),
            )
        except VehicleUnavailable as exc:
            return QuoteAnswer(
                outcome=Outcome.UNAVAILABLE, vehicle_id=vehicle_id, message=str(exc)
            )
        except (VehicleNotFound, ValueError) as exc:
            return QuoteAnswer(
                outcome=Outcome.UNAVAILABLE, vehicle_id=vehicle_id, message=str(exc)
            )

        self._store_quote(quote)
        return self._quote_answer(Outcome.CONFIRMED, quote)

    def validate_quote(self, quote_id: str) -> QuoteAnswer:
        """Whether a quote still stands, at the price the customer was shown.

        Two ways it may not. It can have run out of time, and the window can
        have been priced differently since — a longer stay, a changed rate, a
        discount that has gone. Re-pricing rather than trusting the stored total
        is what makes a revised quote possible to notice at all.
        """
        stored = self.ctx.quotes.get(quote_id)
        if stored is None:
            return QuoteAnswer(outcome=Outcome.UNAVAILABLE, message=f"No quote {quote_id}.")

        held = QuoteModel.model_validate(stored.payload)
        if held.expires_at <= self.ctx.now():
            return QuoteAnswer(
                outcome=Outcome.REVISED_QUOTE,
                quote_id=quote_id,
                vehicle_id=held.vehicle_id,
                previous_total=held.total_charge,
                reason="expired",
                message="That quote has expired and needs re-pricing.",
            )

        fresh = self.quote(
            vehicle_id=held.vehicle_id,
            pickup_at=held.pickup_at,
            return_at=held.return_at,
            delivery_location=held.delivery_location,
        )
        if fresh.outcome is not Outcome.CONFIRMED:
            return fresh
        if fresh.total_charge != held.total_charge:
            return QuoteAnswer(
                outcome=Outcome.REVISED_QUOTE,
                quote_id=fresh.quote_id,
                vehicle_id=held.vehicle_id,
                total_charge=fresh.total_charge,
                deposit=fresh.deposit,
                previous_total=held.total_charge,
                expires_at=fresh.expires_at,
                reason="price_changed",
                message="The price for those dates has changed since the quote.",
                payload=fresh.payload,
            )
        return self._quote_answer(Outcome.CONFIRMED, held)

    def get_reservation(self, reference: str, *, customer_ref: str) -> ReservationAnswer:
        reservation = self.ctx.reservations.get(reference)
        if reservation is None or not self._owned_by(reservation, customer_ref):
            # Deliberately the same answer either way. Telling a stranger that a
            # reference exists but is not theirs confirms it exists.
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reference=reference, reason="not_found",
                message=f"No booking {reference} for this customer.",
            )
        return self._answer(reservation)

    # -- writing -----------------------------------------------------------

    def reserve(
        self, *, quote_id: str, customer_ref: str, idempotency_key: str
    ) -> ReservationAnswer:
        replay = self._replay(idempotency_key)
        if replay is not None:
            return replay

        if not quote_id:
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reason="quote_not_found",
                message="A booking needs a quote the customer has seen.",
            )
        stored = self.ctx.quotes.get(quote_id)
        if stored is None:
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reason="quote_not_found",
                message=f"No quote {quote_id}.",
            )
        quote = QuoteModel.model_validate(stored.payload)

        # The price the customer accepted is the one they are charged. A quote
        # that has moved since goes back to them rather than through.
        checked = self.validate_quote(quote_id)
        if checked.outcome is Outcome.REVISED_QUOTE:
            return ReservationAnswer(
                outcome=Outcome.REVISED_QUOTE, reason=checked.reason or "price_changed",
                message=checked.message, revised_quote=checked,
            )

        operation = self._begin(idempotency_key, "reserve", customer_ref,
                                detail={"quote_id": quote_id})

        injected = self._injected_failure(operation)
        if injected is not None:
            return injected

        # Check and take in one go. Locking the vehicle's live bookings is what
        # stops two customers confirming the same car for overlapping dates:
        # without it both read "free" and both insert.
        conflict = self._conflicting(quote.vehicle_id, quote.pickup_at, quote.return_at)
        if conflict is not None:
            self._settle(operation, Outcome.UNAVAILABLE, detail={"conflict": conflict})
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reason="vehicle_taken",
                vehicle_id=quote.vehicle_id,
                message="That vehicle was taken for part of those dates.",
            )

        now = self.ctx.now()
        reservation = self.ctx.reservations.create(
            reservation_id=self.ctx.counters.next_reservation_reference(),
            is_demo=True,
            customer_id=customer_ref,
            conversation_id=self.ctx.conversation_id,
            vehicle_id=quote.vehicle_id,
            quote_id=quote.quote_id,
            status=ReservationStatus.CONFIRMED.value,
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
            history=[{
                "event": "created", "quote_id": quote.quote_id,
                "channel": CHANNEL_CHAT, "provider": self.name, "at": now.isoformat(),
            }],
        )
        self.ctx.quotes.mark_converted(quote.quote_id)
        self._settle(operation, Outcome.CONFIRMED, reference=reservation.reservation_id)
        return self._answer(reservation, idempotency_key=idempotency_key)

    def modify_reservation(
        self, reference: str, *, customer_ref: str, idempotency_key: str,
        changes: dict[str, Any],
    ) -> ReservationAnswer:
        replay = self._replay(idempotency_key)
        if replay is not None:
            return replay

        reservation = self.ctx.reservations.get(reference)
        if reservation is None or not self._owned_by(reservation, customer_ref):
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reference=reference, reason="not_found",
                message=f"No booking {reference} for this customer.",
            )
        if reservation.status not in BLOCKING:
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reference=reference,
                reason="not_live", status=reservation.status,
                message=f"Booking {reference} is {reservation.status} and cannot be changed.",
            )

        pickup = changes.get("pickup_at") or reservation.pickup_at
        ret = changes.get("return_at") or reservation.return_at
        vehicle_id = changes.get("vehicle_id") or reservation.vehicle_id
        location = changes.get("delivery_location", reservation.delivery_location)
        if ret <= pickup:
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reference=reference, reason="invalid_window",
                message="The return time must be after the pickup time.",
            )

        operation = self._begin(idempotency_key, "modify", customer_ref,
                                detail={"reference": reference})
        injected = self._injected_failure(operation)
        if injected is not None:
            return injected

        conflict = self._conflicting(vehicle_id, pickup, ret, excluding=reference)
        if conflict is not None:
            self._settle(operation, Outcome.UNAVAILABLE, reference=reference)
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reference=reference, reason="vehicle_taken",
                message="The vehicle is committed for part of the new period.",
            )

        priced = self.quote(
            vehicle_id=vehicle_id, pickup_at=pickup, return_at=ret,
            delivery_location=location, excluding=reference,
        )
        if priced.outcome is not Outcome.CONFIRMED:
            self._settle(operation, priced.outcome, reference=reference)
            return ReservationAnswer(
                outcome=priced.outcome, reference=reference, message=priced.message
            )

        # A change that costs more is a new price the customer has not agreed
        # to. Nothing is applied until they do.
        if priced.total_charge > reservation.total_charge and not changes.get("price_accepted"):
            self._settle(operation, Outcome.REVISED_QUOTE, reference=reference)
            return ReservationAnswer(
                outcome=Outcome.REVISED_QUOTE, reference=reference, reason="price_increase",
                message="That change costs more than the booking they accepted.",
                revised_quote=QuoteAnswer(
                    outcome=Outcome.REVISED_QUOTE,
                    quote_id=priced.quote_id, vehicle_id=vehicle_id,
                    total_charge=priced.total_charge, deposit=priced.deposit,
                    previous_total=reservation.total_charge, expires_at=priced.expires_at,
                ),
            )

        before = {
            "pickup_at": reservation.pickup_at.isoformat(),
            "return_at": reservation.return_at.isoformat(),
            "vehicle_id": reservation.vehicle_id,
            "total_charge": str(reservation.total_charge),
        }
        reservation.pickup_at = pickup
        reservation.return_at = ret
        reservation.vehicle_id = vehicle_id
        reservation.delivery_location = location
        reservation.total_charge = priced.total_charge
        reservation.deposit = priced.deposit
        reservation.quote_id = priced.quote_id or reservation.quote_id
        reservation.version += 1
        self.ctx.reservations.append_history(
            reservation,
            {"event": "modified", "before": before, "provider": self.name},
            self.ctx.now(),
        )
        self._settle(operation, Outcome.CONFIRMED, reference=reference)
        return self._answer(reservation, idempotency_key=idempotency_key)

    def cancel_reservation(
        self, reference: str, *, customer_ref: str, idempotency_key: str,
        reason: str | None = None,
    ) -> ReservationAnswer:
        replay = self._replay(idempotency_key)
        if replay is not None:
            return replay

        reservation = self.ctx.reservations.get(reference)
        if reservation is None or not self._owned_by(reservation, customer_ref):
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reference=reference, reason="not_found",
                message=f"No booking {reference} for this customer.",
            )

        operation = self._begin(idempotency_key, "cancel", customer_ref,
                                detail={"reference": reference})
        injected = self._injected_failure(operation)
        if injected is not None:
            return injected

        if reservation.status != ReservationStatus.CANCELLED.value:
            reservation.status = ReservationStatus.CANCELLED.value
            reservation.version += 1
            self.ctx.reservations.append_history(
                reservation,
                {"event": "cancelled", "reason": reason, "provider": self.name},
                self.ctx.now(),
            )
        self._settle(operation, Outcome.CONFIRMED, reference=reference)
        return self._answer(reservation, idempotency_key=idempotency_key)

    def resolve(self, idempotency_key: str) -> ReservationAnswer:
        """What became of an operation whose answer never arrived."""
        operation = self._operation(idempotency_key)
        if operation is None:
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reason="no_such_operation",
                idempotency_key=idempotency_key,
                message="Nothing was ever sent under that key.",
            )
        if operation.outcome is None:
            # Started and never settled. In a real provider this is where we
            # would ask it; here the absence of a reservation is the answer.
            return ReservationAnswer(
                outcome=Outcome.UNKNOWN, idempotency_key=idempotency_key,
                message="That request was sent and never answered.",
            )
        if operation.reference:
            reservation = self.ctx.reservations.get(operation.reference)
            if reservation is not None:
                return self._answer(
                    reservation, idempotency_key=idempotency_key, replayed=True
                )
        return ReservationAnswer(
            outcome=Outcome(operation.outcome), idempotency_key=idempotency_key,
            reference=operation.reference, replayed=True,
        )

    # -- the demo's own lever ---------------------------------------------

    def record_external_booking(
        self, *, vehicle_id: str, pickup_at: datetime, return_at: datetime,
        who: str = "walk-in",
    ) -> ReservationAnswer:
        """A booking taken somewhere other than this conversation.

        The phone call and the walk-in a real rental company also takes, and the
        reason an availability check is only true for as long as nobody else
        acts. Nothing about the agent's path knows this happened.
        """
        conflict = self._conflicting(vehicle_id, pickup_at, return_at)
        if conflict is not None:
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reason="vehicle_taken", vehicle_id=vehicle_id
            )
        now = self.ctx.now()
        # A booking taken elsewhere still belongs to somebody. One standing
        # customer per channel, so the phone's bookings are distinguishable from
        # the counter's and from the agent's.
        holder, _ = self.ctx.customers.get_or_create(f"external:{who}", now)
        reservation = self.ctx.reservations.create(
            reservation_id=self.ctx.counters.next_reservation_reference(),
            is_demo=True,
            customer_id=holder.customer_id,
            conversation_id=None,
            vehicle_id=vehicle_id,
            quote_id=None,
            status=ReservationStatus.CONFIRMED.value,
            pickup_at=pickup_at,
            return_at=return_at,
            delivery_location=None,
            total_charge=Decimal("0"),
            deposit=None,
            currency=self.ctx.engine.rules["currency"],
            payment_status="none",
            documents=[],
            created_at=now,
            updated_at=now,
            version=1,
            history=[{"event": "created", "channel": CHANNEL_EXTERNAL,
                      "who": who, "at": now.isoformat()}],
        )
        return self._answer(reservation)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _owned_by(reservation: Reservation, customer_ref: str) -> bool:
        return bool(customer_ref) and reservation.customer_id == customer_ref

    def _conflicting(
        self, vehicle_id: str, pickup_at: datetime, return_at: datetime,
        *, excluding: str | None = None,
    ) -> str | None:
        """A live booking overlapping this window, with the row locked.

        `with_for_update` is the whole point on Postgres: it serialises two
        confirmations of the same car so the second reads the first's row
        instead of a stale empty set. SQLite takes a write lock for the whole
        transaction and gets there another way.
        """
        session = self.ctx._require_session()
        statement = select(Reservation).where(
            Reservation.vehicle_id == vehicle_id,
            Reservation.status.in_(BLOCKING),
        )
        if excluding:
            statement = statement.where(Reservation.reservation_id != excluding)
        if session.bind is not None and session.bind.dialect.name != "sqlite":
            statement = statement.with_for_update()
        for existing in session.scalars(statement):
            if existing.pickup_at < return_at and pickup_at < existing.return_at:
                return existing.reservation_id
        return None

    def _operation(self, idempotency_key: str) -> BookingOperation | None:
        session = self.ctx._require_session()
        return session.scalar(
            select(BookingOperation).where(
                BookingOperation.idempotency_key == idempotency_key
            )
        )

    def _replay(self, idempotency_key: str) -> ReservationAnswer | None:
        """The same operation arriving twice is the same operation."""
        operation = self._operation(idempotency_key)
        if operation is None:
            return None
        if operation.outcome is None:
            return ReservationAnswer(
                outcome=Outcome.UNKNOWN, idempotency_key=idempotency_key,
                message="An earlier attempt under this key was never answered.",
            )
        return self.resolve(idempotency_key)

    def _begin(
        self, idempotency_key: str, kind: str, customer_ref: str,
        *, detail: dict[str, Any] | None = None,
    ) -> BookingOperation:
        session = self.ctx._require_session()
        operation = BookingOperation(
            idempotency_key=idempotency_key,
            provider=self.name,
            kind=kind,
            customer_ref=customer_ref,
            detail=detail or {},
            created_at=self.ctx.now(),
        )
        session.add(operation)
        session.flush()
        return operation

    def _settle(
        self, operation: BookingOperation, outcome: Outcome,
        *, reference: str | None = None, detail: dict[str, Any] | None = None,
    ) -> None:
        operation.outcome = outcome.value
        operation.settled_at = self.ctx.now()
        if reference:
            operation.reference = reference
        if detail:
            operation.detail = {**(operation.detail or {}), **detail}
        self.ctx._require_session().flush()

    def _injected_failure(self, operation: BookingOperation) -> ReservationAnswer | None:
        """The failures a real provider will eventually produce on its own."""
        mode, self.fail_next = self.fail_next, None
        if mode is None:
            return None
        if mode == "down":
            self._settle(operation, Outcome.PROVIDER_UNAVAILABLE)
            return ReservationAnswer(
                outcome=Outcome.PROVIDER_UNAVAILABLE,
                idempotency_key=operation.idempotency_key,
                message="The booking system could not be reached.",
            )
        if mode == "timeout":
            # Deliberately left unsettled: the operation is in flight as far as
            # anybody here knows, which is exactly the state `resolve` exists
            # for. The caller must reconcile rather than retry.
            return ReservationAnswer(
                outcome=Outcome.UNKNOWN,
                idempotency_key=operation.idempotency_key,
                message="The booking system did not answer in time.",
            )
        if mode == "unavailable":
            self._settle(operation, Outcome.UNAVAILABLE)
            return ReservationAnswer(
                outcome=Outcome.UNAVAILABLE, reason="vehicle_taken",
                idempotency_key=operation.idempotency_key,
                message="That vehicle was taken.",
            )
        return None

    def _store_quote(self, quote: QuoteModel) -> None:
        self.ctx.quotes.save(
            quote_id=quote.quote_id,
            vehicle_id=quote.vehicle_id,
            payload=quote.model_dump(mode="json"),
            total_charge=quote.total_charge,
            deposit=quote.deposit,
            created_at=quote.created_at,
            expires_at=quote.expires_at,
            conversation_id=self.ctx.conversation_id,
            customer_id=self.ctx.customer_id,
        )

    @staticmethod
    def _quote_answer(outcome: Outcome, quote: QuoteModel) -> QuoteAnswer:
        return QuoteAnswer(
            outcome=outcome,
            quote_id=quote.quote_id,
            vehicle_id=quote.vehicle_id,
            total_charge=quote.total_charge,
            deposit=quote.deposit,
            currency=quote.currency,
            expires_at=quote.expires_at,
            payload=quote.model_dump(mode="json"),
        )

    def _answer(
        self, reservation: Reservation, *, idempotency_key: str | None = None,
        replayed: bool = False,
    ) -> ReservationAnswer:
        return ReservationAnswer(
            outcome=Outcome.CONFIRMED
            if reservation.status == ReservationStatus.CONFIRMED.value
            else Outcome.AWAITING_CONFIRMATION
            if reservation.status == ReservationStatus.HELD.value
            else Outcome.UNAVAILABLE,
            reference=reservation.reservation_id,
            status=reservation.status,
            vehicle_id=reservation.vehicle_id,
            pickup_at=reservation.pickup_at,
            return_at=reservation.return_at,
            total_charge=reservation.total_charge,
            deposit=reservation.deposit,
            currency=reservation.currency,
            is_demonstration=True,
            idempotency_key=idempotency_key,
            replayed=replayed,
        )
