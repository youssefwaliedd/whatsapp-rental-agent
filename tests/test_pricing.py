from __future__ import annotations

from decimal import Decimal

import pytest

from rental_agent.engine import pricing
from tests.conftest import dt


# --------------------------------------------------------------------------
# Billable days
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pickup, ret, expected",
    [
        (dt(4, 19), dt(7, 19), 3),           # exactly 72h
        (dt(4, 19), dt(7, 19, 45), 3),       # 45 min late, inside the grace period
        (dt(4, 19), dt(7, 20), 4),           # 1h late, a new day begins
        (dt(4, 19), dt(5, 19), 1),
        (dt(4, 19), dt(4, 19, 30), 1),       # shorter than the grace period
        (dt(1, 0), dt(1, 0, month=10), 30),  # all of September
    ],
)
def test_billable_days(rules, pickup, ret, expected):
    assert pricing.billable_days(pickup, ret, rules) == expected


def test_return_before_pickup_is_rejected(rules):
    with pytest.raises(ValueError):
        pricing.billable_days(dt(7, 19), dt(4, 19), rules)


def test_rental_longer_than_maximum_is_rejected(rules):
    with pytest.raises(ValueError, match="Maximum rental"):
        pricing.validate_rental_length(120, rules)


# --------------------------------------------------------------------------
# Rate selection
# --------------------------------------------------------------------------


def test_short_rental_uses_daily_rate(engine):
    veh = engine.get_vehicle("veh_13")  # G63, 2400/day
    rate = pricing.best_rate(3, veh)
    assert rate.total == Decimal("7200.00")
    assert rate.basis == "daily"


def test_six_days_rounds_up_to_the_cheaper_weekly_rate(engine):
    """veh_01: 120/day, 700/week. Six days by the day costs 720 — more than a
    full week. The customer must never be quoted above the advertised weekly."""
    veh = engine.get_vehicle("veh_01")
    rate = pricing.best_rate(6, veh)
    assert rate.total == Decimal("700.00")
    assert rate.basis == "weekly"


def test_eight_days_is_a_week_plus_a_day(engine):
    veh = engine.get_vehicle("veh_01")
    rate = pricing.best_rate(8, veh)
    assert rate.total == Decimal("820.00")  # 700 + 120
    assert rate.basis == "mixed"


def test_long_rental_uses_the_monthly_rate(engine):
    veh = engine.get_vehicle("veh_01")  # 2200/month
    rate = pricing.best_rate(35, veh)
    assert rate.total == Decimal("2800.00")  # 2200 + 5 × 120
    assert rate.basis == "mixed"


def test_rate_never_exceeds_paying_by_the_day(engine):
    """Property: for every vehicle and every length up to 60 days, the chosen
    rate is no worse than the plain daily price."""
    for veh in engine.list_fleet():
        for days in range(1, 61):
            assert pricing.best_rate(days, veh).total <= pricing.money(veh.daily_price * days)


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------


def test_free_zone_is_free_above_the_minimum_rental(rules):
    assert pricing.delivery_fee("Dubai Marina", 3, rules) == Decimal("0")


def test_free_zone_still_charges_on_a_one_day_rental(rules):
    assert pricing.delivery_fee("Dubai Marina", 1, rules) == Decimal("100.00")


def test_outer_zone_and_airport_fees(rules):
    assert pricing.delivery_fee("Sharjah", 5, rules) == Decimal("250.00")
    assert pricing.delivery_fee("Dubai International Airport (DXB)", 5, rules) == Decimal("150.00")


def test_unknown_location_falls_back_to_the_standard_fee(rules):
    assert pricing.delivery_fee("Some Villa Somewhere", 5, rules) == Decimal("100.00")


def test_no_location_means_no_fee(rules):
    assert pricing.delivery_fee(None, 5, rules) == Decimal("0")


@pytest.mark.parametrize(
    "pickup, ret, expected",
    [
        (dt(4, 19), dt(7, 19), 0),
        (dt(4, 23), dt(7, 19), 1),   # late-night delivery
        (dt(4, 6), dt(7, 5), 2),     # both ends before opening
        (dt(4, 22), dt(7, 22), 2),   # 22:00 is already out of hours
    ],
)
def test_out_of_hours_events(rules, pickup, ret, expected):
    assert pricing.out_of_hours_events(pickup, ret, rules) == expected


