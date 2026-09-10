"""One dispatch path for every tool the agent can call.

Responsibilities, in order:
  1. Resolve the handler, refusing unknown names.
  2. Route state-changing calls through the idempotency ledger.
  3. Convert any exception into an error envelope — a malformed tool call must
     never kill a customer conversation.
  4. Audit the call.
"""

from __future__ import annotations

import time
from typing import Any

from ..context import ToolContext
from ..engine.engine import InvalidWindow, VehicleNotFound, VehicleUnavailable
from ..services.idempotency import audit_read, run_idempotent
from .rental_tools import READ_HANDLERS, ToolError
from .state_tools import PERSISTED_READ_HANDLERS, STATE_HANDLERS

ALL_HANDLERS = {**READ_HANDLERS, **PERSISTED_READ_HANDLERS, **STATE_HANDLERS}

#: Tools that require a session and are protected against replay.
STATE_CHANGING = frozenset(STATE_HANDLERS)
#: Tools that need persistence at all.
NEEDS_SESSION = frozenset(STATE_HANDLERS) | frozenset(PERSISTED_READ_HANDLERS)


def _envelope(exc: Exception) -> dict[str, Any] | None:
    """Map a known exception onto an envelope the agent can act on."""
    if isinstance(exc, InvalidWindow):
        return {
            "error": exc.reason,
            "message": str(exc),
            "hint": exc.hint,
        }
    if isinstance(exc, VehicleNotFound):
        return {"error": "vehicle_not_found", "message": str(exc), "vehicle_id": exc.vehicle_id}
    if isinstance(exc, VehicleUnavailable):
        return {
            "error": "vehicle_unavailable",
            "message": str(exc),
            "vehicle_id": exc.vehicle_id,
            "reason": exc.result.reason.value if exc.result.reason else None,
            "next_available_from": (
                exc.result.next_available_from.isoformat()
                if exc.result.next_available_from
                else None
            ),
            "hint": "Call find_alternatives before telling the customer it is unavailable.",
        }
    if isinstance(exc, KeyError):
        return {"error": "missing_argument", "message": f"Required argument {exc} is missing"}
    if isinstance(exc, (ToolError, ValueError)):
        return {"error": "invalid_request", "message": str(exc)}
    return None


def _mutation_salt(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Version the auto-derived idempotency key for reservation mutations.

    Without this, "move it to 8pm", "actually 7pm", "no, 8pm" would treat the
    third request as a replay of the first and silently do nothing.
    """
    reservation_id = args.get("reservation_id")
    if not reservation_id or ctx.session is None:
        return ""
    reservation = ctx.reservations.get(reservation_id)
    return f"v{reservation.version}" if reservation is not None else ""


def execute_tool(
    ctx: ToolContext,
    name: str,
    args: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    handler = ALL_HANDLERS.get(name)
    if handler is None:
        return {"error": "unknown_tool", "message": f"No tool named '{name}'"}

    if name in NEEDS_SESSION and ctx.session is None:
        return {
            "error": "no_persistence",
            "message": f"'{name}' needs a database session and this context has none",
        }

    # Recheck prerequisites even when an earlier idempotent result exists.
    # A cached success cannot override expired documents or changed terms.
    from ..services.checkout import tool_gate
    try:
        blocked = tool_gate(ctx, name, args)
    except Exception as exc:
        envelope = _envelope(exc)
        if envelope is None:
            raise
        return envelope
    if blocked:
        return blocked

    def run() -> dict[str, Any]:
        try:
            return handler(ctx, args)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all at the boundary
            envelope = _envelope(exc)
            if envelope is None:
                raise
            return envelope

    if name in STATE_CHANGING:
        return run_idempotent(
            ctx,
            tool_name=name,
            args=args,
            operation=run,
            idempotency_key=idempotency_key,
            salt=_mutation_salt(ctx, args),
        )

    started = time.perf_counter()
    result = run()
    audit_read(
        ctx,
        tool_name=name,
        args=args,
        result=result,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def tool_names() -> list[str]:
    return sorted(ALL_HANDLERS)
