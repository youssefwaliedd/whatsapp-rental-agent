"""Availability. Deterministic, calendar-based, no model involvement.

Two sources of unavailability are combined:
  1. Seeded blocks from fleet.json, expressed as offsets from a reference date
     so the demo fleet stays realistic however long after seeding it is shown.
  2. Live demo reservations, passed in as `extra_blocks` by the engine.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..domain.enums import UnavailabilityReason, VehicleStatus
from ..domain.models import AvailabilityResult, Vehicle


class Window:
    """A concrete, timezone-aware unavailable period, half-open: [start, end)."""

    __slots__ = ("start", "end", "reason")

    def __init__(self, start: datetime, end: datetime, reason: str = "on_rent"):
        self.start = start
        self.end = end
        self.reason = reason

    def overlaps(self, pickup_at: datetime, return_at: datetime) -> bool:
        return self.start < return_at and pickup_at < self.end

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Window({self.start.isoformat()} → {self.end.isoformat()}, {self.reason})"


def resolve_blocked_windows(
    vehicle: Vehicle, reference_date: date, tz: ZoneInfo
) -> list[Window]:
    """Turn the vehicle's relative blocked ranges into concrete windows.

    `end_offset_days` is inclusive of that entire day, so the window ends at
    00:00 the following day.
    """
    windows: list[Window] = []
    for block in vehicle.blocked_ranges:
        start_day = reference_date + timedelta(days=block.start_offset_days)
        end_day = reference_date + timedelta(days=block.end_offset_days + 1)
        windows.append(
            Window(
                datetime.combine(start_day, time.min, tzinfo=tz),
                datetime.combine(end_day, time.min, tzinfo=tz),
                block.reason,
            )
        )
    return windows


def check_availability(
    vehicle: Vehicle,
    pickup_at: datetime,
    return_at: datetime,
    *,
    reference_date: date,
    tz: ZoneInfo,
    extra_blocks: list[Window] | None = None,
) -> AvailabilityResult:
    if vehicle.status is VehicleStatus.RETIRED:
        return AvailabilityResult(
            vehicle_id=vehicle.id, available=False, reason=UnavailabilityReason.RETIRED
        )
    if vehicle.status is VehicleStatus.MAINTENANCE:
        return AvailabilityResult(
            vehicle_id=vehicle.id, available=False, reason=UnavailabilityReason.MAINTENANCE
        )

    windows = resolve_blocked_windows(vehicle, reference_date, tz)
    if extra_blocks:
        windows.extend(extra_blocks)

    conflicts = [w for w in windows if w.overlaps(pickup_at, return_at)]
    if not conflicts:
        return AvailabilityResult(vehicle_id=vehicle.id, available=True)

    # Report the earliest moment the whole requested duration could start.
    latest_end = max(w.end for w in conflicts)
    return AvailabilityResult(
        vehicle_id=vehicle.id,
        available=False,
        reason=UnavailabilityReason(conflicts[0].reason)
        if conflicts[0].reason in {r.value for r in UnavailabilityReason}
        else UnavailabilityReason.ON_RENT,
        next_available_from=latest_end,
    )
