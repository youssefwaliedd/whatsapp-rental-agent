"""Where a real flight feed plugs in.

Nothing here calls anything. It exists so that selecting a real source before
one is paid for fails loudly, rather than the agent quietly timing a delivery
against invented arrivals.

WHAT AN ADAPTER HAS TO DO

**Pick a source.** AeroDataBox, FlightAware AeroAPI and Aviationstack all answer
"when does EK456 land at DXB today". They differ in price, in how current the
estimate is, and in whether they give a terminal — which is the field that
actually matters here, because a car meeting the wrong terminal at DXB is a
twenty-minute walk for someone who has just landed.

**Map their status vocabulary onto `FlightStatus`.** Anything unrecognised is
UNAVAILABLE, never SCHEDULED. A wrong "on time" sends a car to meet a plane that
is not coming.

**Prefer the estimate over the schedule.** `Flight.arrives_at` already does, and
a provider that only returns the timetable is barely worth paying for: the
schedule is what the customer already knows.

**Cache, and say how stale.** A number that was true forty minutes ago is worth
having and is not worth planning a delivery around without knowing its age.

**Fail as UNAVAILABLE, never as NOT_FOUND.** They are different answers: one
means the flight does not exist, the other means nobody could be asked. The
agent says different things.

WHETHER THIS IS WORTH BUYING

Their team asks the customer what time they land, in the conversations we have
seen. That is free and it is usually right — a passenger knows their own flight.

What a feed buys is the delay. A customer who told you 9pm before boarding does
not message you from the air to say it is now 10:15, and a car waiting an hour
at arrivals is the visible cost. Worth the subscription only if airport delivery
is a real share of their bookings, which is a question for them and not for us.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from .results import Flight, FlightProviderNotConfigured

REQUIRED_SETTINGS = ("FLIGHT_API_URL", "FLIGHT_API_KEY")


def missing_settings() -> list[str]:
    return [name for name in REQUIRED_SETTINGS if not os.getenv(name)]


class LiveFlightProvider:
    """Not implemented. Selecting it is an error until it is."""

    name = "live"
    is_demonstration = False

    def __init__(self, *_args: Any, **_kwargs: Any):
        raise FlightProviderNotConfigured(
            "FLIGHT_PROVIDER=live is selected and no adapter exists. No flight data "
            "feed has been chosen or paid for; see rental_agent/flights/live.py for "
            "what one has to implement and whether it is worth buying. Set "
            "FLIGHT_PROVIDER=simulated for the demonstration, or leave it unset and "
            "the agent will ask the customer what time they land."
            + (f" Missing settings: {', '.join(missing_settings())}." if missing_settings() else "")
        )

    def look_up(self, number: str, when: date | None = None) -> Flight: ...
