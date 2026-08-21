"""Vehicle cards.

Renders one image per vehicle straight from `fleet.json`, so what a customer
sees on WhatsApp can never disagree with what the engine charges them. A stock
photo of a white G-Wagon under the caption "black with red interior" is the kind
of small mismatch a rental professional notices immediately, and it costs the
demo more credibility than a photograph buys it.

Cards are also honestly what they are: demonstration material, labelled as such.
When a rental company adopts this, `fleet.json` points at their photographs
instead and nothing else changes — the tool layer already hands back whatever
`images` contains.

Regenerate with:  python -m rental_agent.media.cards
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..config import load_fleet
from ..domain.models import Vehicle

CARD_SIZE = (1200, 800)
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "vehicles"

#: Tried in order. The last entry is PIL's bitmap default, which is unreadable
#: at this size — if we ever fall through to it the cards need a real font.
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

#: Card ground tinted by the car's actual colour, so a shelf of thumbnails is
#: distinguishable at a glance rather than twenty identical rectangles.
_PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "black":  ((22, 22, 26), (245, 245, 247)),
    "white":  ((242, 242, 245), (26, 26, 30)),
    "silver": ((214, 216, 220), (26, 26, 30)),
    "grey":   ((120, 124, 130), (250, 250, 252)),
    "red":    ((122, 26, 30), (252, 246, 246)),
    "blue":   ((26, 52, 104), (244, 247, 252)),
    "yellow": ((222, 176, 32), (28, 24, 12)),
}
_DEFAULT_PALETTE = ((38, 40, 46), (245, 245, 247))

_ACCENT = (198, 154, 78)  # muted gold, legible on both light and dark grounds


def _font(size: int, bold: bool = False) -> Any:
    for path in _FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        if bold and "Bold" not in path and not path.endswith(".ttc"):
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:  # pragma: no cover - font present but unreadable
            continue
    return ImageFont.load_default()


def palette_for(color: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    return _PALETTE.get((color or "").lower(), _DEFAULT_PALETTE)


def _money(value: Any) -> str:
    number = int(value) if value == int(value) else value
    return f"{number:,}"


def _frame(vehicle: Vehicle, operator_name: str, eyebrow: str) -> tuple[Any, Any, Any, Any, Any]:
    """The parts every card shares: ground, header rule, and the demo footer.

    Returns the canvas plus the colours and geometry the card bodies draw with,
    so three different cards cannot drift apart in margin or palette.
    """
    background, ink = palette_for(vehicle.color)
    muted = tuple(
        int(i * 0.62 + b * 0.38) for i, b in zip(ink, background)
    )  # ink faded toward the ground

    card = Image.new("RGB", CARD_SIZE, background)
    draw = ImageDraw.Draw(card)
    left, width = 80, CARD_SIZE[0] - 160

    draw.text((left, 64), operator_name.upper(), font=_font(30, bold=True), fill=muted)
    if eyebrow:
        label = eyebrow.upper()
        draw.text(
            (left + width - draw.textlength(label, font=_font(30, bold=True)), 64),
            label,
            font=_font(30, bold=True),
            fill=_ACCENT,
        )
    draw.line([(left, 124), (left + width, 124)], fill=_ACCENT, width=3)

    # The demonstration notice is not optional on any card.
    draw.line([(left, 692), (left + width, 692)], fill=_ACCENT, width=2)
    draw.text(
        (left, 716),
        "DEMONSTRATION VEHICLE · NOT A REAL BOOKING",
        font=_font(26, bold=True),
        fill=muted,
    )
    return card, draw, ink, muted, (left, width)


def render(vehicle: Vehicle, operator_name: str, currency: str = "AED") -> Image.Image:
    """The hero card — what the car is and what it costs."""
    card, draw, ink, muted, (left, width) = _frame(vehicle, operator_name, "")

    draw.text((left, 190), f"{vehicle.make} {vehicle.model}", font=_font(74, bold=True), fill=ink)
    draw.text((left, 288), str(vehicle.year), font=_font(44), fill=muted)
    draw.text(
        (left, 356),
        f"{vehicle.color.title()} · {vehicle.interior_color} interior",
        font=_font(34),
        fill=muted,
    )

    draw.text(
        (left, 456),
        f"{currency} {_money(vehicle.daily_price)}",
        font=_font(86, bold=True),
        fill=ink,
    )
    draw.text((left, 556), "per day", font=_font(32), fill=muted)

    # Facts, right-aligned so the price stays the focal point
    facts = [
        f"{vehicle.included_km_per_day} km included per day",
        f"{currency} {_money(vehicle.deposit)} refundable deposit",
        f"{vehicle.passenger_capacity} seats · {vehicle.transmission}",
    ]
    small = _font(30)
    for index, fact in enumerate(facts):
        text_width = draw.textlength(fact, font=small)
        draw.text((left + width - text_width, 456 + index * 46), fact, font=small, fill=muted)

    return card


def render_specification(
    vehicle: Vehicle, operator_name: str, currency: str = "AED"
) -> Image.Image:
    """The numbers a customer compares two cars on."""
    card, draw, ink, muted, (left, width) = _frame(vehicle, operator_name, "specification")

    draw.text((left, 176), f"{vehicle.make} {vehicle.model}", font=_font(52, bold=True), fill=ink)

    rows = [
        ("Seats", str(vehicle.passenger_capacity)),
        ("Luggage", f"{vehicle.luggage_capacity} bags"),
        ("Transmission", vehicle.transmission.title()),
        ("Body", vehicle.body_type.title()),
        ("Mileage included", f"{vehicle.included_km_per_day} km / day"),
        ("Extra kilometres", f"{currency} {vehicle.extra_km_price} / km"),
    ]
    label_font, value_font = _font(32), _font(32, bold=True)
    for index, (label, value) in enumerate(rows):
        y = 276 + index * 66
        draw.text((left, y), label, font=label_font, fill=muted)
        draw.text(
            (left + width - draw.textlength(value, font=value_font), y),
            value,
            font=value_font,
            fill=ink,
        )
        if index < len(rows) - 1:
            draw.line([(left, y + 50), (left + width, y + 50)], fill=muted, width=1)

    return card


def render_features(vehicle: Vehicle, operator_name: str, currency: str = "AED") -> Image.Image:
    """What is actually in the car. Falls back to the deposit terms when a
    vehicle lists no features, so every car still has three cards."""
    card, draw, ink, muted, (left, width) = _frame(vehicle, operator_name, "what's included")

    draw.text((left, 176), f"{vehicle.make} {vehicle.model}", font=_font(52, bold=True), fill=ink)

    lines = list(vehicle.features) or [
        f"{vehicle.included_km_per_day} km included every day",
        f"{currency} {_money(vehicle.deposit)} deposit, refunded on return",
        "Comprehensive insurance included",
    ]
    body = _font(36)
    for index, line in enumerate(lines[:7]):
        y = 286 + index * 56
        draw.text((left, y), "—", font=body, fill=_ACCENT)
        draw.text((left + 46, y), line, font=body, fill=ink if index < 4 else muted)

    return card


#: Every card a vehicle gets, in the order a customer should receive them: what
#: it is, what it measures, what comes with it.
RENDERERS = [
    ("", render),
    ("spec", render_specification),
    ("features", render_features),
]


def paths_for(vehicle_id: str) -> list[str]:
    """The fleet-relative image paths for one vehicle.

    The single place that knows the naming scheme, so `fleet.json` and the
    renderer cannot disagree about which files exist.
    """
    return [
        f"assets/vehicles/{vehicle_id}.png" if not suffix
        else f"assets/vehicles/{vehicle_id}_{suffix}.png"
        for suffix, _ in RENDERERS
    ]


def generate_all(output_dir: Path | None = None) -> list[Path]:
    """Render every card for every vehicle. Returns the paths written."""
    operator, vehicles = load_fleet()
    target = output_dir or OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)

    written = []
    for vehicle in vehicles:
        for suffix, renderer in RENDERERS:
            name = vehicle.id if not suffix else f"{vehicle.id}_{suffix}"
            path = target / f"{name}.png"
            renderer(vehicle, operator.demo_company_name, operator.currency).save(path, "PNG")
            written.append(path)
    return written


if __name__ == "__main__":  # pragma: no cover - manual regeneration
    paths = generate_all()
    print(f"wrote {len(paths)} cards to {paths[0].parent}")
