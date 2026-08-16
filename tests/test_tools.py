"""The tool boundary the model will sit behind.

These tests exist because a malformed tool call must never crash a customer
conversation, and because every number crossing this boundary must be a string —
floats reaching the model is how "AED 7560.000000001" ends up in a WhatsApp
message.
"""

from __future__ import annotations

import json

from rental_agent.tools.registry import execute_tool
from tests.conftest import dt

WINDOW = {"pickup_at": dt(4, 19).isoformat(), "return_at": dt(7, 19).isoformat()}


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


def test_search_returns_json_safe_results(ctx):
    result = execute_tool(
        ctx, "search_available_vehicles", {**WINDOW, "models": ["G63"], "color": "black"}
    )
    assert result["count"] >= 1
    json.dumps(result)  # must serialise without a custom encoder
    assert result["vehicles"][0]["vehicle_id"] == "veh_13"
    assert isinstance(result["vehicles"][0]["estimated_total"], str)


def test_quote_totals_cross_the_boundary_as_strings(ctx):
    result = execute_tool(ctx, "calculate_quote", {**WINDOW, "vehicle_id": "veh_13"})
    assert result["total_charge"] == "7560.00"
    assert result["is_demo"] is True
    for key in ("rental_subtotal", "vat_amount", "deposit", "total_due_at_delivery"):
        assert isinstance(result[key], str)


def test_vehicle_details_include_the_policy_facts_the_agent_needs(ctx):
    result = execute_tool(ctx, "get_vehicle_details", {"vehicle_id": "veh_18"})
    assert result["minimum_driver_age"] == 30
    assert result["insurance_excess"] == "15000"
    assert result["features"]


def test_discount_tool_states_the_ceiling_and_its_source(ctx):
    result = execute_tool(ctx, "get_allowed_discount", {**WINDOW, "vehicle_id": "veh_13"})
    assert result["max_percent"] == "12"
    assert result["requires_human_approval"] is False
    assert "Do not invent" in result["guidance"]


def test_alternatives_tool_returns_available_substitutes(ctx):
    result = execute_tool(ctx, "find_alternatives", {**WINDOW, "vehicle_id": "veh_18"})
    assert result["count"] >= 1
    assert all(v["vehicle_id"] != "veh_18" for v in result["alternatives"])


# --------------------------------------------------------------------------
# Error envelopes — the agent must always get something it can act on
# --------------------------------------------------------------------------


def test_unknown_tool_is_reported_not_raised(ctx):
    result = execute_tool(ctx, "book_a_flight", {})
    assert result["error"] == "unknown_tool"


def test_missing_argument_is_reported(ctx):
    result = execute_tool(ctx, "calculate_quote", {"vehicle_id": "veh_13"})
    assert result["error"] == "missing_argument"


def test_unknown_vehicle_is_reported(ctx):
    result = execute_tool(ctx, "get_vehicle_details", {"vehicle_id": "veh_999"})
    assert result["error"] == "vehicle_not_found"


def test_malformed_datetime_is_reported(ctx):
    result = execute_tool(
        ctx, "calculate_quote", {"vehicle_id": "veh_13", "pickup_at": "friday", "return_at": "monday"}
    )
    assert result["error"] == "invalid_request"
    assert "ISO 8601" in result["message"]


def test_unavailable_vehicle_pushes_the_agent_toward_alternatives(ctx):
    """The hint matters: without it the model tends to announce 'unavailable'
    and stop, which is the failure mode scenario 2 exists to prevent."""
    result = execute_tool(ctx, "calculate_quote", {**WINDOW, "vehicle_id": "veh_18"})
    assert result["error"] == "vehicle_unavailable"
    assert result["next_available_from"]
    assert "find_alternatives" in result["hint"]


def test_backwards_dates_are_rejected(ctx):
    result = execute_tool(
        ctx,
        "search_available_vehicles",
        {"pickup_at": dt(7, 19).isoformat(), "return_at": dt(4, 19).isoformat()},
    )
    assert result["error"] == "invalid_request"


def test_unknown_category_lists_the_allowed_values(ctx):
    result = execute_tool(
        ctx, "search_available_vehicles", {**WINDOW, "categories": ["spaceship"]}
    )
    assert result["error"] == "invalid_request"
    assert "supercar" in result["message"]


def test_discount_above_the_ceiling_is_refused_at_the_tool_boundary(ctx):
    """A model that decides to be generous cannot get past the engine."""
    result = execute_tool(
        ctx, "calculate_quote", {**WINDOW, "vehicle_id": "veh_13", "discount_percent": "30"}
    )
    assert result["error"] == "invalid_request"
    assert "exceeds the allowed maximum" in result["message"]


def test_naive_datetimes_are_interpreted_in_operator_time(ctx):
    """Entity extraction will not reliably produce an offset; rejecting naive
    datetimes would break more conversations than it protects."""
    result = execute_tool(
        ctx,
        "calculate_quote",
        {"vehicle_id": "veh_13", "pickup_at": "2026-09-04T19:00:00", "return_at": "2026-09-07T19:00:00"},
    )
    assert result["total_charge"] == "7560.00"


def test_shortlist_cap_cannot_be_overridden_by_the_model(ctx):
    result = execute_tool(ctx, "search_available_vehicles", {**WINDOW, "limit": 20})
    assert result["count"] <= 3
