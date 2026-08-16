"""Closed vocabularies shared by the engine, the state store and the tool layer.

Anything the agent is allowed to *set* is an enum, so an LLM cannot smuggle in a
made-up stage, category or status by emitting free text.
"""

from enum import Enum


class Category(str, Enum):
    ECONOMY = "economy"
    SEDAN = "sedan"
    LUXURY_SEDAN = "luxury_sedan"
    SUV = "suv"
    LUXURY_SUV = "luxury_suv"
    SPORTS = "sports"
    SUPERCAR = "supercar"
    CONVERTIBLE = "convertible"


#: Ordered cheapest-to-most-expensive. Used to find "one step up / one step down"
#: neighbours when the requested vehicle is unavailable.
CATEGORY_LADDER: list[Category] = [
    Category.ECONOMY,
    Category.SEDAN,
    Category.SUV,
    Category.LUXURY_SEDAN,
    Category.CONVERTIBLE,
    Category.LUXURY_SUV,
    Category.SPORTS,
    Category.SUPERCAR,
]


class VehicleStatus(str, Enum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"


class Stage(str, Enum):
    NEW_LEAD = "new_lead"
    QUALIFYING = "qualifying"
    CHECKING_AVAILABILITY = "checking_availability"
    OPTIONS_PRESENTED = "options_presented"
    QUOTED = "quoted"
    NEGOTIATING = "negotiating"
    CUSTOMER_DETAILS_PENDING = "customer_details_pending"
    DOCUMENTS_PENDING = "documents_pending"
    PAYMENT_PENDING = "payment_pending"
    RESERVED = "reserved"
    DELIVERY_SCHEDULED = "delivery_scheduled"
    ACTIVE_RENTAL = "active_rental"
    EXTENSION_REQUESTED = "extension_requested"
    RETURN_PENDING = "return_pending"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    LOST_LEAD = "lost_lead"


class Intent(str, Enum):
    NEW_RENTAL = "new_rental"
    MODIFY_BOOKING = "modify_booking"
    EXTEND_RENTAL = "extend_rental"
    CANCEL_BOOKING = "cancel_booking"
    ASK_QUESTION = "ask_question"
    REPORT_PROBLEM = "report_problem"
    SMALL_TALK = "small_talk"
    UNKNOWN = "unknown"


class ResidencyType(str, Enum):
    UAE_RESIDENT = "uae_resident"
    TOURIST = "tourist"
    GCC_RESIDENT = "gcc_resident"
    UNKNOWN = "unknown"


class UnavailabilityReason(str, Enum):
    ON_RENT = "on_rent"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"
    OUTSIDE_RENTAL_LIMITS = "outside_rental_limits"


class ReservationStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
