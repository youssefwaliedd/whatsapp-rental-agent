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
from ..payments import webhook as payments_webhook
from ..evaluation import evaluator
from ..services import booking, handover, outcomes
from ..sources import refresh as refresh_mod
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
        # Same opportunity, same reason: an inbound message is when we notice
        # things that became true while nothing was happening.
        try:
            outcomes.sweep_dropped(ctx)
        except Exception:  # noqa: BLE001 - reporting must never break a turn
            log.exception("failed to sweep dropped conversations")

        # A hold nobody answered stops being the customer's booking. Readers
        # already ignore an expired one, so this is the ledger catching up with
        # what is already true, not the thing that makes it true.
        try:
            released = booking.expire_holds(ctx)
            if released:
                log.info("released %d expired hold(s): %s", len(released), ", ".join(released))
        except Exception:  # noqa: BLE001 - a stale hold must not break a live turn
            log.exception("failed to expire holds")

        # A finished conversation is judged without anybody remembering to ask.
        # Safe to automate because it consults no model and changes nothing a
        # customer sees: it writes down what happened. Turning those findings
        # into lessons, proving them, and activating them stay deliberate.
        try:
            judged = evaluator.evaluate_finished(ctx)
            if judged:
                log.info("evaluated %d finished conversation(s)", len(judged))
        except Exception:  # noqa: BLE001 - reporting must never break a turn
            log.exception("failed to evaluate finished conversations")

    def _tell_owner_about_changes(ctx: ToolContext) -> None:
        """Send the owner a change the customer asked for after they were asked.

        Without this they answer the message they were sent, which is about the
        window the customer has since moved — and an approval would confirm a
        time nobody wants.
        """
        if not settings.staff_number:
            return
        try:
            for notice in booking.unsent_change_notices(ctx):
                client.send_text(
                    settings.staff_number,
                    f"↻ Case {notice['case_code']} — the customer has changed what they "
                    f"are asking for: {notice['wanted']}.\n"
                    f"Confirm only if {notice['reservation_id']} is free for that.",
                )
            ctx.session.commit()
        except Exception:  # noqa: BLE001 - the customer has already been answered
            ctx.session.rollback()
            log.exception("failed to tell the owner about a change request")

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
                ctx, button_id=message.button_id, text=message.text, reason=case.reason
            )
            if outcome is None:
                # "No" and "no problem" mean opposite things. Rather than guess
                # at a real customer's case, put the buttons back.
                _ask_owner_again(ctx, case)
                session.commit()
                return

            note = "" if message.button_id else message.text

            # The car first, then the record. Approval is not a status change —
            # it rechecks the vehicle, applies whatever the customer asked for
            # while they were deciding, and can fail. What actually happened is
            # what the case has to say, or the customer is told a booking exists
            # that does not.
            if case.reason == "booking_hold":
                reservation_id = (case.detail or "").split(":")[0].strip()
                applied = booking.apply_hold_decision(ctx, reservation_id, outcome)
                if applied is not None and applied["outcome"] == "withdrawn":
                    handover.close_case(ctx, case, applied["message"])
                    session.commit()
                    client.send_text(
                        settings.staff_number,
                        f"No action needed on case {case.case_code} — "
                        f"{applied['message']} Nothing has been booked.",
                    )
                    return
                if applied is not None and applied["outcome"] == "conflict":
                    # They said yes and the car is not free after all. The
                    # customer must hear the outcome, not the intention.
                    outcome = "declined"
                    note = applied["message"]
                    log.warning(
                        "approval refused on %s: %s", reservation_id, applied["reason"]
                    )
                elif applied is not None and applied["outcome"] == "confirmed":
                    # So the relay states the window that was actually
                    # confirmed, which is not always the one first requested.
                    case.detail = applied["summary"]

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
        # An answer case can only land here on an empty reply, so asking for a
        # yes or a no would be nonsense — it never wanted one.
        trouble = (
            "Sorry — I didn't catch a figure in that."
            if handover.needs_an_answer(ctx, case.reason)
            else "Sorry — I couldn't read that as a yes or a no."
        )
        client.send_buttons(
            settings.staff_number,
            f"{trouble} Case {case.case_code}:\n\n{case.question or case.detail or ''}",
            _decision_buttons(ctx, case),
        )

    def _decision_buttons(
        ctx: ToolContext, case: Escalation, reason: str | None = None
    ) -> list[dict[str, str]]:
        return [
            {"id": f"{option['id']}:{case.case_code}", "title": option["label"]}
            for option in handover.decision_options(ctx, reason or case.reason)
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
                ctx,
                message.text,
                provider_message_id=message.message_id,
                # Recorded on the message itself. The document check reads this:
                # without it the agent has only the customer's word that
                # anything was sent at all.
                media=[message.media_kind] if message.media_kind else None,
            )
            session.commit()

            if turn.duplicate:
                # Correct — Meta redelivers, and answering twice would double
                # up the reply and re-run the turn's tool calls. Logged at info
                # rather than debug because a message vanishing without a reply
                # is otherwise indistinguishable from a bug.
                log.info("ignored a redelivery of %s", message.message_id)
                return

            # React before replying: a person marks the message they are
            # answering, then answers. The emoji is derived from what the engine
            # actually did, so it cannot congratulate a booking that failed.
            emoji = reactions_mod.for_turn(turn, load_rules())
            if emoji:
                client.send_reaction(message.from_number, message.message_id, emoji)

            client.send_text(message.from_number, turn.reply, typing_for=message.message_id)
            _send_cards(message, turn)
            _send_photos(message, turn)

            if turn.escalated:
                _notify_staff(ctx, message, turn)

            session.commit()
            # After the customer has their answer: a change they asked for on a
            # request somebody is already deciding has to reach that person
            # before their decision does.
            _tell_owner_about_changes(ctx)
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

    def _send_cards(message: InboundMessage, turn: Any) -> None:
        """The engine's own figures, after the sentence and before the photos.

        A quote is the one message where wording is not the model's to choose.
        It writes what it likes around this; the breakdown itself — the total,
        the deposit line, what is due at delivery, the demonstration notice —
        is rendered from the quote that was actually stored.
        """
        for card in getattr(turn, "cards", []):
            result = client.send_text(message.from_number, card)
            if not result.ok:
                log.error("failed to send the quote breakdown: %s", result.error)


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
        # Commit, not just flush. On SQLite a flush opens the write transaction
        # and holds it until commit — which would be after the model call, so
        # one customer's turn would block every other customer for its whole
        # duration. Landing these two rows immediately keeps the lock held for
        # milliseconds instead of seconds.
        session.commit()
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

        raw_reason = state.escalation_reason or "unspecified"
        reason = raw_reason.replace("_", " ")
        question = f"{reason} — {message.text[:200]}"
        handover.open_case(ctx, escalation, question)
        ctx.session.commit()

        customer = ctx.customers.get(ctx.customer_id or "")
        who = customer.name or message.from_number
        deciding = handover.needs_a_decision(ctx, raw_reason)

        if handover.needs_an_answer(ctx, raw_reason):
            ask = handover.answer_prompt(ctx)
        elif deciding:
            ask = handover.decision_prompt(ctx, raw_reason)
        else:
            # No answer to choose between — someone has to take this over. The
            # number is included because the right next action is usually a
            # phone call, not a WhatsApp reply.
            ask = (
                f"*This needs a person.* I've told them a colleague is taking over "
                f"and I've stopped replying.\n"
                f"Reach them on +{message.from_number}."
            )

        # The context first, the question second. Their section 3 asks for full
        # context, and an interactive message body is capped at about a thousand
        # characters — not enough for the conversation that led here. So the
        # briefing is its own message and the buttons carry the decision, which
        # is also the order a person reads them in.
        briefing = handover.case_briefing(ctx, escalation, state)
        if briefing:
            headed = f"⚠️ *{reason.title()}* — case {escalation.case_code}\n\n{briefing}"
            if not client.send_text(settings.staff_number, headed).ok:
                # Never fatal: a decision asked without its briefing is worse
                # than nothing only if the question does not follow, and it does.
                log.error("could not send the case briefing for %s", escalation.case_code)

        note = (
            f"⚠️ *{reason.title()}* — case {escalation.case_code}\n"
            f"{who} said: \"{message.text[:300]}\"\n\n"
            f"{ask}\n"
            "_(demonstration system)_"
        )
        result = client.send_buttons(
            settings.staff_number, note, _decision_buttons(ctx, escalation, raw_reason)
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

    # -- the payment provider's callback ----------------------------------

    def _context(session: Any) -> ToolContext:
        return ToolContext(session=session, now_fn=now_fn, reference_date=reference_date)

    def _tell_them_it_arrived(result: dict[str, Any], reservation_id: str) -> None:
        """The customer paid and is waiting to hear that it landed.

        Delivered by the agent rather than as a fixed string, for the same
        reason a decision is: the facts are settled, the wording is its job. If
        the window has shut the payment is still recorded — money is not
        conditional on being able to send a message about it.
        """
        session = session_factory()
        try:
            ctx = _context(session)
            reservation = ctx.reservations.get(reservation_id)
            if reservation is None or not reservation.customer_id:
                return
            ctx.customer_id = reservation.customer_id
            ctx.conversation_id = reservation.conversation_id
            record = ctx.customers.get(reservation.customer_id)
            if record is None or not window_mod.is_open(ctx, reservation.conversation_id or ""):
                log.info("payment recorded for %s but the customer cannot be messaged",
                         reservation_id)
                return

            turn = agent_factory().relay(ctx, (
                f"Their payment of {result['currency']} {result['amount']} for booking "
                f"{reservation_id} has arrived and is recorded. Tell them it is received, "
                "briefly and warmly, and confirm what happens next for the delivery. Do not "
                "restate the amount as a different figure, and do not ask them to pay again."
            ))
            session.commit()
            client.send_text(record.whatsapp_id, turn.reply)
        except Exception:  # noqa: BLE001 - the money is recorded either way
            session.rollback()
            log.exception("could not tell %s their payment arrived", reservation_id)
        finally:
            session.close()

    app.include_router(
        payments_webhook.build_router(
            session_factory=session_factory,
            context_factory=_context,
            on_paid=_tell_them_it_arrived,
        )
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "demo": True,
            "whatsapp_configured": settings.configured,
            "signature_verification": settings.can_verify_signatures,
            "missing_settings": settings.missing(),
            "fleet_refresh": refresh_mod.STATE.as_dict(),
        }

    return app
