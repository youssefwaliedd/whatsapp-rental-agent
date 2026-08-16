"""RentalEngine — the single authority on availability and price.

The tool layer calls this. The language model never computes any of it.

The engine is deliberately constructed with an explicit clock and reference
date: every demo scenario and every regression replay must be reproducible.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Rules, load_fleet, load_rules
from ..domain.enums import Category
from ..domain.models import (
    AvailabilityResult,
    DiscountAllowance,
    Operator,
    Quote,
    SearchCriteria,
    Vehicle,
    VehicleMatch,
)
from . import pricing, search
from .availability import Window, check_availability
from .locations import normalise_location

#: Placeholder id used when a quote is calculated for display but not persisted.
PREVIEW_QUOTE_ID = "DQ-PREVIEW"

#: Sales policy, enforced here rather than in the tool wrapper so no caller can
#: bypass it: presenting more than three cars at once reduces conversion and is
#: one of the mistakes the evaluator is written to detect.
MAX_SHORTLIST = 3


class VehicleNotFound(Exception):
    """Deliberately not a KeyError: the tool layer maps KeyError to
    "missing argument", and an unknown vehicle id must never be reported as a
    missing one — the agent would re-ask the customer for what they just said."""

    def __init__(self, vehicle_id: str):
        self.vehicle_id = vehicle_id
        super().__init__(f"No vehicle with id {vehicle_id}")


class VehicleUnavailable(Exception):
    def __init__(self, vehicle_id: str, result: AvailabilityResult):
        self.vehicle_id = vehicle_id
        self.result = result
        super().__init__(f"{vehicle_id} is not available for the requested window")


class RentalEngine:
    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime] | None = None,
        reference_date: date | None = None,
        extra_blocks_provider: Callable[[str], list[Window]] | None = None,
    ) -> None:
        self._operator, self._vehicles = load_fleet()
        self._rules: Rules = load_rules()
        self._tz = ZoneInfo(self._operator.timezone)
        self._now_fn = now_fn or (lambda: datetime.now(self._tz))
        self._reference_date = reference_date
        self._extra_blocks_provider = extra_blocks_provider or (lambda _vid: [])
        self._by_id = {v.id: v for v in self._vehicles}

    # -- context ---------------------------------------------------------

    @property
    def operator(self) -> Operator:
        return self._operator

    @property
    def rules(self) -> Rules:
        return self._rules

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def now(self) -> datetime:
        return self._now_fn()

    @property
    def reference_date(self) -> date:
        """Anchor for the seeded availability calendar. Defaults to 'today'."""
        return self._reference_date or self.now().astimezone(self._tz).date()

    # -- fleet -----------------------------------------------------------

    def list_fleet(self) -> tuple[Vehicle, ...]:
        return self._vehicles

    def get_vehicle(self, vehicle_id: str) -> Vehicle:
        try:
            return self._by_id[vehicle_id]
        except KeyError as exc:
            raise VehicleNotFound(vehicle_id) from exc

    # -- availability ----------------------------------------------------

    def check_availability(
        self, vehicle_id: str, pickup_at: datetime, return_at: datetime
    ) -> AvailabilityResult:
        vehicle = self.get_vehicle(vehicle_id)
        return check_availability(
            vehicle,
            pickup_at,
            return_at,
            reference_date=self.reference_date,
            tz=self._tz,
            extra_blocks=self._extra_blocks_provider(vehicle_id),
        )

    # -- search ----------------------------------------------------------

    def search(self, criteria: SearchCriteria) -> list[VehicleMatch]:
        matches: list[VehicleMatch] = []
        for vehicle in self._vehicles:
            minimum_age = self._rules.minimum_age_for(vehicle.category.value)
            ok, _ = search.passes_hard_filters(vehicle, criteria, minimum_age)
            if not ok:
                continue
            if not self.check_availability(
                vehicle.id, criteria.pickup_at, criteria.return_at
            ).available:
                continue
            score, reasons = search.score_vehicle(vehicle, criteria)
            quote = self.calculate_quote(
                vehicle_id=vehicle.id,
                pickup_at=criteria.pickup_at,
                return_at=criteria.return_at,
                delivery_location=criteria.delivery_location,
                skip_availability_check=True,
            )
            matches.append(
                VehicleMatch(
                    vehicle=vehicle,
                    estimated_total=quote.total_charge,
                    billable_days=quote.billable_days,
                    match_score=score,
                    match_reasons=reasons,
                )
            )
        shortlisted = search.prefer_named_models(search.rank(matches), criteria)
        return shortlisted[: min(criteria.limit, MAX_SHORTLIST)]

    def find_alternatives(
        self,
        vehicle_id: str,
        pickup_at: datetime,
        return_at: datetime,
        *,
        limit: int = 3,
        max_daily_price: Decimal | None = None,
        delivery_location: str | None = None,
    ) -> list[VehicleMatch]:
        """Targeted substitutes for an unavailable (or rejected) vehicle.

        Ordered by how close the substitute is to what was asked for, not by
        price — offering a Yaris when someone asked for a G63 is the failure
        mode this ordering exists to prevent.
        """
        target = self.get_vehicle(vehicle_id)
        scored: list[tuple[int, VehicleMatch]] = []

        for candidate in self._vehicles:
            tier_reason = search.alternative_reason(target, candidate)
            if tier_reason is None:
                continue
            if max_daily_price is not None and candidate.daily_price > max_daily_price:
                continue
            if not self.check_availability(candidate.id, pickup_at, return_at).available:
                continue

            tier, reason = tier_reason
            quote = self.calculate_quote(
                vehicle_id=candidate.id,
                pickup_at=pickup_at,
                return_at=return_at,
                delivery_location=delivery_location,
                skip_availability_check=True,
            )
            scored.append(
                (
                    tier,
                    VehicleMatch(
                        vehicle=candidate,
                        estimated_total=quote.total_charge,
                        billable_days=quote.billable_days,
                        # Tier 0 -> 1.0, tier 4 -> 0.2. Ranking only.
                        match_score=round(1.0 - tier * 0.2, 2),
                        match_reasons=[reason],
                    ),
                )
            )

        # Within a tier, the closest price to what the customer was already
        # willing to pay wins — not simply the cheapest car.
        scored.sort(
            key=lambda pair: (
                pair[0],
                abs(pair[1].vehicle.daily_price - target.daily_price),
                pair[1].vehicle.id,
            )
        )
        return [m for _, m in scored[: min(limit, MAX_SHORTLIST)]]

    # -- money -----------------------------------------------------------

    def calculate_quote(
        self,
        *,
        vehicle_id: str,
        pickup_at: datetime,
        return_at: datetime,
        delivery_location: str | None = None,
        discount_percent: Decimal = Decimal("0"),
        excess_reduction: bool = False,
        quote_id: str = PREVIEW_QUOTE_ID,
        skip_availability_check: bool = False,
    ) -> Quote:
        vehicle = self.get_vehicle(vehicle_id)

        if not skip_availability_check:
            result = self.check_availability(vehicle_id, pickup_at, return_at)
            if not result.available:
                raise VehicleUnavailable(vehicle_id, result)

        return pricing.build_quote(
            quote_id=quote_id,
            vehicle=vehicle,
            pickup_at=pickup_at,
            return_at=return_at,
            rules=self._rules,
            now=self.now(),
            delivery_location=normalise_location(delivery_location) or delivery_location,
            discount_percent=discount_percent,
            excess_reduction=excess_reduction,
        )

    def get_allowed_discount(
        self, *, vehicle_id: str, pickup_at: datetime, return_at: datetime
    ) -> DiscountAllowance:
        vehicle = self.get_vehicle(vehicle_id)
        days = pricing.billable_days(pickup_at, return_at, self._rules)
        subtotal = pricing.best_rate(days, vehicle).total
        return pricing.allowed_discount(vehicle, days, subtotal, self._rules)

    # -- policy lookups --------------------------------------------------

    def minimum_age_for(self, category: Category | str) -> int:
        value = category.value if isinstance(category, Category) else category
        return self._rules.minimum_age_for(value)
