"""A local chat harness for testing the agent.

This is development tooling, not a product surface — the same role the console
plays, with a nicer window. Customers only ever reach this agent through
WhatsApp; nothing here is meant to be deployed or shown to one.

What it adds over the console is the inspector: alongside each reply it shows
which tools ran and what the agent currently believes, which is the fastest way
to tell a good answer from a lucky one.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..context import ToolContext

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

#: Every browser session is this customer unless one is named, so a reload
#: continues the same conversation rather than starting a stranger.
DEFAULT_HANDLE = "+971500000000"


class Turn(BaseModel):
    message: str
    handle: str = DEFAULT_HANDLE


def create_app(
    *,
    session_factory: Callable[[], Any],
    agent_factory: Callable[[], Any],
    reference_date: date | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> FastAPI:
    app = FastAPI(title="Sandline Rentals — test chat (DEMO)")

    if (PROJECT_ROOT / "assets").exists():
        app.mount("/assets", StaticFiles(directory=PROJECT_ROOT / "assets"), name="assets")

    def context(session: Any, handle: str) -> ToolContext:
        ctx = ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)
        customer, _ = ctx.customers.get_or_create(handle, ctx.now())
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
        ctx.customer_id = customer.customer_id
        ctx.conversation_id = conversation.conversation_id
        session.flush()
        return ctx

    def snapshot(ctx: ToolContext) -> dict[str, Any]:
        """What the agent believes right now — the inspector panel."""
        state = ctx.load_state()
        prefs = state.vehicle_preferences
        reservations = ctx.reservations.for_customer(ctx.customer_id or "")
        return {
            "stage": state.stage.value,
            "pickup_at": state.pickup_at.strftime("%a %d %b, %-I:%M %p") if state.pickup_at else None,
            "return_at": state.return_at.strftime("%a %d %b, %-I:%M %p") if state.return_at else None,
            "location": state.delivery_location,
            "wants": prefs.models + prefs.makes + [c.value for c in prefs.categories],
            "color": prefs.color,
            "budget": str(prefs.budget_per_day) if prefs.budget_per_day else None,
            "still_missing": state.missing_requirements(),
            "escalated": state.escalated,
            "escalation_reason": state.escalation_reason,
            "bookings": [
                {
                    "reference": r.reservation_id,
                    "vehicle": ctx.engine.get_vehicle(r.vehicle_id).display_name,
                    "status": r.status,
                    "total": f"{r.currency} {r.total_charge}",
                    "pickup": r.pickup_at.strftime("%a %d %b, %-I:%M %p"),
                }
                for r in reservations
            ],
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(HERE / "index.html")

    @app.get("/api/state")
    def read_state(handle: str = DEFAULT_HANDLE) -> JSONResponse:
        with session_factory() as session:
            ctx = context(session, handle)
            history = [
                {"direction": m.direction, "text": m.content}
                for m in ctx.messages.for_conversation(ctx.conversation_id or "")
            ]
            body = {"history": history, "state": snapshot(ctx)}
            session.commit()
        return JSONResponse(body)

    @app.post("/api/message")
    def send(turn: Turn) -> JSONResponse:
        started = datetime.now()
        with session_factory() as session:
            ctx = context(session, turn.handle)
            try:
                result = agent_factory().respond(ctx, turn.message)
                session.commit()
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not a 500
                session.rollback()
                return JSONResponse(
                    {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}, status_code=200
                )

            body = {
                "reply": result.reply,
                "tools": result.tool_calls,
                "escalated": result.escalated,
                "duplicate": result.duplicate,
                "provider_error": result.provider_error,
                "extraction_error": result.extraction_error,
                "seconds": round((datetime.now() - started).total_seconds(), 1),
                "state": snapshot(ctx),
            }
            session.commit()
        return JSONResponse(body)

    @app.post("/api/reset")
    def reset(handle: str = DEFAULT_HANDLE) -> JSONResponse:
        """Close the conversation so the next message starts fresh.

        Deliberately does not delete anything: the transcript and its tool calls
        are what the evaluator reads, and a demo you cannot review afterwards is
        worth less than one you can.
        """
        with session_factory() as session:
            ctx = context(session, handle)
            conversation = ctx.conversations.get(ctx.conversation_id or "")
            if conversation is not None:
                conversation.outcome = "reset"
            session.commit()
        return JSONResponse({"ok": True})

    return app
