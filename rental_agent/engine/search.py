"""Matching and ranking.

The engine decides *which* vehicles are offerable and in what order. The model
only decides how to say it. Ranking is deterministic so the same request always
produces the same shortlist — a requirement for the replay-based regression
tests in the learning loop.
"""

from __future__ import annotations

from decimal import Decimal

from ..domain.enums import CATEGORY_LADDER, Category
from ..domain.models import SearchCriteria, Vehicle, VehicleMatch

# Weights sum to 1.0. Model match dominates: a customer who names a car wants
# that car, and near-misses on it beat a better-scoring unrelated vehicle.
_W_MODEL = Decimal("0.35")
_W_MAKE = Decimal("0.15")
_W_CATEGORY = Decimal("0.20")
_W_COLOR = Decimal("0.15")
_W_BUDGET = Decimal("0.15")

_NEUTRAL_SCORE = 0.5


def _matches_text(value: str, wanted: list[str] | None) -> bool:
    if not wanted:
        return False
    v = value.lower()
    return any(w.lower() in v or v in w.lower() for w in wanted)


def score_vehicle(vehicle: Vehicle, criteria: SearchCriteria) -> tuple[float, list[str]]:
    """0..1 relevance plus human-readable reasons (used by the evaluator, not the customer)."""
    stated = 0
    earned = Decimal("0")
    reasons: list[str] = []

    if criteria.models:
        stated += 1
        if _matches_text(vehicle.model, criteria.models):
            earned += _W_MODEL
            reasons.append("requested model")
    if criteria.makes:
        stated += 1
        if _matches_text(vehicle.make, criteria.makes):
            earned += _W_MAKE
            reasons.append("requested make")
    if criteria.categories:
        stated += 1
        if vehicle.category in criteria.categories:
            earned += _W_CATEGORY
            reasons.append("requested category")
    if criteria.color:
        stated += 1
        if vehicle.color.lower() == criteria.color.lower():
            earned += _W_COLOR
            reasons.append("requested colour")
    if criteria.max_daily_price is not None:
        stated += 1
        if vehicle.daily_price <= criteria.max_daily_price:
            earned += _W_BUDGET
            reasons.append("within budget")

    if stated == 0:
        return _NEUTRAL_SCORE, reasons

    # Normalise by the weight actually in play, so a customer who states only a
    # colour can still reach a perfect score.
    available_weight = Decimal("0")
    if criteria.models:
        available_weight += _W_MODEL
    if criteria.makes:
        available_weight += _W_MAKE
    if criteria.categories:
        available_weight += _W_CATEGORY
    if criteria.color:
        available_weight += _W_COLOR
    if criteria.max_daily_price is not None:
        available_weight += _W_BUDGET

    return float(earned / available_weight), reasons


def passes_hard_filters(
    vehicle: Vehicle, criteria: SearchCriteria, minimum_age: int | None
) -> tuple[bool, str | None]:
    """Filters that are never traded off against score.

    Budget is a hard filter: quoting above a stated budget is a sales mistake the
    evaluator specifically looks for.
    """
    if vehicle.id in criteria.exclude_vehicle_ids:
        return False, "excluded"
    if criteria.categories and vehicle.category not in criteria.categories:
        # A customer who asks for a supercar is not served by a hatchback. When
        # nothing in the class is free, the caller falls back to
        # find_alternatives, which crosses classes deliberately and explains why.
        return False, "wrong category"
    if criteria.min_passenger_capacity and vehicle.passenger_capacity < criteria.min_passenger_capacity:
        return False, "too few seats"
    if criteria.max_daily_price is not None and vehicle.daily_price > criteria.max_daily_price:
        return False, "over budget"
    if (
        minimum_age is not None
        and criteria.driver_age is not None
        and criteria.driver_age < minimum_age
    ):
        return False, f"driver under minimum age {minimum_age}"
    return True, None


def rank(matches: list[VehicleMatch]) -> list[VehicleMatch]:
    """Best score first; cheaper first on ties; id last so ordering is total."""
    return sorted(
        matches,
        key=lambda m: (-m.match_score, m.vehicle.daily_price, m.vehicle.id),
    )


def prefer_named_models(
    matches: list[VehicleMatch], criteria: SearchCriteria
) -> list[VehicleMatch]:
    """If the customer named a model and we have it, show only that model.

    Padding a shortlist with unrelated cars after the customer named one reads
    as not listening. When the named model is unavailable the list is returned
    untouched, and the caller decides whether to present alternatives.
    """
    if not criteria.models:
        return matches
    exact = [m for m in matches if _matches_text(m.vehicle.model, criteria.models)]
    return exact or matches


# --------------------------------------------------------------------------
# Alternatives
# --------------------------------------------------------------------------


def _price_band(
    candidate: Vehicle, spread: Decimal = Decimal("0.35")
) -> tuple[Decimal, Decimal]:
    """Acceptable daily-price range around a reference vehicle."""
    return candidate.daily_price * (1 - spread), candidate.daily_price * (1 + spread)


def alternative_reason(target: Vehicle, candidate: Vehicle) -> tuple[int, str] | None:
    """Rank tier (lower is better) and the phrase explaining the substitution.

    Tiers exist so the agent leads with "same car, different colour" before
    reaching for "similar SUV" — the difference between a good and a lazy
    recommendation.
    """
    if candidate.id == target.id:
        return None

    if candidate.model.lower() == target.model.lower() and candidate.make.lower() == target.make.lower():
        if candidate.color.lower() != target.color.lower():
            return 0, f"identical model in {candidate.color}"
        return 0, f"identical model, {candidate.year}"

    if candidate.make.lower() == target.make.lower() and candidate.category is target.category:
        return 1, f"same brand, same class"

    low, high = _price_band(candidate=target)
    if candidate.category is target.category:
        if low <= candidate.daily_price <= high:
            return 2, "same class, similar price"
        return 3, "same class"

    try:
        idx = CATEGORY_LADDER.index(target.category)
    except ValueError:  # pragma: no cover - all categories are on the ladder
        return None
    neighbours: list[Category] = []
    if idx > 0:
        neighbours.append(CATEGORY_LADDER[idx - 1])
    if idx < len(CATEGORY_LADDER) - 1:
        neighbours.append(CATEGORY_LADDER[idx + 1])
    # A wider band for the adjacent class: a 911 is a fair answer to "the
    # Huracan is booked", a Yaris never is.
    wide_low, wide_high = _price_band(candidate=target, spread=Decimal("0.60"))
    if candidate.category in neighbours and wide_low <= candidate.daily_price <= wide_high:
        return 4, "closest available class"

    return None
