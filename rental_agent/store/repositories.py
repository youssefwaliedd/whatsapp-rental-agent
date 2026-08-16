"""Repositories — every database read and write goes through here.

Keeping SQL out of the service layer means the reservation-blocking rule (which
statuses actually hold a car) is defined in exactly one place.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..engine.availability import Window
from .models import Conversation, Counter, Customer, Escalation, Message, Quote, Reservation, ToolCall

#: Statuses that actually hold a vehicle off the market.
BLOCKING_STATUSES = ("pending", "confirmed")
#: Statuses that count as "the customer's current booking".
LIVE_STATUSES = ("pending", "confirmed")


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------


class Counters:
    def __init__(self, session: Session):
        self.session = session

    def next(self, name: str) -> int:
        """Increment and return. Uses a row lock so two bookings in flight
        cannot be issued the same demo reference."""
        counter = self.session.get(Counter, name, with_for_update=True)
        if counter is None:
            counter = Counter(name=name, value=0)
            self.session.add(counter)
        counter.value += 1
        self.session.flush()
        return counter.value

    def next_reservation_reference(self) -> str:
        return f"DEMO-{self.next('reservation')}"

    def next_quote_reference(self) -> str:
        return f"DQ-{self.next('quote')}"


class Customers:
    def __init__(self, session: Session):
        self.session = session

    def get(self, customer_id: str) -> Customer | None:
        return self.session.get(Customer, customer_id)

    def by_whatsapp_id(self, whatsapp_id: str) -> Customer | None:
        return self.session.scalar(
            select(Customer).where(Customer.whatsapp_id == whatsapp_id)
        )

    def get_or_create(self, whatsapp_id: str, now: datetime) -> tuple[Customer, bool]:
        """Identity in the prototype is the phone number, nothing else.
        Returns (customer, was_created)."""
        existing = self.by_whatsapp_id(whatsapp_id)
        if existing is not None:
            existing.last_seen_at = now
            return existing, False

        customer = Customer(
            customer_id=_short_id("cus"),
            whatsapp_id=whatsapp_id,
            created_at=now,
            last_seen_at=now,
            documents_on_file=[],
            preferences={},
        )
        self.session.add(customer)
        self.session.flush()
        return customer, True

    def save_preference(self, customer: Customer, key: str, value: Any) -> None:
        """Preferences are conversational, never authoritative. Nothing written
        here may influence a price, an availability answer or a policy."""
        preferences = dict(customer.preferences or {})
        preferences[key] = value
        customer.preferences = preferences


class Conversations:
    def __init__(self, session: Session):
        self.session = session

    def get(self, conversation_id: str) -> Conversation | None:
        return self.session.get(Conversation, conversation_id)

    def open_for_customer(self, customer_id: str) -> Conversation | None:
        """The most recent conversation that has not been closed out.

        The prototype keeps one thread per customer: a WhatsApp chat has no
        notion of a new conversation, and treating a follow-up two days later as
        a fresh lead is exactly the memory failure the product is meant to avoid.
        """
        return self.session.scalar(
            select(Conversation)
            .where(
                Conversation.customer_id == customer_id,
                Conversation.outcome.is_(None),
            )
            .order_by(Conversation.created_at.desc())
        )

    def get_or_create(self, customer_id: str, now: datetime) -> tuple[Conversation, bool]:
        existing = self.open_for_customer(customer_id)
        if existing is not None:
            return existing, False

        conversation = Conversation(
            conversation_id=_short_id("conv"),
            customer_id=customer_id,
            state={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(conversation)
        self.session.flush()
        return conversation, True

    def save_state(self, conversation: Conversation, state: dict[str, Any], now: datetime) -> None:
        conversation.state = state
        conversation.stage = state.get("stage", conversation.stage)
        conversation.intent = state.get("intent", conversation.intent)
        conversation.updated_at = now
        # Flushed immediately: a later tool call in the same turn must see the
        # state this one just wrote.
        self.session.flush()

    def all(self) -> list[Conversation]:
        return list(
            self.session.scalars(select(Conversation).order_by(Conversation.created_at))
        )


class Messages:
    def __init__(self, session: Session):
        self.session = session

    def record(
        self,
        *,
        conversation_id: str,
        direction: str,
        content: str,
        now: datetime,
        provider_message_id: str | None = None,
        media: list[str] | None = None,
    ) -> tuple[Message, bool]:
        """Returns (message, is_duplicate).

        Deduplication is enforced by a unique index, not by a prior SELECT: the
        WhatsApp Cloud API retries deliveries, and two webhook workers can be
        processing the same id at the same moment.
        """
        if provider_message_id:
            existing = self.session.scalar(
                select(Message).where(Message.provider_message_id == provider_message_id)
            )
            if existing is not None:
                return existing, True

        message = Message(
            conversation_id=conversation_id,
            direction=direction,
            content=content,
            provider_message_id=provider_message_id,
            media=media or [],
            created_at=now,
        )
        self.session.add(message)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(Message).where(Message.provider_message_id == provider_message_id)
            )
            if existing is None:  # pragma: no cover - only on a non-dedup conflict
                raise
            return existing, True
        return message, False

    def for_conversation(self, conversation_id: str) -> list[Message]:
        return list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            )
        )


class ToolCalls:
    def __init__(self, session: Session):
        self.session = session

    def find_by_idempotency_key(self, tool_name: str, key: str) -> ToolCall | None:
        return self.session.scalar(
            select(ToolCall).where(
                ToolCall.tool_name == tool_name, ToolCall.idempotency_key == key
            )
        )

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        now: datetime,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        status: str = "ok",
        error_code: str | None = None,
        duration_ms: int | None = None,
    ) -> ToolCall:
        call = ToolCall(
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            status=status,
            error_code=error_code,
            idempotency_key=idempotency_key,
            duration_ms=duration_ms,
            created_at=now,
        )
        self.session.add(call)
        self.session.flush()
        return call

    def for_conversation(self, conversation_id: str) -> list[ToolCall]:
        return list(
            self.session.scalars(
                select(ToolCall)
                .where(ToolCall.conversation_id == conversation_id)
                .order_by(ToolCall.id)
            )
        )


class Quotes:
    def __init__(self, session: Session):
        self.session = session

    def get(self, quote_id: str) -> Quote | None:
        return self.session.get(Quote, quote_id)

    def save(
        self,
        *,
        quote_id: str,
        vehicle_id: str,
        payload: dict[str, Any],
        total_charge: Decimal,
        deposit: Decimal,
        created_at: datetime,
        expires_at: datetime,
        conversation_id: str | None = None,
        customer_id: str | None = None,
    ) -> Quote:
        quote = Quote(
            quote_id=quote_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            vehicle_id=vehicle_id,
            payload=payload,
            total_charge=total_charge,
            deposit=deposit,
            created_at=created_at,
            expires_at=expires_at,
        )
        self.session.add(quote)
        self.session.flush()
        return quote

    def mark_converted(self, quote_id: str) -> None:
        quote = self.get(quote_id)
        if quote is not None:
            quote.status = "converted"


class Reservations:
    def __init__(self, session: Session):
        self.session = session

    def get(self, reservation_id: str) -> Reservation | None:
        return self.session.get(Reservation, reservation_id)

    def create(self, **kwargs: Any) -> Reservation:
        reservation = Reservation(**kwargs)
        self.session.add(reservation)
        self.session.flush()
        return reservation

    def active_for_customer(self, customer_id: str) -> Reservation | None:
        """The booking a contextual follow-up most likely refers to."""
        return self.session.scalar(
            select(Reservation)
            .where(
                Reservation.customer_id == customer_id,
                Reservation.status.in_(LIVE_STATUSES),
            )
            .order_by(Reservation.created_at.desc())
        )

    def for_customer(self, customer_id: str) -> list[Reservation]:
        return list(
            self.session.scalars(
                select(Reservation)
                .where(Reservation.customer_id == customer_id)
                .order_by(Reservation.created_at)
            )
        )

    def all(self) -> list[Reservation]:
        return list(
            self.session.scalars(select(Reservation).order_by(Reservation.created_at))
        )

    def blocking_windows(
        self, vehicle_id: str, exclude_reservation_id: str | None = None
    ) -> list[Window]:
        """Windows in which live demo bookings hold this vehicle.

        `exclude_reservation_id` exists for modifications: when a customer moves
        their delivery from 7 PM to 8 PM, the booking must not be treated as a
        conflict with itself.
        """
        statement = select(Reservation).where(
            Reservation.vehicle_id == vehicle_id,
            Reservation.status.in_(BLOCKING_STATUSES),
        )
        if exclude_reservation_id:
            statement = statement.where(Reservation.reservation_id != exclude_reservation_id)
        return [
            Window(r.pickup_at, r.return_at, "on_rent")
            for r in self.session.scalars(statement)
        ]

    def append_history(
        self, reservation: Reservation, entry: dict[str, Any], now: datetime
    ) -> None:
        reservation.history = list(reservation.history or []) + [{**entry, "at": now.isoformat()}]
        reservation.updated_at = now


class Escalations:
    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        *,
        conversation_id: str,
        reason: str,
        now: datetime,
        customer_id: str | None = None,
        detail: str | None = None,
    ) -> Escalation:
        escalation = Escalation(
            conversation_id=conversation_id,
            customer_id=customer_id,
            reason=reason,
            detail=detail,
            created_at=now,
        )
        self.session.add(escalation)
        self.session.flush()
        return escalation

    def open_escalations(self) -> list[Escalation]:
        return list(
            self.session.scalars(select(Escalation).where(Escalation.status == "open"))
        )
