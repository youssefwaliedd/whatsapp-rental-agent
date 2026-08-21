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

from ..config import load_rules
from ..context import ToolContext
from ..formatting import photo_caption
from ..services import handover
from ..store.models import Escalation
from . import reactions as reactions_mod
from . import window as window_mod
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
            background.add_task(_dispatch, message)

        # Acknowledge before doing the work. A turn takes far longer than Meta
        # waits, and a late 200 means a redelivery and a second reply.
        return Response(content="ok", status_code=200)

    # -- routing ----------------------------------------------------------

    def _dispatch(message: InboundMessage) -> None:
        """Decide who is talking before deciding what to say.

        A message from the owner is a decision about someone else's case, not an
        enquiry about renting a car. Without this split the owner's reply to an
        escalation would be treated as a new customer and answered by the sales
        agent, which is both useless and faintly absurd.
        """
        if settings.staff_number and message.from_number == settings.staff_number:
            _handle_owner(message)
            return
        _handle(message)

    def _sweep_overdue(session: Any) -> None:
        """Chase the owner, and stop leaving the customer on read.

        There is no scheduler here by design — a prototype that needs one is
        harder to hand over. Instead every inbound webhook is an opportunity to
        notice that a case has run past its window, which is enough while
        anything at all is happening and honest about what it is.
        """
        ctx = ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)
        for case in handover.overdue_cases(ctx):
            try:
                if handover.needs_reminder(ctx, case):
                    handover.mark_reminded(ctx, case)
                    if settings.staff_number:
                        client.send_text(
                            settings.staff_number,
                            f"⏰ Still waiting on case {case.case_code} — "
                            f"{case.reason.replace('_', ' ')}. "
                            "A customer is holding for this answer.",
                        )
                elif handover.has_timed_out(ctx, case):
                    handover.mark_timed_out(ctx, case)
                    customer = ctx.customers.get(case.customer_id or "")
                    # Only if we are still allowed to speak. Past 24 hours this
                    # would be rejected, and there is no point burning a
                    # template on "sorry, still waiting" when the decision
                    # itself will need one.
                    if customer is not None and window_mod.is_open(ctx, case.conversation_id):
                        client.send_text(
                            customer.whatsapp_id,
                            handover.customer_message(ctx, "timed_out"),
                        )
                session.commit()
            except Exception:  # noqa: BLE001 - a stuck case must not block a live turn
                session.rollback()
                log.exception("failed to sweep case %s", case.case_code)

    # -- the owner's side --------------------------------------------------

    def _handle_owner(message: InboundMessage) -> None:
        """Route a decision back to the one customer it belongs to."""
        session = session_factory()
        try:
            ctx = ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)
            client.mark_read(message.message_id)

            case = handover.find_case(
                ctx,
                reply_to_message_id=message.reply_to,
                text=message.text,
                button_id=message.button_id,
            )
            if case is None:
                _ask_owner_which_case(ctx, message)
                session.commit()
                return

            outcome = handover.outcome_of(
                ctx, button_id=message.button_id, text=message.text
            )
            if outcome is None:
                # "No" and "no problem" mean opposite things. Rather than guess
                # at a real customer's case, put the buttons back.
                _ask_owner_again(ctx, case)
                session.commit()
                return

            note = "" if message.button_id else message.text
            handover.record_decision(ctx, case, outcome=outcome, note=note)
            session.commit()

            client.send_text(
                settings.staff_number,
                f"Got it — case {case.case_code} marked *{outcome.replace('_', ' ')}*. "
                "Letting the customer know now.",
            )
            _relay_to_customer(session, case)
        except Exception:  # noqa: BLE001 - one bad decision must not kill the worker
            session.rollback()
            log.exception("failed to handle owner reply %s", message.message_id)
        finally:
            session.close()

    def _ask_owner_which_case(ctx: ToolContext, message: InboundMessage) -> None:
        open_cases = handover.open_cases(ctx)
        if not open_cases:
            client.send_text(
                settings.staff_number,
                "Thanks — there are no open cases waiting on a decision right now.",
            )
            return
        listed = "\n".join(
            f"  {c.case_code} — {c.reason.replace('_', ' ')}" for c in open_cases
        )
        client.send_text(
            settings.staff_number,
            "I couldn't tell which case that was about. Reply to the original "
            f"message, or include the code:\n\n{listed}",
        )

    def _ask_owner_again(ctx: ToolContext, case: Escalation) -> None:
        client.send_buttons(
            settings.staff_number,
            f"Sorry — I couldn't read that as a yes or a no. Case {case.case_code}:\n\n"
            f"{case.question or case.detail or ''}",
            _decision_buttons(ctx, case),
        )

    def _decision_buttons(ctx: ToolContext, case: Escalation) -> list[dict[str, str]]:
        return [
            {"id": f"{option['id']}:{case.case_code}", "title": option["label"]}
            for option in handover.decision_options(ctx)
        ]

    def _relay_to_customer(session: Any, case: Escalation) -> None:
        """Tell the customer what was decided, in the agent's own voice.

        The decision itself is settled and recorded; only the wording is the
        model's job. `mark_relayed` runs after the send, because a decision
        nobody managed to deliver has not resolved anything.

        If the 24-hour service window has closed — an owner answering the next
        morning is entirely normal — a free-form message would be rejected by
        Meta and the customer would hear nothing at all. In that case a template
        goes out instead to earn a reply, and the answer waits until it can
        actually be delivered.
        """
        ctx = ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)
        ctx.customer_id = case.customer_id
        ctx.conversation_id = case.conversation_id

        record = ctx.customers.get(case.customer_id or "")
        if record is None:
            log.error("case %s has no customer to relay to", case.case_code)
            return

        if not window_mod.is_open(ctx, case.conversation_id):
            _request_reopen(ctx, case, record)
            return

        turn = agent_factory().relay(ctx, handover.relay_directive(case))
        session.commit()

        client.send_text(record.whatsapp_id, turn.reply)
        handover.mark_relayed(ctx, case)
        session.commit()

    def _request_reopen(ctx: ToolContext, case: Escalation, record: Any) -> None:
        """Ask a customer we can no longer message freely to come back to us.

        Deliberately does not mark the case relayed: the answer exists and they
        have not heard it. It stays owed until they reply.
        """
        template = handover.reopen_template(ctx)
        result = client.send_template(
            record.whatsapp_id,
            template["name"],
            language=template["language"],
            body_params=[record.name or "there"],
        )
        if not result.ok:
            log.error(
                "case %s: 24h window shut and the reopen template failed: %s",
                case.case_code,
                result.error,
            )
            return

        handover.mark_reopen_requested(ctx, case)
        ctx.session.commit()
        log.info("case %s: window shut, asked the customer to reply", case.case_code)

    # -- the customer's side -----------------------------------------------

    def _handle(message: InboundMessage) -> None:
        """Answer one customer message. Runs after the 200 has gone back."""
        session = session_factory()
        try:
            _sweep_overdue(session)
            ctx = _context_for(session, message)

            if message.unsupported:
                client.send_text(message.from_number, UNSUPPORTED_REPLY)
                session.commit()
                return

            # A decision that was ready while we could not reach them. Their
            # message has just reopened the window, so it goes out now — before
            # anything else, because they have been owed it since yesterday.
            owed = handover.decided_awaiting_relay(ctx, ctx.conversation_id or "")
            if owed is not None:
                if _record_inbound(ctx, message):
                    return
                client.send_typing(message.message_id)
                turn = agent_factory().relay(ctx, handover.relay_directive(owed))
                session.commit()
                client.send_text(message.from_number, turn.reply, typing_for=message.message_id)
                handover.mark_relayed(ctx, owed)
                session.commit()
                return

            waiting = handover.pending_for_conversation(ctx, ctx.conversation_id or "")
            if waiting is not None:
                # The agent already escalated this and is waiting on a person.
                # Answering now would mean resuming exactly the guessing that
                # the escalation existed to stop.
                if _record_inbound(ctx, message):
                    return
                client.mark_read(message.message_id)
                holding = handover.customer_message(ctx, "already_waiting")
                client.send_text(message.from_number, holding)
                ctx.messages.record(
                    conversation_id=ctx.conversation_id or "",
                    direction="outbound",
                    content=holding,
                    now=ctx.now(),
                )
                session.commit()
                return

            _acknowledge(message)

            turn = agent_factory().respond(
                ctx, message.text, provider_message_id=message.message_id
            )
            session.commit()

            if turn.duplicate:
                log.info("ignored a redelivery of %s", message.message_id)
                return

            # React before replying: a person marks the message they are
            # answering, then answers. The emoji is derived from what the engine
            # actually did, so it cannot congratulate a booking that failed.
            emoji = reactions_mod.for_turn(turn, load_rules())
            if emoji:
                client.send_reaction(message.from_number, message.message_id, emoji)

            client.send_text(message.from_number, turn.reply, typing_for=message.message_id)
            _send_photos(message, turn)

            if turn.escalated:
                _notify_staff(ctx, message, turn)

            session.commit()
        except Exception:  # noqa: BLE001 - one bad turn must not kill the worker
            session.rollback()
            log.exception("failed to handle %s", message.message_id)
        finally:
            session.close()

    def _record_inbound(ctx: ToolContext, message: InboundMessage) -> bool:
        """Write the customer's message to the transcript. Returns True on a
        redelivery, which the caller must drop.

        The agent normally does this itself, but the two paths that answer
        *without* running a turn — relaying an owed decision, and holding a
        customer while a case is open — would otherwise lose the message
        entirely. Two things break when that happens: the transcript the
        evaluator reads is missing a customer turn, and the 24-hour service
        window is measured from exactly this record, so it would never reopen.
        """
        _, duplicate = ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="inbound",
            content=message.text,
            now=ctx.now(),
            provider_message_id=message.message_id,
        )
        if duplicate:
            log.info("ignored a redelivery of %s", message.message_id)
        return duplicate

    def _acknowledge(message: InboundMessage) -> None:
        """Blue ticks, and the typing bubble when the config asks for it.

        Both ride on the same Cloud API call, so this is one request either way
        — the choice is only whether the customer sees "typing…" while the turn
        runs or just a read receipt.
        """
        if load_rules().messaging.get("typing_indicator", True):
            client.send_typing(message.message_id)
        else:
            client.mark_read(message.message_id)

    def _send_photos(message: InboundMessage, turn: Any) -> None:
        """Deliver whatever the agent asked to show, after the words.

        The reply is the answer and goes first; photos are supporting material.
        Sending them ahead of the text would make the customer scroll back up to
        find out what they are looking at.
        """
        for item in getattr(turn, "media", []) or []:
            urls = [client.card_url(path) for path in item.get("images", [])]
            urls = [u for u in urls if u]
            if not urls:
                # No public base URL configured. Meta fetches images itself, so
                # a local path would fail silently on the customer's phone —
                # better a log line here than a blank bubble there.
                log.warning(
                    "no WHATSAPP_MEDIA_BASE_URL: cannot send photos of %s",
                    item.get("vehicle_id"),
                )
                continue
            result = client.send_images(
                message.from_number,
                urls,
                caption=photo_caption(
                    item.get("caption"), item.get("display_name", ""), turn.reply
                ),
                typing_for=message.message_id,
            )
            if not result.ok:
                log.error("could not send photos of %s: %s", item.get("vehicle_id"), result.error)

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
        """Put a decidable question to the owner and open a case on the answer.

        The difference between this and a notification is the whole point of the
        brief's section 3: a notice tells the owner something has gone wrong, and
        leaves them to work out what to do about it in a different app. A case
        asks one question, records the answer, and carries it back to the
        customer who is waiting for it.

        Best effort by design: a failure here is logged but never raised, since
        the customer has already been told a colleague is taking over and
        breaking the turn now would make things worse, not better.
        """
        if not settings.staff_number:
            log.warning("escalation with no WHATSAPP_STAFF_NUMBER configured")
            return

        state = ctx.load_state()
        escalation = ctx.escalations.latest_for_conversation(ctx.conversation_id or "")
        if escalation is None:
            log.error("escalated turn with no escalation record on %s", ctx.conversation_id)
            return

        reason = (state.escalation_reason or "unspecified").replace("_", " ")
        question = f"{reason} — {message.text[:200]}"
        handover.open_case(ctx, escalation, question)
        ctx.session.commit()

        customer = ctx.customers.get(ctx.customer_id or "")
        note = (
            f"⚠️ *{reason.title()}* — case {escalation.case_code}\n\n"
            f"Customer: {customer.name or message.from_number}\n"
            f'They said: "{message.text[:300]}"\n\n'
            "What should I tell them?\n"
            "_(demonstration system)_"
        )
        result = client.send_buttons(
            settings.staff_number, note, _decision_buttons(ctx, escalation)
        )
        if not result.ok:
            log.error("could not notify staff: %s", result.error)
            return

        # Remember which message the owner will be replying to. With several
        # cases open at once this is the precise route back to this one.
        if result.message_ids:
            handover.record_notification(ctx, escalation, result.message_ids[0])
            ctx.session.commit()

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
