"""What a flight lookup can answer.

A landing time is a fact about the world, so it obeys the same rule as a price:
the model never supplies one. It either comes from something that knows, or the
agent asks the customer and says that is what it is doing.

`UNKNOWN` matters as much as it does for bookings. "That flight is delayed to
23:40" and "I could not find that flight" lead to completely different messages,
and a lookup that collapsed them would have the agent sending a car to meet a
plane on a schedule nobody checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class FlightStatus(str, Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    LANDED = "landed"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    #: The provider answered and does not have this flight.
    NOT_FOUND = "not_found"
    #: Nobody could be asked. Different from not_found: one is an answer.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Flight:
    number: str
    status: FlightStatus
    #: Where it lands, as an IATA code, and which terminal if the provider knows.
    arrival_airport: str | None = None
    arrival_terminal: str | None = None
    scheduled_arrival: datetime | None = None
    #: What the provider now expects, which is the one to plan a delivery around.
    estimated_arrival: datetime | None = None
    origin: str | None = None
    airline: str | None = None
    message: str | None = None

    @property
    def known(self) -> bool:
        """Whether this answer may be used to time anything."""
        return self.status not in (FlightStatus.NOT_FOUND, FlightStatus.UNAVAILABLE)

    @property
    def arrives_at(self) -> datetime | None:
        """The time to plan around — estimated where there is one."""
        return self.estimated_arrival or self.scheduled_arrival

    @property
    def is_late(self) -> bool:
        if not (self.estimated_arrival and self.scheduled_arrival):
            return False
        return self.estimated_arrival > self.scheduled_arrival


class FlightProviderNotConfigured(RuntimeError):
    """A real flight source was selected and cannot run.

    Raised rather than degraded. A guessed landing time is worse than no landing
    time: the customer is told a car will meet them, and it will not.
    """
