"""Idempotency and the audit log.

"Duplicate messages cannot create duplicate reservations" is a prototype
acceptance criterion. These tests are that criterion.
"""

from __future__ import annotations

from rental_agent.store.models import Reservation
from rental_agent.tools.registry import execute_tool
from tests.conftest import dt
from tests.test_booking import book, quote_for


def reservation_count(ctx) -> int:
    return ctx.session.query(Reservation).count()


# --------------------------------------------------------------------------
# Replay protection
# --------------------------------------------------------------------------


def test_the_same_booking_call_twice_creates_one_reservation(booking_ctx):
    """The model emitting the same tool call twice, or a webhook retry
    re-running the turn."""
    quote = quote_for(booking_ctx)
    args = {"quote_id": quote["quote_id"]}

    first = execute_tool(booking_ctx, "create_demo_reservation", args)
    second = execute_tool(booking_ctx, "create_demo_reservation", args)

    assert first["reservation_id"] == second["reservation_id"] == "DEMO-1042"
    assert second["idempotent_replay"] is True
    assert "idempotent_replay" not in first
    assert reservation_count(booking_ctx) == 1


def test_an_explicit_idempotency_key_is_honoured(booking_ctx):
    quote = quote_for(booking_ctx)
    args = {"quote_id": quote["quote_id"]}

    first = execute_tool(booking_ctx, "create_demo_reservation", args, idempotency_key="turn-7")
    second = execute_tool(booking_ctx, "create_demo_reservation", args, idempotency_key="turn-7")

    assert second["idempotent_replay"] is True
    assert first["reservation_id"] == second["reservation_id"]
    assert reservation_count(booking_ctx) == 1


def test_a_genuinely_different_booking_is_not_a_replay(booking_ctx):
    book(booking_ctx)
    second = book(booking_ctx, vehicle_id="veh_14")
    assert second["reservation_id"] == "DEMO-1043"
    assert reservation_count(booking_ctx) == 2


def test_a_failed_call_does_not_poison_the_key(booking_ctx):
    """A transient failure must not permanently block the operation. The failing
    call is audited but its key stays free."""
    failed = execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": "DQ-NOPE"})
    assert failed["error"] == "quote_not_found"

    retried = execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": "DQ-NOPE"})
    assert retried["error"] == "quote_not_found"
    assert "idempotent_replay" not in retried


def test_repeating_a_modification_does_not_apply_it_twice(booking_ctx):
    """Two defences in layers: the ledger catches a same-turn retry, and no-op
    detection catches a repeat after the version has already moved on."""
    reservation = book(booking_ctx)
    args = {"reservation_id": reservation["reservation_id"], "return_at": dt(9, 19).isoformat()}

    first = execute_tool(booking_ctx, "modify_demo_reservation", args)
    second = execute_tool(booking_ctx, "modify_demo_reservation", args)

    assert second["no_change"] is True
    assert first["total_charge"] == second["total_charge"]
    stored = booking_ctx.reservations.get(reservation["reservation_id"])
    assert [entry["event"] for entry in stored.history] == ["created", "modified"]
    assert stored.version == 2


def test_repeating_an_extension_does_not_extend_again(booking_ctx):
    reservation = book(booking_ctx)
    args = {"reservation_id": reservation["reservation_id"], "new_return_at": dt(9, 19).isoformat()}

    first = execute_tool(booking_ctx, "extend_demo_rental", args)
    second = execute_tool(booking_ctx, "extend_demo_rental", args)

    assert second["no_change"] is True
    assert second["additional_charge"] == "0.00"
    assert first["total_charge"] == second["total_charge"]


def test_applying_reverting_and_reapplying_is_three_operations(booking_ctx):
    """The version salt. Without it the third request would be dismissed as a
    replay of the first and the customer's change would silently not happen."""
    reservation = book(booking_ctx)
    rid = reservation["reservation_id"]

    to_eight = {"reservation_id": rid, "pickup_at": dt(4, 20).isoformat()}
    to_seven = {"reservation_id": rid, "pickup_at": dt(4, 19).isoformat()}

    assert execute_tool(booking_ctx, "modify_demo_reservation", to_eight)["pickup_at"].endswith(
        "20:00:00+04:00"
    )
    assert execute_tool(booking_ctx, "modify_demo_reservation", to_seven)["pickup_at"].endswith(
        "19:00:00+04:00"
    )
    final = execute_tool(booking_ctx, "modify_demo_reservation", to_eight)

    assert "idempotent_replay" not in final
    assert final["pickup_at"].endswith("20:00:00+04:00")
    stored = booking_ctx.reservations.get(rid)
    assert stored.version == 4  # created + three modifications


