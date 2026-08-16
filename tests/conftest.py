"""Shared fixtures.

Every test runs against a frozen clock and a fixed reference date so the seeded
availability calendar resolves to the same concrete windows on any day the suite
is run. This is also what makes the learning loop's replay harness meaningful.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from rental_agent.config import load_rules
from rental_agent.context import ToolContext
from rental_agent.engine.engine import RentalEngine
from rental_agent.store.db import create_db_engine, init_db

TZ = ZoneInfo("Asia/Dubai")

#: Tuesday. Deliberately mid-week so "this weekend" is unambiguous in scenarios.
REFERENCE_DATE = date(2026, 9, 1)
FROZEN_NOW = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)


def dt(day: int, hour: int = 12, minute: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def rules():
    return load_rules()


@pytest.fixture
def engine() -> RentalEngine:
    return RentalEngine(now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)


@pytest.fixture
def engine_factory():
    """Build an engine with extra availability blocks, to simulate live demo
    reservations without needing the (Milestone 2) reservation store."""

    def _make(extra_blocks_provider=None) -> RentalEngine:
        return RentalEngine(
            now_fn=lambda: FROZEN_NOW,
            reference_date=REFERENCE_DATE,
            extra_blocks_provider=extra_blocks_provider,
        )

    return _make


@pytest.fixture
def ctx(engine) -> ToolContext:
    """Read-only context: engine access, no persistence."""
    return ToolContext.read_only(engine)


@pytest.fixture
def session_factory(tmp_path):
    """A real file-backed SQLite database per test.

    A file rather than :memory: so the connection pooling behaviour matches
    what the deployed prototype will actually do.
    """
    db_engine = create_db_engine(tmp_path / "test.db")
    return init_db(db_engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as sess:
        yield sess
        sess.commit()


@pytest.fixture
def booking_ctx(session) -> ToolContext:
    """A context with a customer and an open conversation, ready to book."""
    ctx = ToolContext(
        session=session,
        now_fn=lambda: FROZEN_NOW,
        reference_date=REFERENCE_DATE,
    )
    customer, _ = ctx.customers.get_or_create("+971500000001", FROZEN_NOW)
    conversation, _ = ctx.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
    ctx.customer_id = customer.customer_id
    ctx.conversation_id = conversation.conversation_id
    return ctx
