from __future__ import annotations

from datetime import datetime

from rental_agent.domain.enums import UnavailabilityReason
from rental_agent.engine.availability import Window
from tests.conftest import REFERENCE_DATE, TZ, dt

# fleet.json blocks veh_18 (Huracan) for offsets 0..10, i.e. 1–11 September
# inclusive, given a reference date of 1 September 2026.


def test_unblocked_vehicle_is_available(engine):
    result = engine.check_availability("veh_13", dt(4, 19), dt(7, 19))
    assert result.available is True
    assert result.reason is None


def test_blocked_vehicle_is_unavailable(engine):
    result = engine.check_availability("veh_18", dt(4, 19), dt(7, 19))
    assert result.available is False
    assert result.reason is UnavailabilityReason.ON_RENT


def test_unavailable_result_reports_when_the_car_frees_up(engine):
    result = engine.check_availability("veh_18", dt(4, 19), dt(7, 19))
    assert result.next_available_from == datetime(2026, 9, 12, 0, 0, tzinfo=TZ)


def test_rental_starting_exactly_when_a_block_ends_is_available(engine):
    """The block covers through 11 September, so 12 September 00:00 is free.
    Off-by-one here would either lose bookings or double-book a car."""
    result = engine.check_availability("veh_18", dt(12, 0), dt(14, 0))
    assert result.available is True


def test_rental_ending_exactly_when_a_block_starts_is_available(engine):
    """veh_10 is blocked for offsets 5..9 → 6–10 September."""
    result = engine.check_availability("veh_10", dt(3, 10), dt(6, 0))
    assert result.available is True


def test_rental_overlapping_a_block_by_one_hour_is_unavailable(engine):
    result = engine.check_availability("veh_10", dt(3, 10), dt(6, 1))
    assert result.available is False


def test_maintenance_vehicle_is_never_offered(engine):
    result = engine.check_availability("veh_06", dt(4, 19), dt(7, 19))
    assert result.available is False
    assert result.reason is UnavailabilityReason.MAINTENANCE


def test_live_reservations_block_a_vehicle(engine_factory):
    """Demo reservations reach the engine as extra blocks. Without this the
    prototype would happily sell the same G63 twice in one demo."""
    booked = Window(dt(4, 0), dt(8, 0), "on_rent")
    engine = engine_factory(lambda vid: [booked] if vid == "veh_13" else [])

    assert engine.check_availability("veh_13", dt(5, 19), dt(6, 19)).available is False
    assert engine.check_availability("veh_14", dt(5, 19), dt(6, 19)).available is True


def test_reference_date_anchors_the_seeded_calendar(engine):
    assert engine.reference_date == REFERENCE_DATE
