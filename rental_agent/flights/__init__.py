"""Choosing where flight arrivals come from.

Unset is a real and sensible choice: with no provider the agent asks the
customer what time they land, which is what the operator's own team does today
and is usually right. What it must never do is invent one.
"""

from __future__ import annotations

import os
from typing import Any

from .base import FlightProvider
from .results import Flight, FlightProviderNotConfigured, FlightStatus
from .simulated import SimulatedFlightProvider

#: No lookup at all. The agent asks the customer, and says that is what it did.
NONE = "none"


def provider_name() -> str:
    return os.getenv("FLIGHT_PROVIDER", NONE).strip().lower()


def build_provider(now_fn: Any = None, name: str | None = None) -> FlightProvider | None:
    """The flight source, or None when there is deliberately not one."""
    chosen = (name or provider_name()).strip().lower()
    if chosen in ("", NONE, "off", "0"):
        return None
    if chosen in ("simulated", "demo"):
        return SimulatedFlightProvider(now_fn)
    if chosen == "live":
        from .live import LiveFlightProvider

        return LiveFlightProvider()
    raise FlightProviderNotConfigured(
        f"Unknown FLIGHT_PROVIDER {chosen!r}. Known: none, simulated, live."
    )


__all__ = [
    "Flight",
    "FlightProvider",
    "FlightProviderNotConfigured",
    "FlightStatus",
    "SimulatedFlightProvider",
    "build_provider",
    "provider_name",
]
