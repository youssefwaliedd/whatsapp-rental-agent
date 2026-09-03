"""Choosing which booking system the agent is talking to.

One place, one environment variable, and no silent fallback. A real provider
that cannot run raises: answering a customer with demonstration availability
because the real system was unreachable is the failure this package exists to
make impossible.
"""

from __future__ import annotations

import os

from ..context import ToolContext
from .base import BookingProvider
from .results import (
    AvailabilityAnswer,
    Outcome,
    ProviderNotConfigured,
    QuoteAnswer,
    ReservationAnswer,
)
from .simulated import SimulatedProvider

#: What runs when nothing says otherwise. Named rather than implied, so a
#: demonstration is always something somebody chose.
DEFAULT_PROVIDER = "simulated"


def provider_name() -> str:
    return os.getenv("BOOKING_PROVIDER", DEFAULT_PROVIDER).strip().lower()


def build_provider(ctx: ToolContext, name: str | None = None) -> BookingProvider:
    """The booking system for this context.

    Raises `ProviderNotConfigured` for a real provider that cannot run. It never
    returns the simulator instead: a customer told a car is free must have been
    told so by the system that actually knows.
    """
    chosen = (name or provider_name()).strip().lower()
    if chosen in ("simulated", "simulator", "demo"):
        return SimulatedProvider(ctx)
    if chosen == "delta":
        from .delta import DeltaProvider

        return DeltaProvider(ctx)
    raise ProviderNotConfigured(
        f"Unknown BOOKING_PROVIDER {chosen!r}. Known providers: simulated, delta."
    )


__all__ = [
    "AvailabilityAnswer",
    "BookingProvider",
    "Outcome",
    "ProviderNotConfigured",
    "QuoteAnswer",
    "ReservationAnswer",
    "SimulatedProvider",
    "build_provider",
    "provider_name",
]
