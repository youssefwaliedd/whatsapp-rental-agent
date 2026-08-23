"""A payment link that goes nowhere, for a demonstration that takes no money.

Used whenever no provider is configured, which is every run until the operator
hands over keys. It behaves exactly like the real one — same shape, same
reference on the reservation — so the conversation, the wording and the tests are
the same ones that will run against Stripe.

The URL is deliberately not a plausible payment page. A demo link that looks real
enough to click is a demo link somebody eventually types a card into.
"""

from __future__ import annotations

from decimal import Decimal

from .base import PaymentLink

DEMO_HOST = "https://demo.invalid/pay"


class SimulatedProvider:
    name = "simulated"

    def create_link(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        description: str,
        metadata: dict[str, str] | None = None,
    ) -> PaymentLink:
        return PaymentLink(
            url=f"{DEMO_HOST}/{reference}",
            reference=reference,
            amount=amount,
            currency=currency,
            purpose=(metadata or {}).get("purpose", "unspecified"),
            is_demo=True,
        )
