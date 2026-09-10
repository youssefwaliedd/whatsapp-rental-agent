"""Attachment receipts and explicit staff review, separate from demo paperwork."""
from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select

from ..store.models import CustomerDocument
from ..whatsapp.media import MediaError, TransientMediaError


def for_customer(ctx):
    if ctx.session is None or not ctx.customer_id:
        return []
    return list(ctx.session.scalars(select(CustomerDocument).where(
        CustomerDocument.customer_id == ctx.customer_id
    ).order_by(CustomerDocument.created_at, CustomerDocument.document_id)))


def summary(ctx):
    # No paths, media URLs, file contents, or identity numbers reach the model.
    return [{"reference": d.document_id, "status": d.status,
             "document_type": d.document_type, "reservation_id": d.reservation_id}
            for d in for_customer(ctx)]


def receive(ctx, message, store, downloader):
    existing = ctx.session.scalar(select(CustomerDocument).where(
        CustomerDocument.provider_message_id == message.message_id))
    if existing:
        return existing
    reservation = ctx.reservations.active_for_customer(ctx.customer_id)
    from . import checkout
    state = ctx.load_state()
    if checkout.enabled(ctx) and state.checkout.get('mode') == 'new':
        reservation = ctx.reservations.get(state.reservation_id) if state.reservation_id else None
        if reservation and reservation.customer_id != ctx.customer_id:
            reservation = None
    row = CustomerDocument(
        document_id="DOC-" + uuid4().hex[:12].upper(),
        provider_message_id=message.message_id, customer_id=ctx.customer_id,
        conversation_id=ctx.conversation_id,
        reservation_id=reservation.reservation_id if reservation else None,
        created_at=ctx.now(), status="download_failed",
    )
    ctx.session.add(row)
    ctx.session.flush()
    try:
        media_id = (message.raw.get(message.media_kind) or {}).get("id", "")
        data, mime = downloader(media_id)
        row.storage_key = store.save(data, mime)
        row.mime_type = mime
        row.sha256 = hashlib.sha256(data).hexdigest()
        row.size_bytes = len(data)
        row.status = "pending_review"
    except (MediaError, OSError) as exc:
        if ctx.session.info.get("durable_turn") and isinstance(exc, (TransientMediaError, OSError)):
            raise
        # Persist a failed receipt, never a false success or a processor URL.
        row.status = "download_failed"
    return row


def review_types(ctx):
    required = ctx.engine.rules["required_documents"]
    allowed = {kind for kinds in required.values() if isinstance(kinds, list) for kind in kinds}
    allowed.discard("credit_card_in_driver_name")
    return sorted(allowed)


def review(ctx, reference, decision, detail, reviewer):
    row = ctx.session.scalar(select(CustomerDocument).where(
        CustomerDocument.document_id == reference).with_for_update().execution_options(populate_existing=True))
    if row is None:
        return None, "Document reference not found."
    if row.status != "pending_review":
        return None, f"{reference} is {row.status}. No decision changed."
    if not row.review_copy_sent_at:
        return None, f"View the attachment first: DOC {reference} VIEW"
    # Card possession is checked at handover, never by collecting card photos.
    allowed = review_types(ctx)
    if decision == "APPROVE":
        if detail not in allowed:
            return None, "Specify one document type: " + ", ".join(sorted(allowed))
        row.document_type = detail
        row.status = "approved"
    elif decision == "REJECT" and detail.strip():
        row.status = "rejected"
        row.review_note = detail.strip()[:300]
    else:
        return None, "Use DOC <reference> APPROVE <document_type> or DOC <reference> REJECT <reason>."
    row.reviewed_by = reviewer
    row.reviewed_at = ctx.now()
    # Does not create a booking, authorise payment, or make demo documents real.
    return row, f"{reference}: {row.status}."


def customer_notice(row):
    if row.status == "approved":
        return (f"A colleague has approved your {row.document_type.replace('_', ' ')} "
                f"({row.document_id}). This approval covers this document only.")
    return (f"A colleague has requested a replacement for document {row.document_id}: "
            f"{row.review_note}. Please attach the replacement here in WhatsApp.")