# --------------------------------------------------------------------------
# Discounts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "days, expected_percent",
    [
        (1, Decimal("10")),   # luxury_suv base only
        (3, Decimal("12")),   # +2
        (7, Decimal("15")),   # +5
        (14, Decimal("18")),  # +8
        (30, Decimal("20")),  # +12, clipped by the absolute cap
    ],
)
def test_discount_ladder(engine, rules, days, expected_percent):
    veh = engine.get_vehicle("veh_13")
    subtotal = pricing.best_rate(days, veh).total
    allowance = pricing.allowed_discount(veh, days, subtotal, rules)
    assert allowance.max_percent == expected_percent


def test_discount_is_capped_at_the_absolute_maximum(engine, rules):
    veh = engine.get_vehicle("veh_13")
    allowance = pricing.allowed_discount(veh, 60, Decimal("100000"), rules)
    assert allowance.max_percent == Decimal(
        str(rules.discount_policy["absolute_max_percent"])
    )


def test_large_discounts_require_human_approval(engine, rules):
    veh = engine.get_vehicle("veh_13")
    allowance = pricing.allowed_discount(veh, 30, Decimal("50000"), rules)
    assert allowance.requires_human_approval is True


def test_quote_rejects_a_discount_above_the_allowance(engine):
    with pytest.raises(ValueError, match="exceeds the allowed maximum"):
        engine.calculate_quote(
            vehicle_id="veh_13",
            pickup_at=dt(4, 19),
            return_at=dt(7, 19),
            discount_percent=Decimal("25"),
        )


# --------------------------------------------------------------------------
# Quote assembly
# --------------------------------------------------------------------------


def test_quote_totals_are_internally_consistent(engine):
    """The three-day G63 from the specification's example conversation."""
    quote = engine.calculate_quote(
        vehicle_id="veh_13",
        pickup_at=dt(4, 19),
        return_at=dt(7, 19),
        delivery_location="Dubai Marina",
    )
    assert quote.billable_days == 3
    assert quote.rental_subtotal == Decimal("7200.00")
    assert quote.delivery_fee == Decimal("0")       # free zone, 3-day rental
    assert quote.out_of_hours_fee == Decimal("0")   # 19:00 both ends
    assert quote.vat_amount == Decimal("360.00")    # 5% of 7200
    assert quote.total_charge == Decimal("7560.00")
    assert quote.deposit == Decimal("5000.00")
    assert quote.total_due_at_delivery == Decimal("12560.00")
    assert quote.included_km_total == 750           # 250/day × 3
    assert quote.is_demo is True


def test_quote_arithmetic_holds_with_every_component_present(engine):
    quote = engine.calculate_quote(
        vehicle_id="veh_13",
        pickup_at=dt(4, 23),          # out of hours
        return_at=dt(11, 23),         # 7 days -> weekly rate
        delivery_location="Sharjah",  # paid zone, both ways
        discount_percent=Decimal("10"),
        excess_reduction=True,
    )
    expected_subtotal = (
        quote.rental_subtotal
        - quote.discount_amount
        + quote.delivery_fee
        + quote.collection_fee
        + quote.out_of_hours_fee
        + quote.extras_total
    )
    assert quote.subtotal_before_vat == expected_subtotal
    assert quote.vat_amount == pricing.money(expected_subtotal * quote.vat_percent / 100)
    assert quote.total_charge == pricing.money(expected_subtotal + quote.vat_amount)
    assert quote.total_due_at_delivery == quote.total_charge + quote.deposit
    assert quote.rate_basis == "weekly"


def test_quote_line_items_reconcile_with_the_total(engine):
    """Every charged line (deposit excluded, it is refundable) must add up to
    the total. A quote whose breakdown does not sum is the single fastest way to
    lose a rental company's trust."""
    quote = engine.calculate_quote(
        vehicle_id="veh_07",
        pickup_at=dt(4, 23),
        return_at=dt(9, 8),
        delivery_location="Deira",
        excess_reduction=True,
    )
    charged = sum(line.amount for line in quote.lines if not line.is_refundable)
    assert charged == quote.total_charge


def test_quote_carries_the_rules_version(engine, rules):
    quote = engine.calculate_quote(
        vehicle_id="veh_01", pickup_at=dt(4, 10), return_at=dt(6, 10)
    )
    assert quote.rules_version == rules.version
