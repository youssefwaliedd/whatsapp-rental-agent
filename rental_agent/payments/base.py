"""Payment providers, behind one interface.

Their brief asks for a link generated from the quoted price. The operator today
takes payment on collection — invoice, transfer, cash, card at handover — so this
may end up used for the holding payment they already take to secure a car, or not
at all. Either way it lives behind an interface, because the UAE market is Telr,
Network International and PayTabs as much as Stripe, and swapping one for another
should be a new file rather than a rewrite.

**No caller supplies an amount.** Everything here is handed a reservation, and
the figure comes from the quote the engine calculated and stored. A parameter for
an amount is a parameter a model could one day fill in, which is the failure this
whole system is built to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PaymentLink:
    """Somewhere for the customer to pay, and what it is for."""

    url: str
    reference: str
    amount: Decimal
    currency: str
    purpose: str
    is_demo: bool = True
    expires_at: int | None = None


class PaymentError(Exception):
    """The provider could not create a link. Never raised past the tool layer —
    a customer waiting to pay must be told something useful, not shown a stack
    trace."""


class PaymentProvider(Protocol):
    name: str

    def create_link(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        description: str,
        metadata: dict[str, str] | None = None,
    ) -> PaymentLink: ...
