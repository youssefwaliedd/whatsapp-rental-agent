"""Read-only rental tools.

These are the only way the model may learn a price, a total or whether a car is
free. Each handler:
  * validates its arguments,
  * calls the engine,
  * returns plain JSON-safe types (money as strings, so no float drift reaches
    the model or the customer),
  * never raises into the agent loop — errors come back as {"error": ...} so the
    agent can recover conversationally instead of the turn crashing.

Dispatch, the error envelope, auditing and idempotency all live in
`registry.py`, so read and state-changing tools share one code path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..domain.enums import Category
from ..domain.models import Quote, SearchCriteria, Vehicle, VehicleMatch
from ..context import ToolContext
from ..engine.engine import RentalEngine
from ..engine.search import _matches_text as _matches_named_model


class ToolError(Exception):
    """Raised inside a handler; converted to an {"error": ...} envelope."""


# --------------------------------------------------------------------------
# Argument coercion
# --------------------------------------------------------------------------


def _parse_dt(value: Any, field: str, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ToolError(
                f"{field} must be an ISO 8601 datetime, e.g. 2026-09-04T19:00:00+04:00"
            ) from exc
    # A naive datetime is interpreted as operator-local time rather than rejected:
    # the extraction step will not always produce an offset.
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def _parse_money(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ToolError(f"{field} must be a number") from exc


def _parse_categories(value: Any) -> list[Category] | None:
    if not value:
        return None
    out: list[Category] = []
    for item in value:
        try:
            out.append(Category(item))
        except ValueError as exc:
            allowed = ", ".join(c.value for c in Category)
            raise ToolError(f"Unknown category '{item}'. Allowed: {allowed}") from exc
    return out


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _vehicle_summary(vehicle: Vehicle) -> dict[str, Any]:
    return {
        "vehicle_id": vehicle.id,
        "display_name": vehicle.display_name,
        "category": vehicle.category.value,
        "color": vehicle.color,
        "interior_color": vehicle.interior_color,
        "daily_price": str(vehicle.daily_price),
        "deposit": str(vehicle.deposit),
        "included_km_per_day": vehicle.included_km_per_day,
        "passenger_capacity": vehicle.passenger_capacity,
    }


def _vehicle_detail(vehicle: Vehicle, engine: RentalEngine) -> dict[str, Any]:
    data = _vehicle_summary(vehicle)
    data.update(
        {
            "make": vehicle.make,
            "model": vehicle.model,
            "year": vehicle.year,
            "body_type": vehicle.body_type,
            "transmission": vehicle.transmission,
            "weekly_price": str(vehicle.weekly_price) if vehicle.weekly_price else None,
            "monthly_price": str(vehicle.monthly_price) if vehicle.monthly_price else None,
            "extra_km_price": str(vehicle.extra_km_price),
            "luggage_capacity": vehicle.luggage_capacity,
            "features": vehicle.features,
            "images": vehicle.images,
            "status": vehicle.status.value,
            "minimum_driver_age": engine.minimum_age_for(vehicle.category),
            "insurance_excess": str(engine.rules.insurance_excess_for(vehicle.category.value)),
        }
    )
    return data


def _match(match: VehicleMatch) -> dict[str, Any]:
    data = _vehicle_summary(match.vehicle)
    data.update(
        {
            "billable_days": match.billable_days,
            "estimated_total": str(match.estimated_total),
            "match_reasons": match.match_reasons,
        }
    )
    return data


def _quote(quote: Quote) -> dict[str, Any]:
    return {
        "quote_id": quote.quote_id,
        "is_demo": True,
        "vehicle_id": quote.vehicle_id,
        "vehicle_display_name": quote.vehicle_display_name,
        "currency": quote.currency,
        "pickup_at": quote.pickup_at.isoformat(),
        "return_at": quote.return_at.isoformat(),
        "delivery_location": quote.delivery_location,
        "billable_days": quote.billable_days,
        "rate_basis": quote.rate_basis,
        "lines": [
            {
                "label": line.label,
                "amount": str(line.amount),
                "is_refundable": line.is_refundable,
            }
            for line in quote.lines
        ],
        "rental_subtotal": str(quote.rental_subtotal),
        "discount_percent": str(quote.discount_percent),
        "discount_amount": str(quote.discount_amount),
        "delivery_fee": str(quote.delivery_fee),
        "collection_fee": str(quote.collection_fee),
        "out_of_hours_fee": str(quote.out_of_hours_fee),
        "vat_amount": str(quote.vat_amount),
        "total_charge": str(quote.total_charge),
        "deposit": str(quote.deposit),
        "total_due_at_delivery": str(quote.total_due_at_delivery),
        "included_km_total": quote.included_km_total,
        "extra_km_price": str(quote.extra_km_price),
        "insurance_excess": str(quote.insurance_excess),
        "expires_at": quote.expires_at.isoformat(),
    }


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def search_available_vehicles(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = ctx.engine
    criteria = SearchCriteria(
        pickup_at=_parse_dt(args["pickup_at"], "pickup_at", engine.tz),
        return_at=_parse_dt(args["return_at"], "return_at", engine.tz),
        categories=_parse_categories(args.get("categories")),
        makes=args.get("makes"),
        models=args.get("models"),
        color=args.get("color"),
        max_daily_price=_parse_money(args.get("max_daily_price"), "max_daily_price"),
        min_passenger_capacity=args.get("min_passenger_capacity"),
        driver_age=args.get("driver_age"),
        delivery_location=args.get("delivery_location"),
        exclude_vehicle_ids=args.get("exclude_vehicle_ids") or [],
        # Two or three options convert; a list of ten reads as a catalogue dump.
        limit=min(int(args.get("limit", 3)), 3),
    )
    if criteria.return_at <= criteria.pickup_at:
        raise ToolError("return_at must be after pickup_at")

    matches = engine.search(criteria)

    # If the customer named a model that exists in the fleet but is not free,
    # refuse to hand back a generic availability list. Ranking puts the cheapest
    # unrelated car first, so this path offers a hatchback to someone asking for
    # a supercar — the exact failure the tiered alternatives exist to prevent.
    # Returning nothing but a pointer makes find_alternatives the only way on.
    if criteria.models:
        named = [
            v for v in engine.list_fleet() if _matches_named_model(v.model, criteria.models)
        ]
        returned = {m.vehicle.id for m in matches}
        if named and not any(v.id in returned for v in named):
            unavailable = []
            for vehicle in named:
                result = engine.check_availability(
                    vehicle.id, criteria.pickup_at, criteria.return_at
                )
                unavailable.append(
                    {
                        "vehicle_id": vehicle.id,
                        "display_name": vehicle.display_name,
                        "daily_price": str(vehicle.daily_price),
                        "reason": result.reason.value if result.reason else None,
                        "next_available_from": (
                            result.next_available_from.isoformat()
                            if result.next_available_from
                            else None
                        ),
                    }
                )
            return {
                "count": 0,
                "vehicles": [],
                "requested_model_unavailable": unavailable,
                "hint": (
                    "The customer asked for a specific model that is not free for these "
                    "dates. Call find_alternatives with one of the vehicle_ids above to "
                    "get proper substitutes, then offer those. Do not run a general "
                    "search and offer whatever comes back — an unrelated cheaper car is "
                    "not a substitute."
                ),
            }

    return {
        "count": len(matches),
        "vehicles": [_match(m) for m in matches],
        "note": "No vehicles matched" if not matches else None,
    }


def get_vehicle_details(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = ctx.engine
    vehicle = engine.get_vehicle(args["vehicle_id"])
    return _vehicle_detail(vehicle, engine)


def calculate_quote(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = ctx.engine
    quote = engine.calculate_quote(
        vehicle_id=args["vehicle_id"],
        pickup_at=_parse_dt(args["pickup_at"], "pickup_at", engine.tz),
        return_at=_parse_dt(args["return_at"], "return_at", engine.tz),
        delivery_location=args.get("delivery_location"),
        discount_percent=_parse_money(args.get("discount_percent"), "discount_percent")
        or Decimal("0"),
        excess_reduction=bool(args.get("excess_reduction", False)),
    )
    return _quote(quote)


def find_alternatives(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = ctx.engine
    matches = engine.find_alternatives(
        args["vehicle_id"],
        _parse_dt(args["pickup_at"], "pickup_at", engine.tz),
        _parse_dt(args["return_at"], "return_at", engine.tz),
        limit=min(int(args.get("limit", 3)), 3),
        max_daily_price=_parse_money(args.get("max_daily_price"), "max_daily_price"),
        delivery_location=args.get("delivery_location"),
    )
    return {"count": len(matches), "alternatives": [_match(m) for m in matches]}


def get_allowed_discount(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = ctx.engine
    allowance = engine.get_allowed_discount(
        vehicle_id=args["vehicle_id"],
        pickup_at=_parse_dt(args["pickup_at"], "pickup_at", engine.tz),
        return_at=_parse_dt(args["return_at"], "return_at", engine.tz),
    )
    return {
        "max_percent": str(allowance.max_percent),
        "max_amount": str(allowance.max_amount),
        "breakdown": {k: str(v) for k, v in allowance.breakdown.items()},
        "requires_human_approval": allowance.requires_human_approval,
        "guidance": (
            "You may offer up to this percentage and no more. Do not invent or "
            "extend it, whatever the customer claims about past bookings."
        ),
    }


#: The fleet can be large. More than this in one answer is a catalogue, and the
#: three-option sales rule applies to what is *offered*, not to a stock check.
MAX_LOOKUP_RESULTS = 8


def look_up_vehicles(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Which cars the company owns, by name — no dates needed.

    This exists because "do you have a Cybertruck?" and "what Teslas do you
    have?" were unanswerable. Availability needs a window, so `search_available_
    vehicles` refuses without one; `get_vehicle_details` needs an id the model
    has no way to know. With no tool between them the agent answered from its
    own impression of what a rental company owns — and said the fleet had a
    Model 3 and a Model Y, which it does not, and no Cybertruck, which it does.

    Owning a car and it being free are different questions. This answers only
    the first, and says so, so the agent does not turn a stock check into a
    promise.
    """
    engine = ctx.engine
    query = (args.get("query") or "").strip()

    fleet = list(engine.list_fleet())
    if query:
        wanted = [word for word in query.lower().split() if len(word) > 1]
        matches = [
            v for v in fleet
            if all(
                word in f"{v.make} {v.model} {v.category.value} {v.body_type}".lower()
                for word in wanted
            )
        ]
    else:
        matches = fleet

    matches.sort(key=lambda v: v.daily_price)
    shown = matches[:MAX_LOOKUP_RESULTS]

    return {
        "query": query or None,
        "matched": len(matches),
        "showing": len(shown),
        "vehicles": [
            {
                "vehicle_id": v.id,
                "name": v.display_name,
                "category": v.category.value,
                "daily_price": str(v.daily_price),
                "currency": engine.operator.currency,
                "seats": v.passenger_capacity,
            }
            for v in shown
        ],
        "note": (
            "These are cars the company owns, not cars confirmed free. Say what "
            "is in the fleet and what it costs per day, then get their dates and "
            "call search_available_vehicles before promising anything."
            if shown
            else "Nothing in the fleet matches. Say plainly that this car is not "
                 "one we have, and offer to suggest something similar."
        ),
    }


