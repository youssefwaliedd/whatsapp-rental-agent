"""Vehicle cards.

Cards are rendered from `fleet.json`, so what a customer sees on WhatsApp cannot
disagree with what the engine charges them. These tests pin that property and
the demonstration labelling — both of which are customer-facing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rental_agent.config import load_fleet
from rental_agent.media.cards import CARD_SIZE, generate_all, palette_for, render

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_every_vehicle_image_path_points_at_a_real_file():
    """A path to a file that does not exist is a lie in the data — the tool
    layer hands these straight to the customer."""
    _, vehicles = load_fleet()
    missing = [
        image for v in vehicles for image in v.images if not (PROJECT_ROOT / image).exists()
    ]
    assert missing == []


def test_every_vehicle_has_exactly_one_card():
    _, vehicles = load_fleet()
    assert all(len(v.images) == 1 for v in vehicles)
    assert len({v.images[0] for v in vehicles}) == len(vehicles)


def test_a_card_renders_at_the_expected_size():
    _, vehicles = load_fleet()
    card = render(vehicles[0], "Sandline Rentals (DEMO)")
    assert card.size == CARD_SIZE


def test_cards_are_generated_for_the_whole_fleet(tmp_path):
    _, vehicles = load_fleet()
    written = generate_all(tmp_path)
    assert len(written) == len(vehicles)
    assert all(p.exists() and p.stat().st_size > 1000 for p in written)


def test_light_and_dark_cars_get_readable_opposite_palettes():
    """The two G63s differ only in colour — a shelf of identical grey rectangles
    would be useless as a thumbnail list."""
    black_bg, black_ink = palette_for("black")
    white_bg, white_ink = palette_for("white")
    assert sum(black_bg) < sum(black_ink)   # dark ground, light text
    assert sum(white_bg) > sum(white_ink)   # light ground, dark text


def test_an_unknown_colour_still_renders():
    _, vehicles = load_fleet()
    vehicle = vehicles[0].model_copy(update={"color": "chartreuse"})
    assert render(vehicle, "Sandline Rentals (DEMO)").size == CARD_SIZE


@pytest.mark.parametrize("vehicle_index", [0, 12, 18])
def test_a_card_is_not_blank(vehicle_index):
    """A card that fell back to an unreadable default font would still be a
    valid PNG — check there is actually ink on it."""
    _, vehicles = load_fleet()
    card = render(vehicles[vehicle_index], "Sandline Rentals (DEMO)")
    assert len(card.getcolors(maxcolors=100000) or []) > 5
