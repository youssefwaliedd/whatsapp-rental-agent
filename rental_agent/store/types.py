"""Column types.

SQLite has no decimal and no timezone. Both matter here: a quote must read back
byte-identical to what the customer was shown, and a pickup time an hour off is
a missed delivery. So both are stored as text and converted at the boundary
rather than trusting the driver.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import String, TypeDecorator


class Money(TypeDecorator):
    """Decimal stored as text.

    SQLAlchemy's Numeric maps to REAL on SQLite, which would reintroduce exactly
    the float drift the engine is careful to avoid.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(Decimal(str(value)))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)


class AwareDateTime(TypeDecorator):
    """Timezone-aware datetime stored as an ISO 8601 string with its offset.

    Naive datetimes are rejected rather than silently assumed: a booking time
    with no timezone is a bug we want to find in tests, not in a delivery.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"Expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError(f"Refusing to store a naive datetime: {value!r}")
        return value.isoformat()

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value)
