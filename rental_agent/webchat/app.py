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
from sqlalchemy import select

from ..context import ToolContext
from ..formatting import photo_caption
from ..whatsapp import reactions as reactions_mod
from ..whatsapp.client import split_message, to_whatsapp_markup
from ..whatsapp.pacing import Pacing

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

#: Every browser session is this customer unless one is named, so a reload
#: continues the same conversation rather than starting a stranger.
DEFAULT_HANDLE = "+971500000000"


class Turn(BaseModel):
    message: str
    handle: str = DEFAULT_HANDLE


class Outcome(BaseModel):
    """How a conversation ended, for the demo's finish control."""

    outcome: str = "dropped"
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

    @app.get("/api/operator")
    def operator() -> JSONResponse:
        """Who the customer thinks they are messaging."""
        from ..config import load_fleet

        who, _ = load_fleet()
        return JSONResponse({
            "name": who.demo_company_name,
            "is_demonstration": who.is_demonstration,
        })

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

            # Replay what WhatsApp would actually show, not just the text. The
            # splitting, the markup conversion, the pause before each following
            # message and the reaction are all computed by the same code the
            # transport uses — so what this window shows is what a phone gets.
            pacing = Pacing.from_rules(ctx.engine.rules)
            parts = split_message(to_whatsapp_markup(result.reply))
            body = {
                "reply": result.reply,
                "parts": [
                    {"text": part, "pause": pacing.compose_seconds(part) if i else 0.0}
                    for i, part in enumerate(parts)
                ],
                "reaction": reactions_mod.for_turn(result, ctx.engine.rules),
                # The engine's own figures, sent after the sentence. On WhatsApp
                # these go as their own message; a window that dropped them was
                # showing less than a customer gets, which is the whole reason
                # the rendered breakdown exists.
                "cards": [to_whatsapp_markup(card) for card in result.cards],
                "tools": result.tool_calls,
                # Served off the mounted /assets directory rather than a public
                # host: on WhatsApp Meta fetches these itself, here the browser
                # does, and the agent's choice of which car to show is the same
                # either way.
                "media": [
                    {
                        "vehicle": item.get("display_name"),
                        "caption": photo_caption(
                            item.get("caption"), item.get("display_name", ""), result.reply
                        ),
                        # An operator's own photographs are absolute and fetched
                        # from their server; generated cards are repo-relative
                        # and served off the mounted /assets route.
                        "images": [
                            path if path.startswith(("http://", "https://"))
                            else f"/{path.lstrip('/')}"
                            for path in item.get("images", [])
                        ],
                    }
                    for item in result.media
                ],
                "escalated": result.escalated,
                "duplicate": result.duplicate,
                "provider_error": result.provider_error,
                "extraction_error": result.extraction_error,
                "seconds": round((datetime.now() - started).total_seconds(), 1),
                "state": snapshot(ctx),
            }
            session.commit()
        return JSONResponse(body)

    def learning_snapshot(ctx: ToolContext) -> dict[str, Any]:
        """What the loop currently knows, believes and is proposing."""
        from ..evaluation import replay as replay_mod
        from ..evaluation import strategies
        from ..evaluation.evaluator import open_mistakes
        from ..store.models import Evaluation

        strategies.ensure_baseline(ctx)
        versions = strategies.history(ctx)
        active = strategies.active_strategy(ctx)
        candidate = next((s for s in versions if s.status == "candidate"), None)

        return {
            "active": {
                "version": active.version if active else None,
                "lessons": list(active.lessons) if active else [],
            },
            "candidate": {
                "version": candidate.version if candidate else None,
                "lessons": list(candidate.lessons) if candidate else [],
                "replayed": bool(candidate and (candidate.replay_passed or candidate.replay_failed)),
                "replay_passed": candidate.replay_passed if candidate else 0,
                "replay_failed": candidate.replay_failed if candidate else 0,
            },
            "mistakes": [
                {
                    "type": m.type,
                    "severity": m.severity,
                    "occurrences": m.occurrences,
                    "was": m.bad_behavior,
                    "should": m.correct_behavior,
                }
                for m in open_mistakes(ctx)
            ],
            "cases": len(replay_mod.cases(ctx)),
            "evaluations": len(list(ctx.session.scalars(select(Evaluation.id)))),
        }

    @app.get("/api/learning")
    def learning(handle: str = DEFAULT_HANDLE) -> JSONResponse:
        with session_factory() as session:
            ctx = context(session, handle)
            body = learning_snapshot(ctx)
            session.commit()
        return JSONResponse(body)

    @app.post("/api/learning/finish")
    def finish(turn: Outcome) -> JSONResponse:
        """End this conversation and judge it now.

        In production a conversation is judged when it has been tagged *and* has
        gone quiet for a day — you cannot know it ended until the customer stops
        replying. Nobody is waiting a day to watch a demo, so this does by hand
        what the webhook sweep does on its own: tags how it ended, and evaluates
        it once.
        """
        from ..evaluation.evaluator import evaluate_conversation, record
        from ..store.models import Evaluation

        with session_factory() as session:
            ctx = context(session, turn.handle)
            conversation = ctx.conversations.get(ctx.conversation_id or "")
            if conversation is None:
                return JSONResponse({"error": "no conversation"}, status_code=200)

            already = ctx.session.scalar(
                select(Evaluation).where(Evaluation.conversation_id == conversation.conversation_id)
            )
            conversation.sales_outcome = turn.outcome
            result = evaluate_conversation(ctx, conversation.conversation_id)
            if already is None:
                record(ctx, result)

            body = {
                "outcome": turn.outcome,
                "already_judged": already is not None,
                "findings": [
                    {
                        "type": f.type,
                        "severity": f.severity,
                        "situation": f.situation,
                        "was": f.bad_behavior,
                        "should": f.correct_behavior,
                    }
                    for f in result.findings
                ],
                "learning": learning_snapshot(ctx),
            }
            session.commit()
        return JSONResponse(body)

    @app.post("/api/learning/propose")
    def propose(handle: str = DEFAULT_HANDLE) -> JSONResponse:
        """Turn what has been found into a candidate set of lessons.

        Free: it calls no model. Proving the candidate does — that is `replay`,
        and it is not driven from here because it takes minutes, not seconds.
        """
        from ..evaluation import cycle

        with session_factory() as session:
            ctx = context(session, handle)
            report = cycle.run(ctx, agent=None, activate=False)
            body = {
                "evaluated": report.evaluated,
                "clean": report.clean,
                "findings": report.findings,
                "new_cases": report.new_cases,
                "total_cases": report.total_cases,
                "candidate": report.candidate_version,
                "lessons": report.lessons,
                "reason": report.reason,
                "learning": learning_snapshot(ctx),
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
