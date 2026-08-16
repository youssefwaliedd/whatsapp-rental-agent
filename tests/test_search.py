from __future__ import annotations

from decimal import Decimal

from rental_agent.domain.enums import Category
from rental_agent.domain.models import SearchCriteria
from rental_agent.engine.availability import Window
from rental_agent.engine.locations import normalise_location
from tests.conftest import dt


def criteria(**overrides) -> SearchCriteria:
    base = dict(pickup_at=dt(4, 19), return_at=dt(7, 19), limit=3)
    base.update(overrides)
    return SearchCriteria(**base)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def test_named_model_and_colour_rank_first(engine):
    """'I need a black G63 this weekend' must put the black G63 at position one."""
    results = engine.search(criteria(models=["G63"], color="black"))
    assert results[0].vehicle.id == "veh_13"
    assert results[0].vehicle.color == "black"
    assert results[0].match_score == 1.0


def test_the_other_g63_ranks_second(engine):
    results = engine.search(criteria(models=["G63"], color="black"))
    assert results[1].vehicle.id == "veh_14"


def test_shortlist_is_never_longer_than_three(engine):
    """Dumping the catalogue is one of the sales mistakes the evaluator flags."""
    assert len(engine.search(criteria(limit=50))) <= 3


def test_budget_is_a_hard_filter(engine):
    results = engine.search(criteria(max_daily_price=Decimal("400")))
    assert results
    assert all(m.vehicle.daily_price <= Decimal("400") for m in results)


def test_seat_requirement_is_a_hard_filter(engine):
    results = engine.search(criteria(min_passenger_capacity=7))
    assert results
    assert all(m.vehicle.passenger_capacity >= 7 for m in results)


def test_underage_driver_cannot_be_offered_a_supercar(engine):
    """Minimum age for supercars is 30 in rules.json."""
    results = engine.search(criteria(categories=[Category.SUPERCAR], driver_age=26))
    assert results == []


def test_old_enough_driver_can_be_offered_a_supercar(engine):
    results = engine.search(criteria(categories=[Category.SUPERCAR], driver_age=35))
    assert [m.vehicle.id for m in results] == ["veh_19"]  # the Huracan is blocked


def test_unavailable_vehicles_never_appear(engine):
    results = engine.search(criteria(categories=[Category.SUPERCAR], limit=3))
    assert "veh_18" not in [m.vehicle.id for m in results]


def test_search_is_deterministic(engine):
    """Required by the replay harness: the same request must always produce the
    same shortlist, or a regression test can never fail reliably."""
    first = [m.vehicle.id for m in engine.search(criteria(categories=[Category.SUV]))]
    second = [m.vehicle.id for m in engine.search(criteria(categories=[Category.SUV]))]
    assert first == second


def test_estimated_total_includes_delivery(engine):
    without = engine.search(criteria(models=["G63"], color="black"))[0]
    with_fee = engine.search(
        criteria(models=["G63"], color="black", delivery_location="Sharjah")
    )[0]
    assert with_fee.estimated_total > without.estimated_total


# --------------------------------------------------------------------------
# Alternatives
# --------------------------------------------------------------------------


def test_alternatives_lead_with_the_identical_model(engine_factory):
    """Scenario 2: the black G63 is taken, so offer the white one first — not a
    cheaper SUV."""
    engine = engine_factory(
        lambda vid: [Window(dt(1, 0), dt(30, 0), "on_rent")] if vid == "veh_13" else []
    )
    alternatives = engine.find_alternatives("veh_13", dt(4, 19), dt(7, 19))
    assert alternatives[0].vehicle.id == "veh_14"
    assert "identical model in white" in alternatives[0].match_reasons[0]


def test_alternatives_never_include_the_requested_vehicle(engine):
    alternatives = engine.find_alternatives("veh_13", dt(4, 19), dt(7, 19))
    assert "veh_13" not in [m.vehicle.id for m in alternatives]


def test_alternatives_stay_in_a_sensible_price_range(engine):
    """A Yaris is never an answer to 'the G63 is booked'."""
    target = engine.get_vehicle("veh_13")
    for match in engine.find_alternatives("veh_13", dt(4, 19), dt(7, 19)):
        assert match.vehicle.daily_price >= target.daily_price * Decimal("0.4")


def test_alternatives_respect_a_stated_budget(engine):
    alternatives = engine.find_alternatives(
        "veh_13", dt(4, 19), dt(7, 19), max_daily_price=Decimal("1500")
    )
    assert all(m.vehicle.daily_price <= Decimal("1500") for m in alternatives)


def test_alternatives_are_available(engine):
    for match in engine.find_alternatives("veh_18", dt(4, 19), dt(7, 19)):
        assert engine.check_availability(match.vehicle.id, dt(4, 19), dt(7, 19)).available


def test_at_most_three_alternatives(engine):
    assert len(engine.find_alternatives("veh_12", dt(4, 19), dt(7, 19), limit=10)) <= 3


# --------------------------------------------------------------------------
# Location normalisation
# --------------------------------------------------------------------------


def test_informal_location_names_resolve(engine):
    assert normalise_location("marina") == "Dubai Marina"
    assert normalise_location("in the Marina please") == "Dubai Marina"
    assert normalise_location("DXB terminal 3") == "Dubai International Airport (DXB)"
    assert normalise_location("Abu Dhabi Airport") == "Abu Dhabi Airport (AUH)"
    assert normalise_location("abu dhabi") == "Abu Dhabi"


def test_unknown_location_is_not_guessed(engine):
    assert normalise_location("my cousin's villa") is None
