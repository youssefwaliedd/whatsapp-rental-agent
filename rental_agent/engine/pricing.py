"""Pricing. Pure functions — no I/O, no model, no randomness.

Every number a customer ever sees is produced here or in `availability.py`.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from ..config import Rules
from ..domain.models import DiscountAllowance, Quote, QuoteLine, Vehicle

MONEY = Decimal("0.01")

_RATE_LABEL = {"day": "Daily", "week": "Weekly", "month": "Monthly"}


def _percent(value: Decimal) -> str:
    """`5.0` -> `5`, `12.5` -> `12.5`. Percentages read badly with dead decimals."""
    normalised = Decimal(str(value)).normalize()
    return f"{normalised:f}"


def money(value: Decimal | int | float | str) -> Decimal:
    """Quantise to 2dp, half-up. Applied at every boundary so stored quotes
    re-serialise identically to what the customer was shown."""
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Rental period
# --------------------------------------------------------------------------


def billable_days(pickup_at: datetime, return_at: datetime, rules: Rules) -> int:
    """Whole 24h periods, with a grace allowance, minimum one day.

    A 72h rental is 3 days; 73h is 4. The grace period stops a customer who is
    forty minutes late from being billed an extra day at quote time.
    """
    if return_at <= pickup_at:
        raise ValueError("return_at must be after pickup_at")

    period = rules.rental_period
    grace = timedelta(minutes=int(period["grace_minutes"]))
    unit = timedelta(hours=int(period["billing_unit_hours"]))

    effective = (return_at - pickup_at) - grace
    days = math.ceil(effective / unit)
    return max(int(period["minimum_rental_days"]), days)


def validate_rental_length(days: int, rules: Rules) -> None:
    period = rules.rental_period
    if days < int(period["minimum_rental_days"]):
        raise ValueError(f"Minimum rental is {period['minimum_rental_days']} day(s)")
    if days > int(period["maximum_rental_days"]):
        raise ValueError(f"Maximum rental is {period['maximum_rental_days']} days")


# --------------------------------------------------------------------------
# Rate selection
# --------------------------------------------------------------------------


class RateBreakdown:
    __slots__ = ("total", "basis", "parts")

    def __init__(self, total: Decimal, basis: str, parts: list[tuple[str, int, Decimal]]):
        self.total = total
        self.basis = basis
        #: (unit label, count, unit price) — drives the human-readable quote lines.
        self.parts = parts


def _daily_only(days: int, vehicle: Vehicle) -> RateBreakdown:
    return RateBreakdown(
        money(vehicle.daily_price * days),
        "daily",
        [("day", days, money(vehicle.daily_price))],
    )


def best_rate(days: int, vehicle: Vehicle) -> RateBreakdown:
    """Cheapest legitimate combination of daily / weekly / monthly rates.

    Includes rounding *up* to a full week or month when that is cheaper than
    paying by the day — six days on a car with a 700 weekly and 120 daily rate
    costs 700, not 720. Without this the demo eventually quotes a price higher
    than its own advertised weekly rate, which reads as a bug to a rental
    company.
    """
    candidates: list[RateBreakdown] = [_daily_only(days, vehicle)]

    if vehicle.weekly_price:
        for weeks in range(1, math.ceil(days / 7) + 1):
            remainder = max(0, days - weeks * 7)
            total = money(vehicle.weekly_price * weeks + vehicle.daily_price * remainder)
            parts: list[tuple[str, int, Decimal]] = [("week", weeks, money(vehicle.weekly_price))]
            if remainder:
                parts.append(("day", remainder, money(vehicle.daily_price)))
            candidates.append(RateBreakdown(total, "weekly" if not remainder else "mixed", parts))

    if vehicle.monthly_price:
        for months in range(1, math.ceil(days / 30) + 1):
            remainder = max(0, days - months * 30)
            sub = best_rate(remainder, vehicle) if remainder else None
            total = money(vehicle.monthly_price * months + (sub.total if sub else Decimal("0")))
            parts = [("month", months, money(vehicle.monthly_price))]
            if sub:
                parts.extend(sub.parts)
            candidates.append(RateBreakdown(total, "monthly" if not remainder else "mixed", parts))

    # Ties break toward the simplest structure (fewest line items).
    return min(candidates, key=lambda c: (c.total, len(c.parts)))


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------


def delivery_fee(location: str | None, days: int, rules: Rules) -> Decimal:
    """Zone-based, with free delivery in core zones above a minimum rental length."""
    if not location:
        return Decimal("0")

    delivery = rules.delivery
    airport = delivery["airport_delivery_fee"]
    if location in airport:
        return money(airport[location])

    standard = money(delivery["standard_delivery_fee"])
    zone = delivery["zone_fees"].get(location)
    if zone is None:
        return standard

    zone = money(zone)
    if zone == 0:
        # Free zones are only free for rentals long enough to earn it.
        if days >= int(delivery["free_delivery_minimum_rental_days"]):
            return Decimal("0")
        return standard
    return zone


def _is_out_of_hours(moment: datetime, rules: Rules) -> bool:
    window = rules.delivery["out_of_hours_window"]
    start = time.fromisoformat(window["from"])
    end = time.fromisoformat(window["to"])
    t = moment.timetz().replace(tzinfo=None)
    # The window wraps past midnight.
    return t >= start or t < end


def out_of_hours_events(pickup_at: datetime, return_at: datetime, rules: Rules) -> int:
    return sum(
        1 for moment in (pickup_at, return_at) if _is_out_of_hours(moment, rules)
    )


# --------------------------------------------------------------------------
# Discounts
# --------------------------------------------------------------------------


def allowed_discount(
    vehicle: Vehicle,
    days: int,
    rental_subtotal: Decimal,
    rules: Rules,
) -> DiscountAllowance:
    """The ceiling the agent may offer. Never derived from what a customer claims."""
    policy = rules.discount_policy
    category = vehicle.category.value

    base = Decimal(str(policy["max_percent_by_category"][category]))
    bonus = Decimal("0")
    for tier in policy["duration_bonus_percent"]:
        if days >= int(tier["minimum_days"]):
            bonus = Decimal(str(tier["bonus_percent"]))

    total = base + bonus
    absolute = Decimal(str(policy["absolute_max_percent"]))
    capped = min(total, absolute)

    return DiscountAllowance(
        max_percent=capped,
        max_amount=money(rental_subtotal * capped / 100),
        breakdown={
            "category_base_percent": base,
            "duration_bonus_percent": bonus,
            "absolute_cap_percent": absolute,
        },
        requires_human_approval=capped
        > Decimal(str(policy["requires_human_approval_above_percent"])),
        rules_version=rules.version,
    )


# --------------------------------------------------------------------------
# Quote assembly
# --------------------------------------------------------------------------


def build_quote(
    *,
    quote_id: str,
    vehicle: Vehicle,
    pickup_at: datetime,
    return_at: datetime,
    rules: Rules,
    now: datetime,
    delivery_location: str | None = None,
    discount_percent: Decimal = Decimal("0"),
    excess_reduction: bool = False,
    quote_validity_hours: int = 24,
) -> Quote:
    days = billable_days(pickup_at, return_at, rules)
    validate_rental_length(days, rules)

    rate = best_rate(days, vehicle)
    lines: list[QuoteLine] = []
    for label, count, unit_price in rate.parts:
        lines.append(
            QuoteLine(
                code=f"rental_{label}",
                label=f"{_RATE_LABEL[label]} rate × {count}",
                amount=money(unit_price * count),
                quantity=count,
                unit_price=unit_price,
            )
        )

    rental_subtotal = rate.total

    allowance = allowed_discount(vehicle, days, rental_subtotal, rules)
    if discount_percent > allowance.max_percent:
        raise ValueError(
            f"Discount {discount_percent}% exceeds the allowed maximum "
            f"{allowance.max_percent}% for {vehicle.category.value} over {days} day(s)"
        )
    discount_amount = money(rental_subtotal * discount_percent / 100)
    if discount_amount:
        lines.append(
            QuoteLine(
                code="discount",
                label=f"Discount ({_percent(discount_percent)}%)",
                amount=-discount_amount,
            )
        )

    delivery = delivery_fee(delivery_location, days, rules)
    collection = (
        delivery if rules.delivery.get("collection_fee_equals_delivery_fee") else Decimal("0")
    )
    if delivery:
        lines.append(QuoteLine(code="delivery", label="Delivery", amount=delivery))
    if collection:
        lines.append(QuoteLine(code="collection", label="Collection", amount=collection))

    ooh_count = out_of_hours_events(pickup_at, return_at, rules)
    out_of_hours = money(Decimal(str(rules.delivery["out_of_hours_fee"])) * ooh_count)
    if out_of_hours:
        lines.append(
            QuoteLine(
                code="out_of_hours",
                label=f"Out-of-hours service ×{ooh_count}",
                amount=out_of_hours,
            )
        )

    extras_total = Decimal("0")
    if excess_reduction:
        daily_extra = Decimal(
            str(rules.insurance["excess_reduction_daily_price_by_category"][vehicle.category.value])
        )
        extras_total = money(daily_extra * days)
        lines.append(
            QuoteLine(
                code="excess_reduction",
                label=f"Excess reduction × {days}",
                amount=extras_total,
                quantity=days,
                unit_price=daily_extra,
            )
        )

    subtotal_before_vat = money(
        rental_subtotal - discount_amount + delivery + collection + out_of_hours + extras_total
    )
    vat_amount = money(subtotal_before_vat * rules.vat_percent / 100)
    total_charge = money(subtotal_before_vat + vat_amount)

    lines.append(QuoteLine(code="vat", label=f"VAT ({_percent(rules.vat_percent)}%)", amount=vat_amount))
    # No line at all when the deposit is unconfirmed. A zero-amount line reads
    # as "no deposit on this car", which is a statement nobody at the operator
    # has made.
    if vehicle.deposit is not None:
        lines.append(
            QuoteLine(
                code="deposit",
                label="Refundable security deposit",
                amount=money(vehicle.deposit),
                is_refundable=True,
            )
        )

    return Quote(
        quote_id=quote_id,
        vehicle_id=vehicle.id,
        vehicle_display_name=vehicle.display_name,
        currency=rules.currency,
        pickup_at=pickup_at,
        return_at=return_at,
        delivery_location=delivery_location,
        billable_days=days,
        rate_basis=rate.basis,
        lines=lines,
        rental_subtotal=rental_subtotal,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        delivery_fee=delivery,
        collection_fee=collection,
        out_of_hours_fee=out_of_hours,
        extras_total=extras_total,
        subtotal_before_vat=subtotal_before_vat,
        vat_percent=rules.vat_percent,
        vat_amount=vat_amount,
        total_charge=total_charge,
        deposit=money(vehicle.deposit) if vehicle.deposit is not None else None,
        # Unknown too, rather than silently equal to the total: the customer
        # plans around this number, and one that omits a deposit they will be
        # asked for at handover is the worst kind of wrong.
        total_due_at_delivery=(
            money(total_charge + vehicle.deposit) if vehicle.deposit is not None else None
        ),
        included_km_total=vehicle.included_km_per_day * days,
        extra_km_price=(
            money(vehicle.extra_km_price) if vehicle.extra_km_price is not None else None
        ),
        insurance_excess=rules.insurance_excess_for(vehicle.category.value),
        created_at=now,
        expires_at=now + timedelta(hours=quote_validity_hours),
        rules_version=rules.version,
    )
