"""Which provider is in use, decided by configuration rather than by code."""

from __future__ import annotations

import os

from .base import PaymentError, PaymentLink, PaymentProvider
from .simulated import SimulatedProvider

__all__ = ["PaymentError", "PaymentLink", "PaymentProvider", "build_provider"]


def build_provider(name: str | None = None) -> PaymentProvider:
    """Simulated unless told otherwise, so nothing takes money by accident."""
    chosen = (name or os.getenv("PAYMENT_PROVIDER", "simulated")).lower()
    if chosen == "stripe":
        from .stripe import StripeProvider

        return StripeProvider()
    if chosen == "simulated":
        return SimulatedProvider()
    raise PaymentError(f"Unknown payment provider: {chosen}")
