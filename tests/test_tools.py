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


# --------------------------------------------------------------------------
# A named model that is unavailable must not become a generic list
# --------------------------------------------------------------------------


def test_an_unavailable_named_model_returns_a_pointer_not_a_hatchback(ctx):
    """Ranking puts the cheapest unrelated car first, so a plain search for a
    booked Lamborghini returns a Kia. That is the failure the tiered
    alternatives exist to prevent, so this path refuses to answer at all."""
    result = execute_tool(
        ctx, "search_available_vehicles", {**WINDOW, "models": ["Huracan"]}
    )
    assert result["count"] == 0
    assert result["vehicles"] == []
    assert result["requested_model_unavailable"][0]["vehicle_id"] == "veh_18"
    assert result["requested_model_unavailable"][0]["next_available_from"]
    assert "find_alternatives" in result["hint"]


def test_an_available_named_model_is_returned_normally(ctx):
    result = execute_tool(ctx, "search_available_vehicles", {**WINDOW, "models": ["G63"]})
    assert result["count"] >= 1
    assert "requested_model_unavailable" not in result


def test_a_model_not_in_the_fleet_still_gets_a_general_search(ctx):
    """Asking for a Bugatti is different from asking for a car we own but have
    booked out — there is nothing to substitute for, so show what is free."""
    result = execute_tool(ctx, "search_available_vehicles", {**WINDOW, "models": ["Chiron"]})
    assert "requested_model_unavailable" not in result
    assert result["count"] >= 1


# --------------------------------------------------------------------------
# Showing photos
#
# The split of responsibility is the same one that governs prices: the model
# decides *when* a photo helps the sale, the engine decides *which file that
# is*. There is no argument on this tool that lets a caption be attached to the
# wrong car, and these tests are what keep it that way.
# --------------------------------------------------------------------------


def test_photos_are_queued_for_the_transport_not_sent_by_the_tool(ctx):
    """The tool runs inside the agent loop, which has no transport and no idea
    whether the customer is on WhatsApp or in the local test window."""
    result = execute_tool(ctx, "show_vehicle_photos", {"vehicle_id": "veh_13"})

    assert result["sent"] == 3
    assert result["vehicle_id"] == "veh_13"

    queued = ctx.take_media()
    assert len(queued) == 1
    assert queued[0]["vehicle_id"] == "veh_13"
    assert all(path.startswith("assets/vehicles/veh_13") for path in queued[0]["images"])


def test_the_model_never_supplies_an_image_path(ctx):
    """A tool that accepted a URL would let the agent caption a Kia with a photo
    of a G63. The only argument is the vehicle id."""
    from rental_agent.agent.schemas import TOOLS

    schema = next(t for t in TOOLS if t["name"] == "show_vehicle_photos")
    assert set(schema["input_schema"]["properties"]) == {"vehicle_id", "caption"}
    assert schema["input_schema"]["required"] == ["vehicle_id"]


def test_draining_the_queue_means_a_retried_turn_cannot_send_twice(ctx):
    execute_tool(ctx, "show_vehicle_photos", {"vehicle_id": "veh_13"})
    assert len(ctx.take_media()) == 1
    assert ctx.take_media() == []


def test_asking_for_photos_of_an_unknown_car_is_reported_not_raised(ctx):
    result = execute_tool(ctx, "show_vehicle_photos", {"vehicle_id": "veh_999"})
    assert result["error"] == "vehicle_not_found"
    assert ctx.take_media() == []


def test_the_photo_count_is_capped_by_config(ctx):
    """WhatsApp has no album; each photo is a separate message and a separate
    notification. The ceiling belongs in rules.json, not in the prompt."""
    from rental_agent.config import load_rules

    cap = load_rules().messaging["photos"]["max_per_message"]
    execute_tool(ctx, "show_vehicle_photos", {"vehicle_id": "veh_13"})
    assert len(ctx.take_media()[0]["images"]) <= cap


# --------------------------------------------------------------------------
# What the company owns
#
# Found in the preview: asked "what kinds of Tesla do you have", the agent
# answered "the Model 3 and Model Y" — neither of which is in the fleet. Asked
# "do you have a Cybertruck", it said no. The fleet has one.
#
# It was not lying so much as guessing. Availability needs dates, so the search
# tool refuses without them; details need an id the model cannot know. With no
# tool between the two, the only thing left to answer from was its own
# impression of what a rental company owns.
# --------------------------------------------------------------------------


def test_a_car_in_the_fleet_is_found_by_name(ctx):
    result = execute_tool(ctx, "look_up_vehicles", {"query": "g63"})
    assert result["matched"] >= 1
    assert any("G63" in v["name"] for v in result["vehicles"])


def test_a_car_that_is_not_there_says_so_plainly(ctx):
    result = execute_tool(ctx, "look_up_vehicles", {"query": "delorean"})
    assert result["matched"] == 0
    assert result["vehicles"] == []
    assert "not one we have" in result["note"]


def test_looking_up_a_make_returns_every_model_of_it(ctx):
    result = execute_tool(ctx, "look_up_vehicles", {"query": "mercedes"})
    assert result["matched"] >= 2


def test_the_lookup_does_not_claim_anything_is_free(ctx):
    """Owning a car and it being free are different questions. Answering the
    first as though it were the second is how a customer is promised a car that
    is already out."""
    result = execute_tool(ctx, "look_up_vehicles", {"query": "g63"})
    assert "not cars confirmed free" in result["note"]
    assert "search_available_vehicles" in result["note"]
    assert not any("available" in str(v).lower() for v in result["vehicles"])


def test_the_whole_fleet_is_not_dumped_into_one_answer(ctx):
    """A hundred cars in one message is a catalogue, not a reply."""
    from rental_agent.tools.rental_tools import MAX_LOOKUP_RESULTS

    result = execute_tool(ctx, "look_up_vehicles", {})
    assert result["showing"] <= MAX_LOOKUP_RESULTS
    assert result["showing"] <= result["matched"]


def test_the_cheapest_come_first_when_nothing_is_named(ctx):
    """"What's your cheapest car?" is a real question and should not require a
    second tool call to answer."""
    from decimal import Decimal

    result = execute_tool(ctx, "look_up_vehicles", {})
    prices = [Decimal(v["daily_price"]) for v in result["vehicles"]]
    assert prices == sorted(prices)


def test_the_tool_forbids_answering_from_impression():
    from rental_agent.agent.schemas import TOOLS

    [schema] = [t for t in TOOLS if t["name"] == "look_up_vehicles"]
    assert "NEVER answer a question about what the fleet contains" in schema["description"]
