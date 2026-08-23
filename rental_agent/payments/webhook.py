"""The provider telling us a customer actually paid.

Without this the system asks for money and never learns that it arrived. A
customer who has just paid AED 2,937.90 and asks "is that confirmed?" gets told
their payment is outstanding — which is worse than never having asked, because
now it looks like the money is lost.

Two things this refuses to do:

**Trust an unsigned event.** Anyone who learns the URL could otherwise post
"paid" for any booking. Stripe signs every event; the signature is checked
against the raw body, before the JSON is parsed, exactly as the Meta webhook
does.

**Trust our own expectation over the processor's record.** The amount recorded is
the one Stripe says was received, not the one we asked for. They differ more
often than people expect — a customer paying an old link, a currency conversion,
a partial payment — and reconciling against what you hoped for is how money goes
missing on paper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("rental_agent.payments.webhook")

#: Stripe rejects its own events older than this by default. An attacker
#: replaying a captured "paid" event a day later should not succeed either.
TOLERANCE_SECONDS = 300

PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


def verify(payload: bytes, header: str | None, secret: str, *, now: float | None = None) -> bool:
    """Stripe's `t=…,v1=…` scheme, over the raw body.

    Compared with `compare_digest`: a timing-safe comparison costs nothing and
    the alternative leaks the signature one byte at a time.
    """
    if not header or not secret:
        return False
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    timestamp, signature = parts.get("t"), parts.get("v1")
    if not timestamp or not signature:
        return False
    try:
        age = (now or time.time()) - int(timestamp)
    except ValueError:
        return False
    if abs(age) > TOLERANCE_SECONDS:
        return False

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def build_router(
    *,
    session_factory: Callable[[], Any],
    context_factory: Callable[[Any], Any],
    on_paid: Callable[[Any, str], None] | None = None,
    secret: str | None = None,
) -> APIRouter:
    """The provider's callback, injected the same way the Meta one is."""
    router = APIRouter()

    @router.post("/payments/stripe")
    async def stripe_event(request: Request) -> dict[str, Any]:
        raw = await request.body()
        signing_secret = secret or os.getenv("STRIPE_WEBHOOK_SECRET", "")
        if not verify(raw, request.headers.get("stripe-signature"), signing_secret):
            # 400 rather than 403: Stripe retries on 5xx and gives up on 4xx,
            # and an event we cannot authenticate should never be retried.
            log.warning("rejected a payment event with a bad or missing signature")
            raise HTTPException(status_code=400, detail="signature verification failed")

        event = json.loads(raw)
        if event.get("type") not in PAID_EVENTS:
            return {"ignored": event.get("type")}

        session = event.get("data", {}).get("object", {})
        if session.get("payment_status") != "paid":
            return {"ignored": "not paid"}

        reservation_id = (session.get("metadata") or {}).get("reservation_id")
        if not reservation_id:
            log.error("paid session %s carries no reservation_id", session.get("id"))
            return {"ignored": "no reservation"}

        from ..services import payments as payments_service

        db = session_factory()
        try:
            ctx = context_factory(db)
            result = payments_service.mark_paid(
                ctx,
                reservation_id=reservation_id,
                amount_minor=session.get("amount_total"),
                currency=(session.get("currency") or "aed").upper(),
                reference=session.get("id", ""),
                event_id=event.get("id", ""),
            )
            db.commit()
        except Exception:  # noqa: BLE001 - a 500 makes Stripe retry, which is right
            db.rollback()
            log.exception("failed to record payment for %s", reservation_id)
            raise
        finally:
            db.close()

        if on_paid is not None and result.get("recorded"):
            try:
                on_paid(result, reservation_id)
            except Exception:  # noqa: BLE001 - the money is recorded either way
                log.exception("recorded the payment but could not tell the customer")
        return result

    return router
