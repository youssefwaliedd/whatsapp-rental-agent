"""Looking up a flight, without the conversation knowing who answers.

One operation. A flight number and the day it is expected, in; what is known
about it, out.

The interface exists for the same reason the booking one does — so that when
Delta pays for a flight data feed, or decides they will not, the change is a
class rather than a rewrite of how the agent talks about airport deliveries.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from .results import Flight


@runtime_checkable
class FlightProvider(Protocol):
    name: str
    #: Whether answers from this provider describe real aircraft.
    is_demonstration: bool

    def look_up(self, number: str, when: date | None = None) -> Flight:
        """What is known about this flight, or plainly that nothing is."""
