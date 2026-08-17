"""The WhatsApp Cloud API webhook.

Two endpoints:

* ``GET  /webhook`` — Meta's one-off verification handshake.
* ``POST /webhook`` — every inbound event.

The shape of this file is dictated by one constraint: **Meta retries a webhook
it does not get a 200 from within seconds**, and an agent turn takes far longer
than that. So the request is verified, acknowledged immediately, and the
conversation happens afterwards. Answering inline would guarantee duplicate
deliveries and duplicate replies.

Signature verification runs on the raw request body before any parsing, because
Meta signs the exact bytes it sent.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, Request, Response

from ..context import ToolContext
from .client import WhatsAppClient
from .payloads import InboundMessage, parse_messages, parse_statuses, verify_signature
from .settings import WhatsAppSettings

log = logging.getLogger("rental_agent.whatsapp")

#: Sent when a customer shares something we cannot read — a photo, a voice note.
UNSUPPORTED_REPLY = (
    "Sorry, I can only read text messages at the moment. Could you type that for me?"
)


def create_app(
    *,
    session_factory: Callable[[], Any],
    agent_factory: Callable[[], Any],
    settings: WhatsAppSettings | None = None,
    client: WhatsAppClient | None = None,
    reference_date: date | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the webhook app.

    Everything it depends on is injected, so the whole request path can be
    exercised against simulated Meta payloads with no credentials and no network.
    """
    settings = settings or WhatsAppSettings()
    client = client or WhatsAppClient(settings)
    app = FastAPI(title="Sandline Rentals — WhatsApp webhook (DEMO)")

    # -- verification handshake ------------------------------------------

    @app.get("/webhook")
    def verify(request: Request) -> Response:
        params = request.query_params
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == settings.verify_token
            and settings.verify_token
        ):
            # Meta expects the challenge echoed back as bare text.
            return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
        return Response(content="verification failed", status_code=403)

    # -- inbound events ---------------------------------------------------

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks) -> Response:
        raw = await request.body()

        if settings.can_verify_signatures:
            signature = request.headers.get("X-Hub-Signature-256")
            if not verify_signature(raw, signature, settings.app_secret):
                # Anyone can find the URL; only Meta can sign for it.
                log.warning("rejected a webhook with a bad signature")
                return Response(content="invalid signature", status_code=403)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed body, nothing to retry for
            return Response(content="ok", status_code=200)

        for status in parse_statuses(payload):
            log.debug("delivery status %s for %s", status.get("status"), status.get("id"))

        messages = parse_messages(payload)
        for message in messages:
            background.add_task(_handle, message)

        # Acknowledge before doing the work. A turn takes far longer than Meta
        # waits, and a late 200 means a redelivery and a second reply.
        return Response(content="ok", status_code=200)

    # -- the turn ---------------------------------------------------------

    def _handle(message: InboundMessage) -> None:
        """Answer one customer message. Runs after the 200 has gone back."""
        session = session_factory()
        try:
            ctx = _context_for(session, message)

            if message.unsupported:
                client.send_text(message.from_number, UNSUPPORTED_REPLY)
                session.commit()
                return

            client.mark_read(message.message_id)

            turn = agent_factory().respond(
                ctx, message.text, provider_message_id=message.message_id
            )
            session.commit()

            if turn.duplicate:
                log.info("ignored a redelivery of %s", message.message_id)
                return

            client.send_text(message.from_number, turn.reply)

            if turn.escalated:
                _notify_staff(ctx, message, turn)

            session.commit()
        except Exception:  # noqa: BLE001 - one bad turn must not kill the worker
            session.rollback()
            log.exception("failed to handle %s", message.message_id)
        finally:
            session.close()

    def _context_for(session: Any, message: InboundMessage) -> ToolContext:
        ctx = ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)
        customer, created = ctx.customers.get_or_create(message.from_number, ctx.now())
        if message.contact_name and not customer.name:
            customer.name = message.contact_name
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
        ctx.customer_id = customer.customer_id
        ctx.conversation_id = conversation.conversation_id
        session.flush()
        return ctx

    def _notify_staff(ctx: ToolContext, message: InboundMessage, turn: Any) -> None:
        """Hand a live incident to a human on WhatsApp.

        Best effort by design: a failure here is logged but never raised, since
        the customer has already been told a colleague is taking over and
        breaking the turn now would make things worse, not better.
        """
        if not settings.staff_number:
            log.warning("escalation with no WHATSAPP_STAFF_NUMBER configured")
            return

        state = ctx.load_state()
        note = (
            f"⚠️ Escalation — {state.escalation_reason or 'unspecified'}\n\n"
            f"Customer: {message.from_number}\n"
            f'They said: "{message.text[:300]}"\n\n'
            f"Conversation: {ctx.conversation_id}\n"
            f"(demonstration system)"
        )
        result = client.send_text(settings.staff_number, note)
        if not result.ok:
            log.error("could not notify staff: %s", result.error)

    # -- health -----------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "demo": True,
            "whatsapp_configured": settings.configured,
            "signature_verification": settings.can_verify_signatures,
            "missing_settings": settings.missing(),
        }

    return app
