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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ..context import ToolContext
from ..store.models import MessagePresentation, Reservation
from ..payments.webhook import build_router, reconcile_checkout
from ..payments.base import PaymentError
from ..formatting import photo_caption
from ..whatsapp import reactions as reactions_mod
from ..whatsapp.client import split_message, to_whatsapp_markup
from ..whatsapp.pacing import Pacing
from ..services import documents as document_service

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
    document_store=None,
    document_checker=None,
) -> FastAPI:
    app = FastAPI(title="Sandline Rentals — test chat (DEMO)")

    if (PROJECT_ROOT / "assets").exists():
        app.mount("/assets", StaticFiles(directory=PROJECT_ROOT / "assets"), name="assets")

    def context(session: Any, handle: str, *, read_only=False) -> ToolContext:
        ctx = ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)
        # Polling a populated chat must stay read-only. Updating last_seen_at
        # here contended with the chat turn's SQLite write transaction.
        customer = ctx.customers.by_whatsapp_id(handle) if read_only else None
        if customer is None:
            customer, _ = ctx.customers.get_or_create(handle, ctx.now())
        conversation = ctx.conversations.open_for_customer(customer.customer_id)
        if conversation is None:
            conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
        ctx.customer_id = customer.customer_id
        ctx.conversation_id = conversation.conversation_id
        if document_store is not None and hasattr(document_store,'cipher'):
            session.info['document_cipher'] = document_store.cipher
        else:
            import os
            if not os.getenv('DOCUMENT_ENCRYPTION_KEY'):
                from ..whatsapp.storage import existing_browser_cipher
                restored = existing_browser_cipher()
                if restored is not None:
                    session.info['document_cipher'] = restored
        session.flush()
        return ctx

    def snapshot(ctx: ToolContext) -> dict[str, Any]:
        """What the agent believes right now — the inspector panel."""
        state = ctx.load_state()
        prefs = state.vehicle_preferences
        reservations = ctx.reservations.for_customer(ctx.customer_id or "")
        return {
            'documents': document_service.summary(ctx),
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
                    "payment_status": r.payment_status,
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
            ctx = context(session, handle, read_only=True)
            history = [
                {"id": m.id, "direction": m.direction, "text": m.content,
                 "created_at": m.created_at.isoformat(),
                 "presentation": stored.payload if (stored := session.get(MessagePresentation, m.id)) else None}
                for m in ctx.messages.for_conversation(ctx.conversation_id or "")
            ]
            latest = next((m["presentation"] for m in reversed(history) if m["presentation"]), {})
            body = {"history": history, "state": snapshot(ctx),
                    "tools": latest.get("tools", []), "seconds": latest.get("seconds"),
                    "provider_error": latest.get("provider_error")}
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
            messages = ctx.messages.for_conversation(ctx.conversation_id)
            outbound = next((m for m in reversed(messages) if m.direction == "outbound" and m.content == result.reply), None)
            if outbound is not None and not result.duplicate:
                body["created_at"] = outbound.created_at.isoformat()
                body["message_id"] = outbound.id
                presentation = {k: body[k] for k in ("reply", "parts", "cards", "media", "reaction", "created_at", "tools", "seconds", "provider_error")}
                session.merge(MessagePresentation(message_id=outbound.id, payload=presentation))
            session.commit()
        return JSONResponse(body)

    @app.post('/api/documents')
    async def upload_document(request: Request, handle: str = DEFAULT_HANDLE):
        # Custom header requires browser preflight for cross-origin uploads.
        # The local harness does not grant CORS access to other websites.
        import hashlib
        import re
        from ..services import checkout, documents
        from ..services.document_checks import DocumentChecker, notice
        from ..whatsapp.media import MAX_BYTES, MediaError, validate_file
        from ..whatsapp.storage import build_browser_store
        if request.headers.get('x-sample-document') != '1':
            return JSONResponse({'error':'Use the sample document attachment button in the chat.'},status_code=403)
        upload_id = request.headers.get('x-upload-id','')
        if not re.fullmatch(r'[a-zA-Z0-9-]{10,64}',upload_id):
            return JSONResponse({'error':'Missing upload reference.'},status_code=400)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>MAX_BYTES:
                return JSONResponse({'error':'Please send a JPG, PNG or PDF no larger than 10 MB.'},status_code=413)
        mime = request.headers.get('content-type','').split(';')[0]
        try: validate_file(bytes(data),mime)
        except MediaError as exc: return JSONResponse({'error':str(exc)},status_code=400)
        def process():
            nonlocal document_store, document_checker
            from ..store.models import CustomerDocument
            if document_store is None: document_store = build_browser_store()
            if document_checker is None: document_checker = DocumentChecker()
            with session_factory() as session:
                ctx = context(session,handle)
                provider_id = 'browser-upload:'+upload_id
                existing = session.scalar(select(CustomerDocument).where(CustomerDocument.provider_message_id==provider_id))
                if existing:
                    if existing.customer_id != ctx.customer_id or existing.sha256 != hashlib.sha256(data).hexdigest():
                        return JSONResponse({'error':'This upload reference belongs to a different file.'},status_code=409)
                    return JSONResponse({'ok':True,'document':existing.document_id,'state':snapshot(ctx)})
                from types import SimpleNamespace
                message = SimpleNamespace(message_id=provider_id,media_kind='document',raw={'document':{'id':upload_id}})
                ctx.messages.record(conversation_id=ctx.conversation_id,direction='inbound',content='Attached a sample document',
                    now=ctx.now(),provider_message_id=provider_id)
                row = documents.receive(ctx,message,document_store,lambda _:(bytes(data),mime))
                if row.status != 'pending_review': raise MediaError('The attachment could not be stored.')
                document_checker.check(ctx,row,document_store)
                session.flush()
                reply = notice(row)
                if checkout.enabled(ctx): reply += '\n\n'+checkout.document_checklist(ctx)
                ctx.messages.record(conversation_id=ctx.conversation_id,direction='outbound',content=reply,now=ctx.now())
                row.notified_at=ctx.now()
                session.commit()
                return JSONResponse({'ok':True,'document':row.document_id,'reply':reply,'state':snapshot(ctx)})
        try: return await run_in_threadpool(process)
        except Exception:
            return JSONResponse({'error':'The document could not be checked. Please try uploading it again. Booking and payment remain blocked.'},status_code=503)

    # Signed payment callbacks work in the browser harness as well as WhatsApp.
    app.include_router(build_router(session_factory=session_factory,
        context_factory=lambda session: ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)))

    @app.get("/payments/return")
    def checkout_return(session_id: str = "", handle: str = DEFAULT_HANDLE):
        from html import escape
        from ..payments.stripe import StripeProvider
        message = "No payment has been verified. Return to the chat to check your booking."
        with session_factory() as session:
            ctx = context(session, handle)
            # Only verify sessions previously issued to this chat's customer.
            booking = next((r for r in ctx.reservations.for_customer(ctx.customer_id)
                if any(h.get("event") == "payment_link_created" and h.get("reference") == session_id
                       for h in r.history or [])), None)
            if booking:
                try:
                    checkout = StripeProvider().retrieve_checkout(session_id)
                    if checkout.get("id") != session_id or (checkout.get("metadata") or {}).get("reservation_id") != booking.reservation_id:
                        message = "The checkout does not match this booking. A colleague must check it."
                    elif checkout.get("payment_status") == "paid":
                        result = reconcile_checkout(ctx, checkout)
                        message = result.get("message") or ("Your payment is already recorded." if result.get("reason") == "already_recorded" else "Payment needs review. Please do not pay again.")
                    else:
                        message = "Payment has not completed. You can return to the chat."
                except PaymentError:
                    message = "Stripe could not be reached to verify payment. Please do not pay again; check back shortly."
            session.commit()
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Payment status</title>'
            '<main style="font:18px system-ui;max-width:600px;margin:80px auto;padding:24px">'
            '<h1>Payment status</h1><p>' + escape(message) + '</p><a href="/">Return to chat</a></main>')

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
            ctx = context(session, handle, read_only=True)
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
