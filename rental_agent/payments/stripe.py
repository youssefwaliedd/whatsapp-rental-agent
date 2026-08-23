"""Stripe Checkout, over the HTTP API rather than the SDK.

httpx is already a dependency and the surface used here is one endpoint, so the
SDK would be a package to install, pin and audit for a single POST.

Amounts go to Stripe in the currency's minor unit — fils for AED — as an integer.
Getting that wrong is the classic payment bug and it fails in the expensive
direction: a customer charged a hundred times the quote. So the conversion is one
function with one test per currency we accept.
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx

from .base import PaymentError, PaymentLink

API = "https://api.stripe.com/v1/checkout/sessions"

#: Currencies where Stripe expects the amount as a whole number rather than in
#: cents. AED is not one of them; this is here so a future one is a data change.
ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG",
                "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}


def minor_units(amount: Decimal, currency: str) -> int:
    """AED 1,887.90 -> 188790. The classic payment bug, isolated and tested."""
    if currency.upper() in ZERO_DECIMAL:
        return int(amount.quantize(Decimal("1")))
    return int((amount * 100).quantize(Decimal("1")))


class StripeProvider:
    name = "stripe"

    def __init__(self, api_key: str | None = None, *, timeout: float = 15.0) -> None:
        self.api_key = api_key or os.getenv("STRIPE_API_KEY", "")
        if not self.api_key:
            raise PaymentError("STRIPE_API_KEY is not set")
        self.timeout = timeout

    @property
    def is_live(self) -> bool:
        """Test keys start sk_test_. Worth knowing before a demo takes money."""
        return self.api_key.startswith("sk_live_")

    def create_link(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        description: str,
        metadata: dict[str, str] | None = None,
    ) -> PaymentLink:
        form = {
            "mode": "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(minor_units(amount, currency)),
            "line_items[0][price_data][product_data][name]": description[:250],
            "client_reference_id": reference,
            "success_url": os.getenv("PAYMENT_SUCCESS_URL", "https://example.com/paid"),
            "cancel_url": os.getenv("PAYMENT_CANCEL_URL", "https://example.com/cancelled"),
        }
        for key, value in (metadata or {}).items():
            form[f"metadata[{key}]"] = str(value)

        try:
            response = httpx.post(
                API, data=form, auth=(self.api_key, ""), timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise PaymentError(f"could not reach Stripe: {exc}") from exc

        if response.status_code >= 400:
            detail = response.json().get("error", {}).get("message", response.text[:200])
            raise PaymentError(f"Stripe refused the request: {detail}")

        body = response.json()
        url = body.get("url")
        if not url:
            raise PaymentError("Stripe returned a session with no payment page")
        return PaymentLink(
            url=url,
            reference=body.get("id", reference),
            amount=amount,
            currency=currency.upper(),
            purpose=(metadata or {}).get("purpose", "unspecified"),
            is_demo=not self.is_live,
        )
