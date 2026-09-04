"""A flight board for a demonstration, and nothing more.

Answers plausibly for a handful of real Emirates and flydubai numbers into DXB,
because a demo of an airport delivery needs a plane to meet. Everything it says
is invented, and it says so: `is_demonstration` travels on the provider so
nothing downstream can mistake it for a feed.

Deliberately not clever. It does not model airlines, routes or weather — it
produces an arrival time, a terminal, and occasionally a delay, which is the
whole of what a delivery needs to be timed against.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .results import Flight, FlightStatus

TZ = ZoneInfo("Asia/Dubai")

#: A few real numbers into Dubai, so a demonstration reads like one. Terminal 3
#: is Emirates; flydubai is largely Terminal 2.
KNOWN = {
    "EK456": {"airline": "Emirates", "origin": "LHR", "terminal": "3", "arrives": time(21, 0)},
    "EK002": {"airline": "Emirates", "origin": "LHR", "terminal": "3", "arrives": time(6, 45)},
    "EK008": {"airline": "Emirates", "origin": "LHR", "terminal": "3", "arrives": time(19, 25)},
    "EK076": {"airline": "Emirates", "origin": "CDG", "terminal": "3", "arrives": time(18, 10)},
    "FZ008": {"airline": "flydubai", "origin": "BEY", "terminal": "2", "arrives": time(15, 40)},
    "BA109": {"airline": "British Airways", "origin": "LHR", "terminal": "1", "arrives": time(23, 55)},
}

#: Which of them is running late, and by how much. Fixed rather than random, so
#: a demonstration says the same thing twice.
DELAYS = {"EK076": 35, "BA109": 70}


class SimulatedFlightProvider:
    """Invented arrivals, clearly labelled."""

    name = "simulated"
    is_demonstration = True

    def __init__(self, now_fn=None):
        self._now = now_fn or (lambda: datetime.now(TZ))

    def look_up(self, number: str, when: date | None = None) -> Flight:
        key = (number or "").replace(" ", "").upper()
        details = KNOWN.get(key)
        if details is None:
            return Flight(
                number=key,
                status=FlightStatus.NOT_FOUND,
                message=(
                    "That flight is not on the demonstration board. Ask the customer "
                    "what time they land rather than guessing."
                ),
            )

        day = when or self._now().date()
        scheduled = datetime.combine(day, details["arrives"], tzinfo=TZ)
        late_by = DELAYS.get(key, 0)
        estimated = scheduled + timedelta(minutes=late_by)

        now = self._now()
        if estimated < now:
            status = FlightStatus.LANDED
        elif late_by:
            status = FlightStatus.DELAYED
        elif estimated - now < timedelta(hours=2):
            status = FlightStatus.ACTIVE
        else:
            status = FlightStatus.SCHEDULED

        return Flight(
            number=key,
            status=status,
            arrival_airport="DXB",
            arrival_terminal=details["terminal"],
            scheduled_arrival=scheduled,
            estimated_arrival=estimated,
            origin=details["origin"],
            airline=details["airline"],
        )
