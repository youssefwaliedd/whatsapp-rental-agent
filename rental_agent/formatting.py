"""WhatsApp message formatting.

Deterministic rendering of engine facts. The model chooses *what* to send and
writes the conversational text around it; these helpers render the numbers, so a
price can never be reworded into something the engine did not produce.

WhatsApp markup: *bold*, _italic_, ```mono```.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from .config import Rules
from .domain.models import Quote, Vehicle, VehicleMatch


def amount(value: Decimal | int | str, currency: str = "AED") -> str:
    """`AED 2,400` — trailing .00 dropped, thousands separated."""
    dec = Decimal(str(value))
    if dec == dec.to_integral_value():
        return f"{currency} {int(dec):,}"
    return f"{currency} {dec:,.2f}"


def days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def line_label(line) -> str:
    """`Daily rate × 3` becomes `3 × AED 2,400` where a unit price is known."""
    if line.quantity and line.unit_price is not None:
        unit = line.label.split(" × ")[0]
        return f"{line.quantity} × {amount(line.unit_price)}  ({unit})"
    return line.label


def when(moment: datetime) -> str:
    """`Friday 4 Sep, 7:00 PM` — the weekday matters, customers book by day name."""
    hour = moment.strftime("%I").lstrip("0") or "12"
    return f"{moment.strftime('%A')} {moment.day} {moment.strftime('%b')}, {hour}:{moment.strftime('%M %p')}"


def vehicle_card(vehicle: Vehicle, currency: str = "AED") -> str:
    return "\n".join(
        [
            f"*{vehicle.display_name}*",
            f"{vehicle.color.title()} with {vehicle.interior_color} interior",
            f"{amount(vehicle.daily_price, currency)} per day",
            f"{vehicle.included_km_per_day} km included per day",
            f"{amount(vehicle.deposit, currency)} refundable deposit",
        ]
    )


def option_list(matches: list[VehicleMatch], currency: str = "AED") -> str:
    """Two or three options, each with its all-in total for the requested dates."""
    blocks = []
    for index, match in enumerate(matches, start=1):
        total = amount(match.estimated_total, currency)
        blocks.append(
            f"{index}. {vehicle_card(match.vehicle, currency)}\n"
            f"Total for {days(match.billable_days)}: *{total}*"
        )
    return "\n\n".join(blocks)


def quote_message(quote: Quote, rules: Rules) -> str:
    lines = [
        f"*Demo quote {quote.quote_id}*",
        quote.vehicle_display_name,
        f"{when(quote.pickup_at)} → {when(quote.return_at)}  ({days(quote.billable_days)})",
    ]
    if quote.delivery_location:
        lines.append(f"Delivery: {quote.delivery_location}")
    lines.append("")

    for line in quote.lines:
        if line.is_refundable:
            continue
        lines.append(f"{line_label(line)} — {amount(line.amount, quote.currency)}")

    lines += [
        "",
        f"*Total: {amount(quote.total_charge, quote.currency)}*",
        f"Refundable deposit: {amount(quote.deposit, quote.currency)}",
        f"Due at delivery: {amount(quote.total_due_at_delivery, quote.currency)}",
        "",
        f"{quote.included_km_total} km included · "
        f"{amount(quote.extra_km_price, quote.currency)}/km after",
        f"Insurance excess: {amount(quote.insurance_excess, quote.currency)}",
        "",
        demo_footer(rules),
    ]
    return "\n".join(lines)


def demo_footer(rules: Rules) -> str:
    """Every quote, confirmation and modification carries this. Non-negotiable —
    the prototype must never read as a real booking."""
    return f"_{rules.demo_disclosure['required_footer']}_"


def _squash(text: str) -> str:
    """Lowercase, punctuation-free, single-spaced — for comparing two strings
    that a customer would read as the same sentence."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def photo_caption(caption: str | None, fallback: str, reply: str) -> str | None:
    """What goes under the first photo, given what was already said in the reply.

    A model asked for a caption will often produce the sentence it just sent —
    which on a phone is the same line printed twice, once as a message and once
    under the picture. Rather than ask the prompt to remember not to, the
    duplicate is detected and dropped here.

    Falls back to the vehicle's name, which is never wrong and tells the
    customer which car they are looking at when several were discussed.
    """
    written, spoken = _squash(caption), _squash(reply)
    if written and spoken and (written in spoken or spoken in written):
        return fallback or None
    return (caption or "").strip() or fallback or None
