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
from rental_agent.engine.engine import RentalEngine

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
