"""What a booking provider can answer, and nothing about how.

Every outcome the conversation has to handle differently gets its own name. The
list is deliberately not "ok / error": the difference between *this car is gone*
and *we do not know whether your booking exists* is the difference between
offering an alternative and telling a customer to wait, and a connector that
collapsed them would make that choice for the conversation.

`UNKNOWN` is the one that earns its place. A provider that accepted a booking
and then timed out has left a reservation that may or may not exist, and the
only safe thing to say is that it is still being checked. Claiming either
success or failure there is how a customer ends up with two cars or none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    #: The provider holds a booking and says it is confirmed.
    CONFIRMED = "confirmed"
    #: Taken, but somebody who can see the real fleet has still to agree. What a
    #: provider returns when it cannot confirm inventory itself.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    #: The car is not free for that window — including because somebody else
    #: took it between the quote and the booking.
    UNAVAILABLE = "unavailable"
    #: The provider will do it, at a different price or on different terms. The
    #: customer has to accept the new figure before anything is booked.
    REVISED_QUOTE = "revised_quote"
    #: The provider could not be reached. Nothing was decided.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The request may or may not have been carried out. Reconcile before
    #: telling the customer anything.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AvailabilityAnswer:
    vehicle_id: str
    available: bool
    outcome: Outcome = Outcome.CONFIRMED
    reason: str | None = None
    next_available_from: datetime | None = None
    message: str | None = None

    @property
    def usable(self) -> bool:
        """Whether this answer may be relied on to tell a customer anything."""
        return self.outcome is Outcome.CONFIRMED


@dataclass(frozen=True)
class QuoteAnswer:
    outcome: Outcome
    quote_id: str | None = None
    vehicle_id: str | None = None
    total_charge: Decimal | None = None
    deposit: Decimal | None = None
    currency: str = "AED"
    expires_at: datetime | None = None
    #: Set on REVISED_QUOTE: what the customer was last shown, so the
    #: conversation can say what changed rather than only what it now costs.
    previous_total: Decimal | None = None
    #: Why a quote was revised — "expired" or "price_changed". They read the
    #: same to a caller and not at all to a customer: one needs re-quoting, the
    #: other needs the new figure accepting.
    reason: str | None = None
    message: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReservationAnswer:
    outcome: Outcome
    #: The provider's own reference. Ours only where the provider is ours.
    reference: str | None = None
    status: str | None = None
    vehicle_id: str | None = None
    pickup_at: datetime | None = None
    return_at: datetime | None = None
    total_charge: Decimal | None = None
    deposit: Decimal | None = None
    currency: str = "AED"
    #: True while the booking is a demonstration. Carried on the answer rather
    #: than assumed by the caller, so a real provider cannot inherit it.
    is_demonstration: bool = True
    #: Echoed back so a retry can be recognised as the same operation.
    idempotency_key: str | None = None
    #: Set when this answer is a replay of an operation already carried out.
    replayed: bool = False
    reason: str | None = None
    message: str | None = None
    revised_quote: QuoteAnswer | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    @property
    def booked(self) -> bool:
        return self.outcome is Outcome.CONFIRMED

    @property
    def settled(self) -> bool:
        """Whether the provider has told us what actually happened."""
        return self.outcome not in (Outcome.UNKNOWN, Outcome.PROVIDER_UNAVAILABLE)


class ProviderNotConfigured(RuntimeError):
    """A real provider was selected and cannot run.

    Raised rather than degraded on purpose. Falling back to simulated
    availability when the real system is unreachable would answer a customer
    with invented inventory, which is the single failure this whole design
    exists to prevent.
    """