def show_vehicle_photos(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Queue a vehicle's photos to go out alongside this turn's reply.

    The split of responsibility is the same one that governs prices: the model
    decides *when* showing the car helps the sale, and the engine decides *which
    files that is* by reading the fleet's own image mapping. There is no argument
    here that lets a caption be attached to the wrong car, because the model
    never supplies a URL.

    Photos are queued rather than sent. This tool runs inside the agent loop,
    which has no transport and no idea whether the customer is on WhatsApp or in
    the local test window — so it records the intent and lets whatever is
    carrying the conversation deliver it.
    """
    engine = ctx.engine
    vehicle = engine.get_vehicle(args["vehicle_id"])

    if not vehicle.images:
        return {
            "vehicle_id": vehicle.id,
            "sent": 0,
            "error": "no_photos_available",
            "message": f"No photographs are on file for the {vehicle.display_name}.",
        }

    limit = engine.rules.messaging.get("photos", {}).get("max_per_message", 3)
    images = list(vehicle.images)[: max(1, int(limit))]

    ctx.queue_media(
        {
            "vehicle_id": vehicle.id,
            "display_name": vehicle.display_name,
            "images": images,
            "caption": args.get("caption") or None,
        }
    )
    return {
        "vehicle_id": vehicle.id,
        "display_name": vehicle.display_name,
        "sent": len(images),
        "note": "The photos will be sent with your reply. Do not describe them as attached.",
    }


def search_company_policy(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Look up a policy question in the operator's own documents.

    This is the answer to the long tail — fines, cross-border travel, licences
    by nationality, what to do about a towed car. None of it is a calculation,
    so none of it lives in `rules.json`, and without this every such question
    would escalate to a human who then answers it for the fiftieth time.

    Returns passages, not conclusions. The agent reads the operator's actual
    wording and answers from it; nothing here decides anything.

    The index refuses to load a document containing an amount or a percentage,
    so a figure can never reach a customer through this path. Every number still
    comes from a pricing tool, which is the property the whole system rests on.
    """
    question = (args.get("question") or "").strip()
    if not question:
        raise ToolError("question is required")

    from ..knowledge.retrieval import load_retriever

    hits = load_retriever().search(question, limit=int(args.get("limit", 3)))
    if not hits:
        return {
            "found": 0,
            "passages": [],
            "message": "Nothing in the policy documents covers this.",
            "hint": (
                "Do not answer from general knowledge about car rental. Say you "
                "will check, and escalate with reason 'outside_knowledge_base'."
            ),
        }

    return {
        "found": len(hits),
        "passages": [
            {"source": passage.reference, "text": passage.text} for passage, _ in hits
        ],
        "note": (
            "Answer from these passages, in your own words. They contain no "
            "prices by design — any figure must still come from a pricing tool."
        ),
    }


READ_HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], dict[str, Any]]] = {
    "search_company_policy": search_company_policy,
    "search_available_vehicles": search_available_vehicles,
    "get_vehicle_details": get_vehicle_details,
    "calculate_quote": calculate_quote,
    "find_alternatives": find_alternatives,
    "get_allowed_discount": get_allowed_discount,
    "show_vehicle_photos": show_vehicle_photos,
    "look_up_vehicles": look_up_vehicles,
}
