"""Domain models.

Money is `Decimal` everywhere. The engine quantises to 2dp at the boundaries so
that a quote read back from storage always re-serialises to the same string the
customer was shown.
"""

from __future__ import annotations

from datetime import date, datetime
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
    #: None when the operator does not publish it. Guessing would put a year in
    #: front of the customer that nobody stated — "Mercedes-Benz G63 — 2025"
    #: reads as a fact about the car, not as a placeholder.
    year: int | None = None
    category: Category
    body_type: str
    color: str
    interior_color: str
    daily_price: Decimal
    weekly_price: Decimal | None = None
    monthly_price: Decimal | None = None
    #: None when the operator has not confirmed it. **Zero is a claim** — it
    #: tells a customer this car needs no deposit — so an unknown deposit must
    #: not be encoded as one. Delta's listings advertise "no deposit required
    #: (T&Cs apply)" while their terms require AED 5,000-20,000 subject to the
    #: vehicle, and a range is not something an engine can quote.
    deposit: Decimal | None = None
    included_km_per_day: int
    #: None for the same reason: zero here promises free kilometres past the
    #: allowance. Their terms give a per-vehicle range, not a rate.
    extra_km_price: Decimal | None = None
    features: list[str] = Field(default_factory=list)
    passenger_capacity: int
    luggage_capacity: int
    transmission: str = "automatic"
    images: list[str] = Field(default_factory=list)
    #: Where this vehicle's figures came from, when the config was generated
    #: from an external source. Provenance for exactly one question: "where did
    #: AED 2,199 come from?" — which somebody will ask, and the answer should
    #: not be "I think the importer read it somewhere".
    source_url: str | None = None
    status: VehicleStatus = VehicleStatus.ACTIVE
    blocked_ranges: list[BlockedRange] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        """What the customer is told the car is. Only what is actually known."""
        return f"{self.make} {self.model} — {self.year}" if self.year else f"{self.make} {self.model}"


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
    #: None when the vehicle's deposit is unconfirmed. The total due at delivery
    #: goes with it — a figure that quietly omits an unknown deposit is worse
    #: than no figure, because the customer plans around it.
    deposit: Decimal | None = None
    total_due_at_delivery: Decimal | None = None

    included_km_total: int
    extra_km_price: Decimal | None = None
    insurance_excess: Decimal
    #: True when the excess above is the bottom of a range the insurer sets, so
    #: it must be phrased as "from" rather than as the amount.
    insurance_excess_is_minimum: bool = False

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
    deposit: Decimal | None = None
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
    pickup_date: date | None = None
    return_date: date | None = None
    delivery_location: str | None = None
    return_location: str | None = None

    vehicle_preferences: VehiclePreferences = Field(default_factory=VehiclePreferences)

    presented_vehicle_ids: list[str] = Field(default_factory=list)
    current_vehicle_options: list[str] = Field(default_factory=list)
    selected_vehicle_id: str | None = None
    quote_id: str | None = None
    reservation_id: str | None = None

    driver_age: int | None = None
    residency: ResidencyType = ResidencyType.UNKNOWN

    #: A figure a colleague has been asked for and has not yet supplied. The
    #: conversation carries on — this only forbids stating that one number.
    awaiting_figure: str | None = None

    #: Slots the agent has asked about. Asking twice because the customer never
    #: answered is legitimate, so this alone is not evidence of a mistake.
    asked_slots: list[str] = Field(default_factory=list)
    #: Slots the agent asked for *while already knowing them*. That is the real
    #: repeated-question failure, and it can only be detected at ask time —
    #: by evaluation time the value is present either way.
    redundant_asks: list[str] = Field(default_factory=list)
    validation_findings: list[dict[str, str]] = Field(default_factory=list)

    def unanswered_asks(self, slot: str) -> int:
        """How many times this has been asked while still not supplied.

        A customer who keeps steering back to price is not going to produce a
        delivery address because they were asked a third time. They are telling
        you what they care about, and a salesperson follows that.
        """
        if getattr(self, slot, None) is not None:
            return 0
        return self.asked_slots.count(slot)
    #: Turns in a row the model could not be reached for. One is bad luck; a
    #: second means the customer is stuck behind an outage and needs a person,
    #: not another apology.
    consecutive_provider_failures: int = 0
    escalated: bool = False
    escalation_reason: str | None = None
    #: Where the conversation was before it escalated. Without this, resolving a
    #: case leaves the agent stuck reassuring a customer whose problem has
    #: already been answered.
    stage_before_escalation: Stage | None = None

    strategy_version: str | None = None
    updated_at: datetime | None = None

    #: Needed before availability can be checked at all. A car is free for a
    #: window or it is not; where it gets dropped off does not change that.
    REQUIRED_SLOTS: ClassVar[tuple[str, ...]] = (
        "pickup_at",
        "return_at",
    )

    #: Needed before a total can be quoted, because delivery may carry a fee.
    #: Deliberately not required to *search* — a customer who asks what is
    #: available on Friday should be shown cars, not asked for their address.
    #: Answering "where shall I deliver it?" to "what supercars do you have?" is
    #: how a salesperson loses someone who was ready to buy.
    QUOTE_SLOTS: ClassVar[tuple[str, ...]] = REQUIRED_SLOTS + ("delivery_location",)

    def missing_requirements(self) -> list[str]:
        """Slots still unknown, in ask-order. Empty means we can search."""
        return [slot for slot in self.REQUIRED_SLOTS if getattr(self, slot) is None]

    def missing_for_quote(self) -> list[str]:
        """Slots still unknown before a total can be put in front of them."""
        return [slot for slot in self.QUOTE_SLOTS if getattr(self, slot) is None]

    def has_active_booking(self) -> bool:
        return self.reservation_id is not None
