"""Domain models.

Money is `Decimal` everywhere. The engine quantises to 2dp at the boundaries so
that a quote read back from storage always re-serialises to the same string the
customer was shown.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    Category,
    Intent,
    ReservationStatus,
    ResidencyType,
    Stage,
    UnavailabilityReason,
    VehicleStatus,
)


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------
# Fleet
# --------------------------------------------------------------------------


class BlockedRange(Base):
    """A window in which a vehicle cannot be rented.

    Offsets are relative to the engine's reference date so the seeded demo fleet
    never goes stale. `end_offset_days` is inclusive of that whole day.
    """

    start_offset_days: int
    end_offset_days: int
    reason: str = "on_rent"


class Vehicle(Base):
    id: str
    make: str
    model: str
    year: int
    category: Category
    body_type: str
    color: str
    interior_color: str
    daily_price: Decimal
    weekly_price: Decimal | None = None
    monthly_price: Decimal | None = None
    deposit: Decimal
    included_km_per_day: int
    extra_km_price: Decimal
    features: list[str] = Field(default_factory=list)
    passenger_capacity: int
    luggage_capacity: int
    transmission: str = "automatic"
    images: list[str] = Field(default_factory=list)
    status: VehicleStatus = VehicleStatus.ACTIVE
    blocked_ranges: list[BlockedRange] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        return f"{self.make} {self.model} — {self.year}"


class Operator(Base):
    demo_company_name: str
    currency: str
    timezone: str
    is_demonstration: bool = True


# --------------------------------------------------------------------------
# Availability & search
# --------------------------------------------------------------------------


class AvailabilityResult(Base):
    vehicle_id: str
    available: bool
    reason: UnavailabilityReason | None = None
    #: Populated when the vehicle frees up later within the searched horizon.
    next_available_from: datetime | None = None


class VehicleMatch(Base):
    """A vehicle the engine has confirmed free for the requested window."""

    vehicle: Vehicle
    estimated_total: Decimal
    billable_days: int
    #: 0..1, how well it matches the stated preferences. Ranking only — never shown.
    match_score: float
    match_reasons: list[str] = Field(default_factory=list)


class SearchCriteria(Base):
    pickup_at: datetime
    return_at: datetime
    categories: list[Category] | None = None
    makes: list[str] | None = None
    models: list[str] | None = None
    color: str | None = None
    max_daily_price: Decimal | None = None
    min_passenger_capacity: int | None = None
    driver_age: int | None = None
    delivery_location: str | None = None
    exclude_vehicle_ids: list[str] = Field(default_factory=list)
    limit: int = 3


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


class QuoteLine(Base):
    code: str
    label: str
    amount: Decimal
    #: Deposits are shown but not part of the rental charge.
    is_refundable: bool = False
    #: Set on unit-priced lines so the formatter can render "3 × AED 2,400"
    #: without money formatting leaking into the pricing layer.
    quantity: int | None = None
    unit_price: Decimal | None = None


class Quote(Base):
    quote_id: str
    is_demo: Literal[True] = True
    vehicle_id: str
    vehicle_display_name: str
    currency: str = "AED"

    pickup_at: datetime
    return_at: datetime
    delivery_location: str | None = None

    billable_days: int
    rate_basis: Literal["daily", "weekly", "monthly", "mixed"]

    lines: list[QuoteLine] = Field(default_factory=list)

    rental_subtotal: Decimal
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    delivery_fee: Decimal = Decimal("0")
    collection_fee: Decimal = Decimal("0")
    out_of_hours_fee: Decimal = Decimal("0")
    extras_total: Decimal = Decimal("0")

    subtotal_before_vat: Decimal
    vat_percent: Decimal
    vat_amount: Decimal
    total_charge: Decimal
    deposit: Decimal
    total_due_at_delivery: Decimal

    included_km_total: int
    extra_km_price: Decimal
    insurance_excess: Decimal

    created_at: datetime
    expires_at: datetime
    rules_version: str


class DiscountAllowance(Base):
    """Result of `get_allowed_discount` — the ceiling the agent may offer."""

    max_percent: Decimal
    max_amount: Decimal
    breakdown: dict[str, Decimal]
    requires_human_approval: bool
    rules_version: str


# --------------------------------------------------------------------------
# Customer, reservation, conversation state
# --------------------------------------------------------------------------


class Customer(Base):
    customer_id: str
    whatsapp_id: str
    name: str | None = None
    email: str | None = None
    residency: ResidencyType = ResidencyType.UNKNOWN
    date_of_birth: str | None = None
    driver_age: int | None = None
    documents_on_file: list[str] = Field(default_factory=list)
    #: Learned, non-authoritative preferences (colour, favourite models, tone).
    preferences: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


class Reservation(Base):
    reservation_id: str
    is_demo: Literal[True] = True
    customer_id: str
    vehicle_id: str
    quote_id: str | None = None
    status: ReservationStatus = ReservationStatus.CONFIRMED
    pickup_at: datetime
    return_at: datetime
    delivery_location: str | None = None
    total_charge: Decimal
    deposit: Decimal
    currency: str = "AED"
    created_at: datetime
    updated_at: datetime
    #: Every modification appends here; nothing is overwritten in place.
    history: list[dict[str, Any]] = Field(default_factory=list)


class VehiclePreferences(Base):
    makes: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    color: str | None = None
    budget_per_day: Decimal | None = None
    min_passengers: int | None = None


class ConversationState(Base):
    """The agent's working memory. Source of truth for "what do I already know?".

    The transcript is evidence; this object is memory. Before asking anything the
    agent must consult `missing_requirements()`.
    """

    conversation_id: str
    customer_id: str
    stage: Stage = Stage.NEW_LEAD
    intent: Intent = Intent.UNKNOWN

    pickup_at: datetime | None = None
    return_at: datetime | None = None
    delivery_location: str | None = None
    return_location: str | None = None

    vehicle_preferences: VehiclePreferences = Field(default_factory=VehiclePreferences)

    presented_vehicle_ids: list[str] = Field(default_factory=list)
    selected_vehicle_id: str | None = None
    quote_id: str | None = None
    reservation_id: str | None = None

    driver_age: int | None = None
    residency: ResidencyType = ResidencyType.UNKNOWN

    #: Questions already asked, so the agent can detect its own repetition.
    asked_slots: list[str] = Field(default_factory=list)
    escalated: bool = False
    escalation_reason: str | None = None

    strategy_version: str | None = None
    updated_at: datetime | None = None

    #: Slots required before availability can be checked, in the order to ask them.
    REQUIRED_SLOTS: ClassVar[tuple[str, ...]] = (
        "pickup_at",
        "return_at",
        "delivery_location",
    )

    def missing_requirements(self) -> list[str]:
        """Slots still unknown, in ask-order. Empty means we can search."""
        missing = []
        for slot in self.REQUIRED_SLOTS:
            if getattr(self, slot) is None:
                missing.append(slot)
        return missing

    def has_active_booking(self) -> bool:
        return self.reservation_id is not None