def test_cancelling_twice_is_reported_once(booking_ctx):
    reservation = book(booking_ctx)
    args = {"reservation_id": reservation["reservation_id"]}

    first = execute_tool(booking_ctx, "cancel_demo_reservation", args)
    second = execute_tool(booking_ctx, "cancel_demo_reservation", args)

    assert first["cancellation_fee"] == second["cancellation_fee"]
    assert second["already_cancelled"] is True
    assert second["status"] == "cancelled"


def test_reads_are_never_served_from_the_ledger(booking_ctx):
    """Availability must always be fresh: the same search before and after a
    booking has to give different answers."""
    args = {
        "pickup_at": dt(4, 19).isoformat(),
        "return_at": dt(7, 19).isoformat(),
        "models": ["G63"],
    }
    before = execute_tool(booking_ctx, "search_available_vehicles", args)
    book(booking_ctx)
    after = execute_tool(booking_ctx, "search_available_vehicles", args)

    assert "veh_13" in [v["vehicle_id"] for v in before["vehicles"]]
    assert "veh_13" not in [v["vehicle_id"] for v in after["vehicles"]]


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


def test_every_call_is_audited(booking_ctx):
    execute_tool(
        booking_ctx,
        "search_available_vehicles",
        {"pickup_at": dt(4, 19).isoformat(), "return_at": dt(7, 19).isoformat()},
    )
    book(booking_ctx)

    calls = booking_ctx.tool_calls.for_conversation(booking_ctx.conversation_id)
    names = [call.tool_name for call in calls]
    assert names == [
        "search_available_vehicles",
        "create_demo_quote",
        "create_demo_reservation",
    ]


def test_state_changing_calls_carry_their_key_and_reads_do_not(booking_ctx):
    execute_tool(
        booking_ctx,
        "search_available_vehicles",
        {"pickup_at": dt(4, 19).isoformat(), "return_at": dt(7, 19).isoformat()},
    )
    book(booking_ctx)

    by_name = {c.tool_name: c for c in booking_ctx.tool_calls.for_conversation(booking_ctx.conversation_id)}
    assert by_name["search_available_vehicles"].idempotency_key is None
    assert by_name["create_demo_reservation"].idempotency_key is not None


def test_failures_are_audited_with_their_error_code(booking_ctx):
    execute_tool(booking_ctx, "create_demo_reservation", {"quote_id": "DQ-NOPE"})
    call = booking_ctx.tool_calls.for_conversation(booking_ctx.conversation_id)[-1]
    assert call.status == "error"
    assert call.error_code == "quote_not_found"


def test_the_audit_log_keeps_the_arguments_the_model_supplied(booking_ctx):
    """The evaluator reads these to tell "asked the engine" from "made it up"."""
    book(booking_ctx)
    calls = booking_ctx.tool_calls.for_conversation(booking_ctx.conversation_id)
    quote_call = next(c for c in calls if c.tool_name == "create_demo_quote")
    assert quote_call.arguments["vehicle_id"] == "veh_13"
    assert quote_call.result["total_charge"] == "7560.00"


def test_read_results_are_not_duplicated_into_the_audit_log(booking_ctx):
    """Successful read payloads are left out; the log records that the check
    happened, not a second copy of the fleet."""
    execute_tool(
        booking_ctx,
        "search_available_vehicles",
        {"pickup_at": dt(4, 19).isoformat(), "return_at": dt(7, 19).isoformat()},
    )
    call = booking_ctx.tool_calls.for_conversation(booking_ctx.conversation_id)[0]
    assert call.result == {}
    assert call.status == "ok"


# --------------------------------------------------------------------------
# Session guard
# --------------------------------------------------------------------------


def test_state_changing_tools_refuse_to_run_without_persistence(ctx):
    result = execute_tool(ctx, "create_demo_reservation", {"quote_id": "DQ-1"})
    assert result["error"] == "no_persistence"
