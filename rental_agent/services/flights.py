"""Meeting a plane.

Two things the conversation needs and must not invent: when the aircraft is
expected, and when to have a car at the kerb. The first comes from a provider or
from the customer. The second is the first plus a buffer that lives in
configuration, because how long immigration and baggage take at DXB is an
operator's judgement, not a model's.

With no provider configured this returns "ask them", which is not a failure. It
is what the operator's own team does, and a passenger usually knows their own
flight. What no configuration permits is a guess.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..context import ToolContext
from ..flights import FlightStatus, build_provider

#: What the agent should do when nothing can look a flight up.
ASK_THE_CUSTOMER = (
    "No flight lookup is configured, so you cannot know when this lands. Ask the "
    "customer what time they land and say you will time the delivery around it. "
    "Never estimate an arrival time yourself."
)


def arrivals_buffer(ctx: ToolContext) -> timedelta:
    """How long after landing before they are at the kerb."""
    minutes = (
        ctx.engine.rules.get("delivery", {})
        .get("airport", {})
        .get("arrivals_buffer_minutes", 60)
    )
    return timedelta(minutes=int(minutes))


def look_up_flight(
    ctx: ToolContext, *, flight_number: str, arrival_date: date | None = None
) -> dict[str, Any]:
    """What is known about a flight, and when a car should meet it."""
    if not (flight_number or "").strip():
        return {"error": "invalid_request", "message": "A flight number is needed."}

    provider = build_provider(ctx.now)
    if provider is None:
        return {
            "known": False,
            "reason": "no_provider",
            "guidance": ASK_THE_CUSTOMER,
        }

    flight = provider.look_up(flight_number, arrival_date)
    if not flight.known:
        return {
            "known": False,
            "flight_number": flight.number,
            "reason": flight.status.value,
            "guidance": flight.message or ASK_THE_CUSTOMER,
        }

    arrives = flight.arrives_at
    deliver_at = arrives + arrivals_buffer(ctx) if arrives else None
    return {
        "known": True,
        "is_demonstration": getattr(provider, "is_demonstration", True),
        "flight_number": flight.number,
        "status": flight.status.value,
        "airline": flight.airline,
        "origin": flight.origin,
        "arrival_airport": flight.arrival_airport,
        "arrival_terminal": flight.arrival_terminal,
        "scheduled_arrival": flight.scheduled_arrival.isoformat() if flight.scheduled_arrival else None,
        "estimated_arrival": flight.estimated_arrival.isoformat() if flight.estimated_arrival else None,
        "delayed": flight.is_late,
        "delay_minutes": (
            int((flight.estimated_arrival - flight.scheduled_arrival).total_seconds() // 60)
            if flight.is_late else 0
        ),
        "suggested_delivery_at": deliver_at.isoformat() if deliver_at else None,
        "guidance": _guidance(flight, deliver_at),
    }


def _guidance(flight: Any, deliver_at: datetime | None) -> str:
    """What to do with this, in the agent's own terms."""
    if flight.status is FlightStatus.CANCELLED:
        return (
            "That flight is cancelled. Do not schedule a delivery against it — ask "
            "the customer what they are now on."
        )
    if flight.status is FlightStatus.LANDED:
        return (
            "It has already landed. Ask where they are rather than timing a delivery "
            "against an arrival that has happened."
        )
    when = f"{deliver_at:%-I:%M %p}" if deliver_at else "a time you agree with them"
    terminal = f" Terminal {flight.arrival_terminal}" if flight.arrival_terminal else ""
    if flight.is_late:
        return (
            f"Running late. Offer delivery at{terminal} arrivals around {when}, say it "
            "is timed to the current estimate, and tell them plainly that the flight is "
            "delayed rather than letting them discover it at the kerb."
        )
    return (
        f"Offer delivery at{terminal} arrivals around {when} — after landing, plus time "
        "for immigration and bags. Confirm it with them rather than assuming."
    )
