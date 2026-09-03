"""Persistence schema.

Design notes that matter later:

* Conversation *state* is stored as a JSON blob in its own column, separate from
  the message transcript. The agent reads state; the transcript is evidence for
  the evaluator. Keeping them apart is what stops the agent re-reading chat
  history and re-asking questions.
* `tool_calls` is both the audit log and the idempotency ledger. One table, so
  a replayed call cannot be served from a cache that the audit log never saw.
* `reservations.history` is append-only. Modifications never overwrite; the
  evaluator needs to see what changed and when.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import AwareDateTime, Money


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String, primary_key=True)
    #: The WhatsApp phone number. The only identity the prototype has.
    whatsapp_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String, default=None)
    email: Mapped[str | None] = mapped_column(String, default=None)
    residency: Mapped[str] = mapped_column(String, default="unknown")
    date_of_birth: Mapped[str | None] = mapped_column(String, default=None)
    driver_age: Mapped[int | None] = mapped_column(Integer, default=None)
    documents_on_file: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: Learned, non-authoritative: colour, favourite models, preferred pickup
    #: time, tone. Never business facts.
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(AwareDateTime)

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="customer")


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True)

    stage: Mapped[str] = mapped_column(String, default="new_lead", index=True)
    intent: Mapped[str] = mapped_column(String, default="unknown")
    #: The serialised ConversationState. Source of truth for what the agent knows.
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    strategy_version: Mapped[str | None] = mapped_column(String, default=None)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    escalation_reason: Mapped[str | None] = mapped_column(String, default=None)
    #: Set when the conversation closes; the evaluator reads it. Null means
    #: open, and `open_for_customer` relies on that — so how a *sale* ended
    #: cannot live here. Writing "booked" into it would close the thread and
    #: greet the same customer as a stranger on their next message.
    outcome: Mapped[str | None] = mapped_column(String, default=None)
    #: How the sale went: booked, dropped or escalated. Their brief's section 4,
    #: and the thing that makes a pile of transcripts searchable — "show me
    #: everyone who went quiet after a quote" is the question worth asking.
    sales_outcome: Mapped[str | None] = mapped_column(String, default=None, index=True)

    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime)
    last_message_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)

    customer: Mapped[Customer] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), index=True
    )
    direction: Mapped[str] = mapped_column(String)  # inbound | outbound
    #: WhatsApp's message id. Unique, so a webhook retry cannot double-process.
    #: Nullable because simulator and agent messages have no provider id.
    provider_message_id: Mapped[str | None] = mapped_column(String, default=None)
    content: Mapped[str] = mapped_column(Text)
    media: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        # A partial-style guarantee: SQLite treats NULLs as distinct, so many
        # rows may have no provider id while real ids stay unique.
        UniqueConstraint("provider_message_id", name="uq_messages_provider_message_id"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )


class ToolCall(Base):
    """Audit log and idempotency ledger in one table."""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str | None] = mapped_column(String, index=True, default=None)
    tool_name: Mapped[str] = mapped_column(String, index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="ok")  # ok | error
    error_code: Mapped[str | None] = mapped_column(String, default=None)
    #: Only set for state-changing tools. Read-only tools are audited but never
    #: replayed from the ledger — re-reading availability must always be fresh.
    idempotency_key: Mapped[str | None] = mapped_column(String, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)

    __table_args__ = (
        UniqueConstraint("tool_name", "idempotency_key", name="uq_tool_calls_idempotency"),
    )


class Quote(Base):
    __tablename__ = "quotes"

    quote_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String, index=True, default=None)
    customer_id: Mapped[str | None] = mapped_column(String, index=True, default=None)
    vehicle_id: Mapped[str] = mapped_column(String, index=True)
    #: The full serialised Quote, so the exact figures shown can be reproduced
    #: even if config or pricing logic changes afterwards.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    total_charge: Mapped[Decimal] = mapped_column(Money)
    #: Null where the operator has not confirmed a deposit for this vehicle. The
    #: column has to allow it or the quote cannot be stored at all, and the
    #: customer's booking fails on a figure that was never needed to take it.
    deposit: Mapped[Decimal | None] = mapped_column(Money, default=None)
    status: Mapped[str] = mapped_column(String, default="active")  # active|superseded|converted
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime)


class Reservation(Base):
    __tablename__ = "reservations"

    reservation_id: Mapped[str] = mapped_column(String, primary_key=True)
    #: Structurally true, never derived from a flag the model could set.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String, index=True, default=None)
    vehicle_id: Mapped[str] = mapped_column(String, index=True)
    quote_id: Mapped[str | None] = mapped_column(String, default=None)

    status: Mapped[str] = mapped_column(String, default="confirmed", index=True)
    pickup_at: Mapped[datetime] = mapped_column(AwareDateTime)
    return_at: Mapped[datetime] = mapped_column(AwareDateTime)
    delivery_location: Mapped[str | None] = mapped_column(String, default=None)

    total_charge: Mapped[Decimal] = mapped_column(Money)
    deposit: Mapped[Decimal | None] = mapped_column(Money, default=None)
    currency: Mapped[str] = mapped_column(String, default="AED")

    #: All simulated. none | authorised | paid | refunded
    payment_status: Mapped[str] = mapped_column(String, default="none")
    payment_reference: Mapped[str | None] = mapped_column(String, default=None)
    documents: Mapped[list[str]] = mapped_column(JSON, default=list)
    delivery_scheduled_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)

    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime)
    #: Bumped on every change. Auto-derived idempotency keys include it, so
    #: "move it to 8pm" applied, reverted, then applied again is three distinct
    #: operations rather than one replayed twice.
    version: Mapped[int] = mapped_column(Integer, default=1)
    #: Append-only. Modifications add entries; nothing is overwritten.
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    __table_args__ = (
        Index("ix_reservations_vehicle_status", "vehicle_id", "status"),
    )


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    customer_id: Mapped[str | None] = mapped_column(String, default=None)
    reason: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    #: Populated once a staff notification is actually sent (Milestone 4).
    notified_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)
    #: open → awaiting_decision → decided → relayed. `timed_out` is a branch off
    #: awaiting_decision that stays open: a late answer is still worth having.
    status: Mapped[str] = mapped_column(String, default="open")
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)

    # -- the question put to the owner ------------------------------------

    #: What the owner was actually asked. Stored so the decision is
    #: interpretable months later, when "approve" on its own means nothing.
    question: Mapped[str | None] = mapped_column(Text, default=None)
    #: Short human code — "4A2". The fallback route when the owner types a new
    #: message instead of replying to ours.
    case_code: Mapped[str | None] = mapped_column(String, index=True, default=None)
    #: The wamid of the message we sent the owner. WhatsApp puts this in
    #: `context.id` when they swipe-to-reply, which is the precise route back to
    #: this case even with several open at once.
    notification_message_id: Mapped[str | None] = mapped_column(String, index=True, default=None)
    reminded_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)

    # -- the answer --------------------------------------------------------

    #: approved | declined | owner_calling. Null until the owner answers.
    decision: Mapped[str | None] = mapped_column(String, default=None)
    #: Anything the owner typed alongside the decision — a condition, an amount,
    #: a reason. Authority, but recorded rather than interpreted.
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)
    decided_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)
    #: When the customer was actually told. A decision nobody relayed has not
    #: resolved anything.
    relayed_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)
    #: Set when the answer was ready but the 24-hour window had closed, so a
    #: template went out asking the customer to reply. The decision is still
    #: owed; it is delivered the moment they do.
    reopen_requested_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)


class Counter(Base):
    """Sequential demo references. A table rather than max()+1 so two concurrent
    bookings cannot be handed the same DEMO- number."""

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Evaluation(Base):
    """One assessment of one conversation.

    Findings are stored with the evidence that produced them, so a mistake can
    always be traced back to the message and tool call that prove it — the
    difference between a learning loop and a rumour mill.
    """

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    #: Which agent strategy produced the conversation being judged.
    strategy_version: Mapped[str | None] = mapped_column(String, default=None)
    outcome: Mapped[str | None] = mapped_column(String, default=None)

    message_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Customer messages per agent question — lower is a tighter conversation.
    question_count: Mapped[int] = mapped_column(Integer, default=0)

    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)


class Mistake(Base):
    """A classified mistake, promoted from an evaluation finding.

    Deliberately separate from `evaluations`: an evaluation is a snapshot of one
    conversation, while a mistake is a durable lesson that outlives it and can be
    retrieved into future conversations.
    """

    __tablename__ = "mistakes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="medium")
    situation: Mapped[str] = mapped_column(Text)
    bad_behavior: Mapped[str] = mapped_column(Text)
    correct_behavior: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    conversation_id: Mapped[str | None] = mapped_column(String, default=None)
    agent_version: Mapped[str | None] = mapped_column(String, default=None)
    #: detected -> corrected -> tested -> active
    status: Mapped[str] = mapped_column(String, default="detected", index=True)
    #: How many separate conversations produced this same mistake.
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    #: Which ones. Kept so re-evaluating a conversation cannot inflate the count
    #: — the number has to mean "distinct conversations", or a habit seen once
    #: looks like a crisis after a few re-runs.
    conversation_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime)


class BookingOperation(Base):
    """One write sent to a booking provider, and what became of it.

    The record that makes a timeout survivable. A reservation created a moment
    before the connection dropped exists in the provider and nowhere in our
    conversation, and repeating the request would give the customer a second
    car. This is where `resolve` looks it up by the key the caller derived, so
    the retry finds the original instead of making another.

    Written before the operation is attempted and updated after, so a row with
    no outcome is exactly the case that needs reconciling.
    """

    __tablename__ = "booking_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String)
    #: reserve | modify | cancel
    kind: Mapped[str] = mapped_column(String)
    customer_ref: Mapped[str | None] = mapped_column(String, index=True, default=None)
    #: The provider's reference, once there is one.
    reference: Mapped[str | None] = mapped_column(String, index=True, default=None)
    #: Null while the operation is in flight — the state that needs resolving.
    outcome: Mapped[str | None] = mapped_column(String, default=None)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    settled_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)


class Strategy(Base):
    """A versioned set of behavioural lessons appended to the agent's prompt.

    Deliberately *additive only*. A strategy cannot rewrite the system prompt,
    cannot touch configuration, and cannot carry a price, a policy or a limit —
    those come from the engine and are not learnable. What it can carry is how
    to conduct a conversation: what to ask first, when to offer alternatives,
    how to handle a push on price.

    A version is created as a candidate and only becomes active if every
    regression case still passes against it.
    """

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String, unique=True, index=True)
    #: candidate -> active -> superseded, or candidate -> rejected
    status: Mapped[str] = mapped_column(String, default="candidate", index=True)
    lessons: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: Mistake ids this version was built to correct.
    derived_from: Mapped[list[int]] = mapped_column(JSON, default=list)
    parent_version: Mapped[str | None] = mapped_column(String, default=None)

    replay_passed: Mapped[int] = mapped_column(Integer, default=0)
    replay_failed: Mapped[int] = mapped_column(Integer, default=0)
    rejection_reason: Mapped[str | None] = mapped_column(Text, default=None)
    #: Which cases failed and what appeared instead. A rejection without this is
    #: a dead end: nobody can tell whether the lessons are wrong or the cases
    #: are, and the run that would say costs an hour to repeat.
    replay_detail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    activated_at: Mapped[datetime | None] = mapped_column(AwareDateTime, default=None)


class RegressionCase(Base):
    """A real conversation that once went wrong, kept as a test.

    Stores the customer's turns and the mistake that must not recur. A candidate
    strategy is replayed against every case before it can be activated — which
    is what stops a correction for one problem from quietly creating another.
    """

    __tablename__ = "regression_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    #: The customer side of the conversation, in order.
    customer_turns: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: The finding type that must not appear when this is replayed.
    forbidden_finding: Mapped[str] = mapped_column(String, index=True)
    source_conversation_id: Mapped[str | None] = mapped_column(String, default=None)
    source_mistake_id: Mapped[int | None] = mapped_column(Integer, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
