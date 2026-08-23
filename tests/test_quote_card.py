"""The figures go out as the engine rendered them.

Observed on 23 Aug, one message before the customer would have booked: the agent
quoted "The total for the rental is AED 1,887.90" and never mentioned the deposit
at all. Nothing was wrong with the tool result — it carried `deposit: null` and
said so — but the customer only ever saw the model's prose summary of it.

`formatting.quote_message` existed for exactly this and was wired only into the
scripted demo. So a quote now sends the rendered breakdown alongside the reply,
the way photographs already worked: the model decides when a quote happens and
writes the sentence around it, and does not decide which parts of it the customer
sees.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rental_agent.tools.registry import execute_tool

from .conftest import dt


@pytest.fixture
def quoted(booking_ctx):
    """A quote, made the way the agent makes one."""
    vehicle = booking_ctx.engine.list_fleet()[0]
    result = execute_tool(
        booking_ctx,
        "create_demo_quote",
        {
            "vehicle_id": vehicle.id,
            "pickup_at": dt(10, 17).isoformat(),
            "return_at": dt(12, 17).isoformat(),
            "delivery_location": "Dubai Marina",
        },
    )
    assert "error" not in result, result
    return result


def test_a_quote_stages_the_rendered_figures(booking_ctx, quoted):
    cards = booking_ctx.take_cards()
    assert len(cards) == 1
    card = cards[0]
    assert quoted["quote_id"] in card
    assert "Total:" in card
    # The line that went missing.
    assert "deposit" in card.lower()


def test_the_breakdown_carries_the_demonstration_notice(booking_ctx, quoted):
    assert "demonstration" in booking_ctx.take_cards()[0].lower()


def test_an_unconfirmed_deposit_is_stated_rather_than_omitted(booking_ctx):
    from rental_agent.formatting import UNCONFIRMED

    vehicle = booking_ctx.engine.list_fleet()[0].model_copy(update={"deposit": None})
    booking_ctx.engine._vehicles = tuple(
        vehicle if v.id == vehicle.id else v for v in booking_ctx.engine.list_fleet()
    )
    booking_ctx.engine._by_id[vehicle.id] = vehicle

    execute_tool(
        booking_ctx,
        "create_demo_quote",
        {
            "vehicle_id": vehicle.id,
            "pickup_at": dt(10, 17).isoformat(),
            "return_at": dt(12, 17).isoformat(),
        },
    )
    card = booking_ctx.take_cards()[0]
    assert UNCONFIRMED in card
    assert "plus the deposit once confirmed" in card


def test_the_model_is_told_not_to_repeat_the_breakdown(quoted):
    assert quoted["card_sent"] is True
    assert "already been sent" in quoted["card_note"]


def test_a_null_figure_never_reaches_the_model_as_the_string_none(booking_ctx):
    vehicle = booking_ctx.engine.list_fleet()[0].model_copy(update={"deposit": None})
    booking_ctx.engine._by_id[vehicle.id] = vehicle
    booking_ctx.engine._vehicles = tuple(
        vehicle if v.id == vehicle.id else v for v in booking_ctx.engine.list_fleet()
    )
    result = execute_tool(
        booking_ctx,
        "create_demo_quote",
        {"vehicle_id": vehicle.id, "pickup_at": dt(10, 17).isoformat(),
         "return_at": dt(12, 17).isoformat()},
    )
    assert result["deposit"] is None
    assert result["total_due_at_delivery"] is None


def test_draining_means_a_retried_turn_cannot_send_it_twice(booking_ctx, quoted):
    assert booking_ctx.take_cards()
    assert booking_ctx.take_cards() == []
