"""Idempotency for state-changing tools.

The acceptance criterion is blunt: duplicate messages must not create duplicate
reservations. Three things can cause a repeat, and this handles all of them:

  1. WhatsApp retries a webhook delivery.
  2. The model emits the same tool call twice in one turn.
  3. The customer impatiently sends "yes" again.

Every state-changing call is claimed in the `tool_calls` ledger under a key. If
the key has already been used, the stored result is returned verbatim and no
work happens. Errors are *not* stored under the key, so a genuine retry after a
transient failure still gets to run.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from ..context import ToolContext


def derive_key(
    ctx: ToolContext, tool_name: str, args: dict[str, Any], salt: str = ""
) -> str:
    """Fallback key when the caller supplies none.

    Same conversation + same tool + same arguments = same operation. `salt`
    carries a version for mutations, so applying a change, reverting it, and
    applying it again is three operations rather than one replayed twice.
    """
    payload = json.dumps(
        {"conversation": ctx.conversation_id, "tool": tool_name, "args": args, "salt": salt},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def run_idempotent(
    ctx: ToolContext,
    *,
    tool_name: str,
    args: dict[str, Any],
    operation: Callable[[], dict[str, Any]],
    idempotency_key: str | None = None,
    salt: str = "",
) -> dict[str, Any]:
    key = idempotency_key or derive_key(ctx, tool_name, args, salt)
    session = ctx._require_session()
    ledger = ctx.tool_calls

    existing = ledger.find_by_idempotency_key(tool_name, key)
    if existing is not None:
        # `idempotent_replay` is surfaced to the agent so it can say "that's
        # already booked" rather than announcing a second confirmation.
        return {**existing.result, "idempotent_replay": True}

    started = time.perf_counter()
    result = operation()
    duration_ms = int((time.perf_counter() - started) * 1000)
    failed = "error" in result

    try:
        ledger.record(
            tool_name=tool_name,
            arguments=args,
            result=result,
            now=ctx.now(),
            conversation_id=ctx.conversation_id,
            # A failed call keeps the key free so the agent can legitimately retry.
            idempotency_key=None if failed else key,
            status="error" if failed else "ok",
            error_code=result.get("error") if failed else None,
            duration_ms=duration_ms,
        )
        session.flush()
    except IntegrityError:
        # Another writer claimed the key between our lookup and our insert.
        # Rolling back discards whatever this call created — the reservation
        # included — so the winner's single result stands.
        session.rollback()
        winner = ledger.find_by_idempotency_key(tool_name, key)
        if winner is None:  # pragma: no cover - conflict on something else
            raise
        return {**winner.result, "idempotent_replay": True}

    return result


def audit_read(
    ctx: ToolContext,
    *,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    duration_ms: int | None = None,
) -> None:
    """Record a read-only call.

    Read tools are audited but never replayed: the evaluator needs to know the
    agent actually checked availability before promising a car, and a cached
    availability answer would be worse than useless.
    """
    if ctx.session is None:
        return
    ctx.tool_calls.record(
        tool_name=tool_name,
        arguments=args,
        result={} if "error" not in result else result,
        now=ctx.now(),
        conversation_id=ctx.conversation_id,
        status="error" if "error" in result else "ok",
        error_code=result.get("error"),
        duration_ms=duration_ms,
    )
