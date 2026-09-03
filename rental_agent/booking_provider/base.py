"""The booking system, as the conversation is allowed to see it.

Seven operations, no provider detail. Everything above this line — the tools the
model calls, the guards on the way out, the wording — is written against these
signatures, so replacing the system underneath is a new class and a config line
rather than a change to how the agent sells.

Two rules make that real rather than aspirational:

**The provider is authoritative.** Local state may remember what a customer was
told; it may never be the reason we believe a booking exists. Anything the
conversation asserts about a booking traces to an answer from here.

**Idempotency is the caller's to supply.** Every write takes a key the caller
derives from the operation, not from the moment — so a retried message, a
repeated tool call and a process that restarted mid-request all arrive as the
same operation, and `resolve` can find out what happened to it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from .results import AvailabilityAnswer, QuoteAnswer, ReservationAnswer


@runtime_checkable
class BookingProvider(Protocol):
    #: Short name, for logs, `/health` and the demonstration disclosure.
    name: str

    #: Whether this provider can settle availability itself. False means every
    #: booking it takes is a request somebody has to confirm — which is a
    #: property of the system, not a policy choice made in configuration.
    authoritative: bool

    #: Whether bookings taken through it are demonstrations.
    is_demonstration: bool

    def check_availability(
        self, vehicle_id: str, pickup_at: datetime, return_at: datetime
    ) -> AvailabilityAnswer:
        """Whether this vehicle can be had for this window."""

    def quote(
        self,
        *,
        vehicle_id: str,
        pickup_at: datetime,
        return_at: datetime,
        delivery_location: str | None = None,
        discount_percent: Decimal | None = None,
    ) -> QuoteAnswer:
        """Price the window, or say why it cannot be priced."""

    def validate_quote(self, quote_id: str) -> QuoteAnswer:
        """Whether a quote already given still stands.

        Returns REVISED_QUOTE when the price or terms have moved, so the caller
        can put the new figure to the customer instead of booking at it.
        """

    def reserve(
        self, *, quote_id: str, customer_ref: str, idempotency_key: str
    ) -> ReservationAnswer:
        """Turn an accepted quote into a booking.

        Must check and take inventory atomically: two customers confirming the
        same car for overlapping dates cannot both succeed.
        """

    def get_reservation(self, reference: str, *, customer_ref: str) -> ReservationAnswer:
        """Read a booking back. `customer_ref` is checked, not decorative."""

    def modify_reservation(
        self,
        reference: str,
        *,
        customer_ref: str,
        idempotency_key: str,
        changes: dict[str, Any],
    ) -> ReservationAnswer:
        """Change a booking, or report what stops the change."""

    def cancel_reservation(
        self, reference: str, *, customer_ref: str, idempotency_key: str,
        reason: str | None = None,
    ) -> ReservationAnswer:
        """Cancel a booking and release whatever it was holding."""

    def resolve(self, idempotency_key: str) -> ReservationAnswer:
        """What became of an operation whose answer never arrived.

        The one that makes a timeout survivable: an operation that succeeded and
        then timed out is found here rather than repeated, so a customer does
        not end up with two bookings for one car.
        """
