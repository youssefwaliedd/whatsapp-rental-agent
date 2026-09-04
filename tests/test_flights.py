"""Meeting a plane without inventing when it lands.

An arrival time is a fact about the world, so it obeys the rule every other fact
here obeys: the model never supplies one. It comes from something that knows, or
the agent asks the customer and says that is what it is doing.

Worth noting what prompted this. A competitor's agent handles airport delivery
by asking the customer for the landing time — twice — which is free and usually
right. What it cannot do is notice the flight is now an hour late, which is the
whole cost: a car waiting at arrivals for an hour.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from rental_agent.engine.locations import normalise_location, terminal_in
from rental_agent.flights import (
    FlightProviderNotConfigured,
    FlightStatus,
    SimulatedFlightProvider,
    build_provider,
)
from rental_agent.services.flights import arrivals_buffer, look_up_flight
from rental_agent.tools.registry import execute_tool

TZ = ZoneInfo("Asia/Dubai")


@pytest.fixture
def board():
    """A fixed moment, so a delayed flight is delayed every time this runs."""
    return SimulatedFlightProvider(lambda: datetime(2026, 9, 4, 12, 0, tzinfo=TZ))


# --- the lookup itself ------------------------------------------------------


def test_a_known_flight_comes_back_with_its_terminal(board):
    flight = board.look_up("EK456")

    assert flight.known is True
    assert flight.arrival_airport == "DXB"
    assert flight.arrival_terminal == "3"
    assert flight.airline == "Emirates"


def test_a_delay_is_reported_as_one(board):
    """The reason a feed is worth paying for. A customer who told you 9pm before
    boarding does not message from the air to say it is now 10:15."""
    flight = board.look_up("BA109")

    assert flight.status is FlightStatus.DELAYED
    assert flight.is_late is True
    assert flight.arrives_at == flight.estimated_arrival
    assert flight.estimated_arrival > flight.scheduled_arrival


def test_an_unknown_flight_says_so_rather_than_guessing(board):
    flight = board.look_up("XX999")

    assert flight.status is FlightStatus.NOT_FOUND
    assert flight.known is False
    assert "ask the customer" in (flight.message or "").lower()


def test_a_flight_number_is_read_however_it_was_typed(board):
    assert board.look_up("ek 456").number == "EK456"
    assert board.look_up("  Ek456 ").arrival_terminal == "3"


# --- what the conversation is told ------------------------------------------


def test_with_no_provider_the_agent_is_told_to_ask(booking_ctx, monkeypatch):
    """Unset is a real choice, not a broken one: it is what their own team does."""
    monkeypatch.delenv("FLIGHT_PROVIDER", raising=False)

    answer = look_up_flight(booking_ctx, flight_number="EK456")

    assert answer["known"] is False
    assert answer["reason"] == "no_provider"
    assert "Ask the customer" in answer["guidance"]
    assert "Never estimate" in answer["guidance"]


def test_a_delivery_time_allows_for_immigration_and_bags(booking_ctx, monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "simulated")

    answer = look_up_flight(booking_ctx, flight_number="EK456")
    landing = datetime.fromisoformat(answer["estimated_arrival"])
    delivery = datetime.fromisoformat(answer["suggested_delivery_at"])

    assert answer["known"] is True
    assert delivery - landing == arrivals_buffer(booking_ctx)
    assert delivery > landing


def test_a_delayed_flight_is_said_out_loud(booking_ctx, monkeypatch):
    """Letting them discover it at the kerb is the failure."""
    monkeypatch.setenv("FLIGHT_PROVIDER", "simulated")

    answer = look_up_flight(booking_ctx, flight_number="BA109")

    assert answer["delayed"] is True
    assert answer["delay_minutes"] == 70
    assert "delayed" in answer["guidance"]


def test_a_cancelled_or_landed_flight_is_not_something_to_deliver_against(booking_ctx, monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "simulated")
    # Yesterday's EK456 has already landed.
    answer = look_up_flight(
        booking_ctx, flight_number="EK456", arrival_date=date(2020, 1, 1)
    )

    assert answer["status"] == "landed"
    assert "already landed" in answer["guidance"]


def test_the_answer_says_it_is_a_demonstration(booking_ctx, monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "simulated")
    assert look_up_flight(booking_ctx, flight_number="EK456")["is_demonstration"] is True


# --- through the tool the model calls ---------------------------------------


def test_the_model_can_look_a_flight_up(booking_ctx, monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "simulated")

    result = execute_tool(booking_ctx, "look_up_flight", {"flight_number": "EK456"})

    assert result["known"] is True
    assert result["arrival_terminal"] == "3"


def test_a_nonsense_date_is_refused_rather_than_ignored(booking_ctx, monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "simulated")
    result = execute_tool(
        booking_ctx, "look_up_flight",
        {"flight_number": "EK456", "arrival_date": "next tuesday"},
    )
    assert result["error"] == "invalid_request"


def test_an_empty_flight_number_is_refused(booking_ctx):
    assert execute_tool(booking_ctx, "look_up_flight", {"flight_number": " "})["error"]


# --- and the terminal survives ----------------------------------------------


@pytest.mark.parametrize("said,terminal", [
    ("EK456 terminal 3", "3"),
    ("DXB T2 arrivals", "2"),
    ("terminal 1 please", "1"),
    ("dubai marina", None),
])
def test_the_terminal_is_kept(said, terminal):
    """`normalise_location` collapses a terminal to its airport, because pricing
    and delivery zones are per airport. A car meeting the wrong terminal at DXB
    is a twenty-minute walk for somebody who has just landed."""
    assert terminal_in(said) == terminal


def test_the_airport_is_still_normalised():
    assert normalise_location("EK456 terminal 3") == "Dubai International Airport (DXB)"


# --- choosing a provider ----------------------------------------------------


def test_nothing_is_looked_up_unless_somebody_says_so(monkeypatch):
    monkeypatch.delenv("FLIGHT_PROVIDER", raising=False)
    assert build_provider() is None


def test_selecting_a_live_feed_that_does_not_exist_fails_loudly(monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "live")
    with pytest.raises(FlightProviderNotConfigured) as raised:
        build_provider()
    assert "no adapter exists" in str(raised.value)


def test_an_unknown_provider_is_refused(monkeypatch):
    monkeypatch.setenv("FLIGHT_PROVIDER", "whatever")
    with pytest.raises(FlightProviderNotConfigured):
        build_provider()


# --- and the terminal reaches the booking -----------------------------------
#
# It was extracted and then dropped: the agent could say "Terminal 3 arrivals at
# 10pm" and the reservation recorded "Dubai International Airport (DXB)". The
# person driving the car had no way to know which of DXB's three terminals to
# go to, which is the one detail an airport delivery turns on.


def _booked(ctx):
    from tests.conftest import dt

    vehicle = ctx.engine.list_fleet()[0]
    quote = execute_tool(ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id,
        "pickup_at": dt(10, 17).isoformat(),
        "return_at": dt(12, 17).isoformat(),
    })
    ctx.messages.record(
        conversation_id=ctx.conversation_id,
        direction="inbound", content="ok book it", now=ctx.now(),
    )
    ctx.session.flush()
    return execute_tool(ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})


def test_an_airport_delivery_records_the_terminal_and_the_flight(booking_ctx):
    from tests.conftest import dt

    made = _booked(booking_ctx)

    result = execute_tool(booking_ctx, "schedule_demo_delivery", {
        "reservation_id": made["reservation_id"],
        "delivery_at": dt(10, 22).isoformat(),
        "delivery_location": "DXB terminal 3 arrivals",
        "flight_number": "ek 456",
    })

    assert result["delivery_terminal"] == "3"
    assert result["delivery_flight"] == "EK456"
    assert result["delivery_location"] == "Dubai International Airport (DXB)"
    assert "Terminal 3" in result["guidance"]


def test_the_terminal_is_taken_from_what_the_customer_wrote(booking_ctx):
    """"T2" is usually said in passing rather than answered as a question."""
    from tests.conftest import dt

    made = _booked(booking_ctx)

    result = execute_tool(booking_ctx, "schedule_demo_delivery", {
        "reservation_id": made["reservation_id"],
        "delivery_at": dt(10, 22).isoformat(),
        "delivery_location": "meet me at DXB T2",
    })

    assert result["delivery_terminal"] == "2"


def test_a_delivery_that_is_not_to_an_airport_has_no_terminal(booking_ctx):
    from tests.conftest import dt

    made = _booked(booking_ctx)

    result = execute_tool(booking_ctx, "schedule_demo_delivery", {
        "reservation_id": made["reservation_id"],
        "delivery_at": dt(10, 22).isoformat(),
        "delivery_location": "Dubai Marina",
    })

    assert result["delivery_terminal"] is None
    assert "guidance" not in result


def test_the_terminal_survives_on_the_booking_itself(booking_ctx):
    """Not just in the reply — on the row somebody dispatches the car from."""
    from tests.conftest import dt

    made = _booked(booking_ctx)
    execute_tool(booking_ctx, "schedule_demo_delivery", {
        "reservation_id": made["reservation_id"],
        "delivery_at": dt(10, 22).isoformat(),
        "delivery_location": "DXB terminal 3",
        "flight_number": "EK456",
    })

    stored = booking_ctx.reservations.get(made["reservation_id"])
    assert stored.delivery_terminal == "3"
    assert stored.delivery_flight == "EK456"
    last = stored.history[-1]
    assert last["event"] == "delivery_scheduled" and last["terminal"] == "3"
