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


#: What the customer sees where a figure the operator has not confirmed would
#: otherwise be printed. Deliberately not a number and deliberately not silence:
#: silence lets the customer assume there is nothing to pay.
UNCONFIRMED = "confirmed for this car before booking"


def vehicle_card(vehicle: Vehicle, currency: str = "AED") -> str:
    deposit = (
        f"{amount(vehicle.deposit, currency)} refundable deposit"
        if vehicle.deposit is not None
        else f"Refundable deposit — {UNCONFIRMED}"
    )
    return "\n".join(
        [
            f"*{vehicle.display_name}*",
            f"{vehicle.color.title()} with {vehicle.interior_color} interior",
            f"{amount(vehicle.daily_price, currency)} per day",
            f"{vehicle.included_km_per_day} km included per day",
            deposit,
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

    lines += ["", f"*Total: {amount(quote.total_charge, quote.currency)}*"]

    if quote.deposit is not None:
        lines.append(f"Refundable deposit: {amount(quote.deposit, quote.currency)}")
    else:
        lines.append(f"Refundable deposit: {UNCONFIRMED}")

    if quote.total_due_at_delivery is not None:
        lines.append(f"Due at delivery: {amount(quote.total_due_at_delivery, quote.currency)}")
    else:
        # Stating the rental total here would read as the whole amount due, with
        # a deposit the customer has not been told about arriving at handover.
        lines.append("Due at delivery: the total above, plus the deposit once confirmed")

    per_km = (
        f"{amount(quote.extra_km_price, quote.currency)}/km after"
        if quote.extra_km_price is not None
        else f"the rate beyond that is {UNCONFIRMED}"
    )
    lines += [
        "",
        f"{quote.included_km_total} km included · {per_km}",
        f"Insurance excess: {excess(quote)}",
        "",
        demo_footer(rules),
    ]
    return "\n".join(lines)


def excess(quote: Quote) -> str:
    """`from AED 5,000` where the operator publishes a floor rather than a figure.

    The word carries real weight: the excess is what a customer owes after an
    accident, and printing the bottom of the insurer's range as though it were
    the amount understates their exposure at the worst possible moment.
    """
    shown = amount(quote.insurance_excess, quote.currency)
    return f"from {shown}" if quote.insurance_excess_is_minimum else shown


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
