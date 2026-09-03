"""Where Delta's own booking system will plug in.

Nothing here talks to anything. It exists so the shape of the work is written
down while it is still fresh, and so that selecting a real provider before it
exists fails loudly instead of quietly answering customers with demo inventory.

No endpoint, credential or capability below is claimed to exist. Delta has not
described a system to us; what they have said, in thirteen conversations, is
"please allow me a moment to check", which is a person looking at something we
cannot see. Everything here is a slot, not an assumption.

WHAT AN ADAPTER HAS TO IMPLEMENT

**Authentication.** Whatever they use — a key, OAuth, a session. Read from the
environment, never from config committed to the repository. Failure to
authenticate is `PROVIDER_UNAVAILABLE`, never a fallback to local data.

**Identity mapping.** Two directions, both needed:
  - vehicles: our `veh_01` against their identifier. Today our ids come from
    their public catalogue import (`sources/delta.py`), which is not necessarily
    the identifier their booking system uses.
  - customers: our `cus_...` against their customer record, if they keep one.
    A booking made for the wrong customer is worse than a booking refused.

**Availability reads.** `check_availability` must answer for one vehicle and one
window. If their system can only list everything, cache it here rather than
making the conversation wait — but see the warning below about staleness.

**Reservation writes.** `reserve` has to be atomic on their side, not ours. If
their API cannot guarantee that two simultaneous requests for one car do not
both succeed, that is a fact about their system that no adapter can fix, and it
must be reported rather than hidden: return `AWAITING_CONFIRMATION` and let a
person settle it.

**Quote validation.** Whether they price bookings themselves or accept ours. If
theirs, `validate_quote` must be able to detect that a price has moved and
return `REVISED_QUOTE`, because the customer accepted a figure and is entitled
to be asked again before being charged another.

**Idempotency and reconciliation.** The important one. If their API accepts an
idempotency key, pass ours through. If it does not, `resolve` must be able to
find a reservation by some reference we control — a customer reference, an
external id, a search by customer and window — or a timeout after a successful
write will produce a second booking for the same customer.

**Cancellation and modification.** Including what their fee rules are, which are
theirs and not ours to compute.

**Status mapping.** Their vocabulary onto `Outcome`. The mapping is a judgement:
anything that means "we have it and it is yours" is CONFIRMED; anything meaning
"received, pending" is AWAITING_CONFIRMATION; a refusal for the dates is
UNAVAILABLE. Anything unrecognised is UNKNOWN, never CONFIRMED.

IF THEY CAN ONLY GIVE US READ ACCESS, OR A DAILY EXPORT

Then automatic confirmation is not safely possible, and the honest answer is to
say so rather than build something that looks like it works.

A read-only or delayed source tells us what was true when it was produced. Their
staff take bookings by phone and at the counter; a car booked at 2pm is not in a
snapshot taken at 6am, and an agent confirming it at 3pm sends a customer to
collect a car that is already out. That is the single worst outcome a rental
business has, and it is the reason this prototype confirms nothing it cannot
settle.

With a stale source the correct behaviour is the one already built: take the
booking as a request, tell the customer it is awaiting confirmation, and put it
to a person who can see the real fleet. Set `authoritative = False` and that
happens by itself — every path above it already handles it.

Questions 23 and 24 of the policy questionnaire ask Delta exactly this: where
availability is checked, and whether we can read from it or receive it daily.
The answer decides which of these two systems they are buying.
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Any

from .results import ProviderNotConfigured, ReservationAnswer

#: Every environment variable an adapter would need before it could run. Named
#: here so a misconfiguration is one clear error rather than a timeout.
REQUIRED_SETTINGS = ("DELTA_BOOKING_BASE_URL", "DELTA_BOOKING_API_KEY")


def missing_settings() -> list[str]:
    return [name for name in REQUIRED_SETTINGS if not os.getenv(name)]


class DeltaProvider:
    """Not implemented. Selecting it is an error until it is.

    Every method raises. There is deliberately no partial implementation and no
    degraded mode: a connector that answered some questions from Delta and the
    rest from demo data would be the most dangerous thing in this repository.
    """

    name = "delta"
    authoritative = False
    is_demonstration = False

    def __init__(self, *_args: Any, **_kwargs: Any):
        raise ProviderNotConfigured(
            "BOOKING_PROVIDER=delta is selected and no adapter exists yet. "
            "Delta has not supplied a booking system to connect to; see "
            "rental_agent/booking_provider/delta.py for what an adapter must "
            "implement, and questions 23-24 of the policy questionnaire for what "
            "they still have to answer. Set BOOKING_PROVIDER=simulated to run the "
            "demonstration, which never claims to be their inventory."
            + (f" Missing settings: {', '.join(missing_settings())}." if missing_settings() else "")
        )

    def check_availability(self, vehicle_id: str, pickup_at: datetime, return_at: datetime): ...
    def quote(self, **kwargs: Any): ...
    def validate_quote(self, quote_id: str): ...
    def reserve(self, **kwargs: Any) -> ReservationAnswer: ...
    def get_reservation(self, reference: str, *, customer_ref: str): ...
    def modify_reservation(self, reference: str, **kwargs: Any): ...
    def cancel_reservation(self, reference: str, **kwargs: Any): ...
    def resolve(self, idempotency_key: str): ...
