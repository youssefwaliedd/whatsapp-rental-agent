"""Keeping the fleet current without asking the operator's site mid-conversation.

Querying live is not an option and the numbers say so plainly: their catalogue
API takes about three seconds, a vehicle page about two, and prices live only on
the pages — so a search across a hundred-odd cars would be four minutes against
a two-to-five second target. It would also still not answer the question that
matters, because their site publishes no availability at any speed.

So the fleet is a snapshot, refreshed on a schedule. Fresh within a day, instant
at conversation time, and a failure lands at four in the morning in a log rather
than at five in the afternoon in front of a customer.

**The important part of this module is what it refuses to do.** An import that
comes back empty because the site was down, or half-length because a plugin
changed the price markup, must never replace good data — an agent that has
forgotten ninety cars is far worse than one quoting yesterday's prices. So the
new fleet is validated before it is written, the previous file is kept, and a
refresh that fails leaves everything exactly as it was.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import config as config_mod
from ..env import load_dotenv

# The switch below lives in .env alongside every other setting, so reading it
# must not depend on whichever module happened to be imported first.
load_dotenv()

log = logging.getLogger("rental_agent.refresh")

#: Prices and fleet composition change on the order of weeks; availability
#: changes hourly but is not on their site at any frequency, so refreshing
#: faster buys nothing. Nightly, in the quiet hour after any evening edits.
DEFAULT_HOUR = 4
DEFAULT_TIMEZONE = "Asia/Dubai"

#: A refresh returning fewer than this share of the current fleet is treated as
#: a broken import rather than a shrinking company. Rental firms do not lose a
#: quarter of their cars overnight; scrapers break like that all the time.
MINIMUM_RETAINED = 0.75

#: After a failure the fleet is knowingly stale, so waiting out the remaining
#: day before looking again is the wrong trade — an hour is often enough for a
#: deploy or an outage on their side to have ended. Retries stop at the next
#: scheduled run, which would happen anyway.
DEFAULT_RETRY_MINUTES = 60

#: Written into the fleet document on every successful refresh, so a restarted
#: process can tell how old the snapshot on disk is. In-memory state cannot
#: answer that — it is empty at boot, which is exactly when the question is
#: asked.
STAMP = "_refreshed_at"


def _tz_name() -> str:
    return os.getenv("FLEET_REFRESH_TZ", DEFAULT_TIMEZONE)


def _tz() -> ZoneInfo:
    return ZoneInfo(_tz_name())


def _hour() -> int:
    return int(os.getenv("FLEET_REFRESH_HOUR", DEFAULT_HOUR))


def _retry_seconds() -> float:
    return float(os.getenv("FLEET_REFRESH_RETRY_MINUTES", DEFAULT_RETRY_MINUTES)) * 60


class RefreshRejected(Exception):
    """The new fleet did not survive validation and was not written."""


@dataclass
class RefreshState:
    """What happened last time, so `/health` can answer for it."""

    enabled: bool = False
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    next_attempt: datetime | None = None
    vehicles: int = 0
    skipped: int = 0
    changes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            # The snapshot on disk, not this process: after a restart the two
            # differ, and the one that matters is how old the fleet is.
            "snapshot_taken_at": (
                taken.isoformat() if (taken := snapshot_taken_at()) else None
            ),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "next_attempt": self.next_attempt.isoformat() if self.next_attempt else None,
            "vehicles": self.vehicles,
            "skipped": self.skipped,
            "recent_changes": self.changes[:10],
        }


STATE = RefreshState()


def _fleet_path() -> Path:
    return config_mod.config_dir() / "fleet.json"


def validate(new: dict, current: dict | None) -> None:
    """Refuse a fleet that looks like a broken import rather than a real change."""
    vehicles = new.get("vehicles") or []
    if not vehicles:
        raise RefreshRejected("the import returned no vehicles at all")

    from ..domain.models import Vehicle

    for entry in vehicles:
        try:
            Vehicle.model_validate(entry)
        except Exception as exc:  # noqa: BLE001 - reported, not raised onward
            raise RefreshRejected(
                f"{entry.get('id', '?')} does not parse as a vehicle: {str(exc)[:160]}"
            ) from exc

    priceless = [v["id"] for v in vehicles if not v.get("daily_price")]
    if priceless:
        raise RefreshRejected(f"{len(priceless)} vehicles came back with no daily rate")

    if current:
        before = len(current.get("vehicles") or [])
        if before and len(vehicles) < before * MINIMUM_RETAINED:
            raise RefreshRejected(
                f"fleet fell from {before} to {len(vehicles)} vehicles — that is a "
                "broken import, not a company selling a quarter of its cars overnight"
            )


def describe_changes(new: dict, current: dict | None) -> list[str]:
    """What actually moved, so a log line says something useful."""
    if not current:
        return ["first import"]

    def by_name(fleet: dict) -> dict[str, Any]:
        return {f"{v['make']} {v['model']}": v for v in fleet.get("vehicles") or []}

    before, after = by_name(current), by_name(new)
    changes: list[str] = []
    for name in sorted(set(after) - set(before)):
        changes.append(f"added {name} at {after[name]['daily_price']}")
    for name in sorted(set(before) - set(after)):
        changes.append(f"removed {name}")
    for name in sorted(set(before) & set(after)):
        was, now = before[name].get("daily_price"), after[name].get("daily_price")
        if was != now:
            changes.append(f"{name}: {was} -> {now}")
    return changes or ["no changes"]


def refresh_once(collect: Callable[[], tuple[dict, list[str]]]) -> list[str]:
    """Import, validate, and only then replace the fleet on disk.

    The previous file is kept beside the new one. A refresh that fails leaves
    everything exactly as it was, which is the entire point.
    """
    taken = datetime.now(_tz())
    STATE.last_attempt = taken
    path = _fleet_path()
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    new, skipped = collect()
    validate(new, current)
    changes = describe_changes(new, current)

    if current is not None:
        path.with_suffix(".json.previous").write_text(
            json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    # Copied rather than mutated: the caller's document is theirs, and a test
    # that reuses one should not find a timestamp in it afterwards.
    stamped = {**new, STAMP: taken.isoformat()}
    path.write_text(json.dumps(stamped, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # The agent reads config through an lru_cache, so without this the process
    # keeps serving the fleet it started with and the refresh does nothing.
    config_mod.reload()

    STATE.last_success = taken
    STATE.last_error = None
    STATE.consecutive_failures = 0
    STATE.vehicles = len(new["vehicles"])
    STATE.skipped = len(skipped)
    STATE.changes = changes
    return changes


def attempt(collect: Callable[[], tuple[dict, list[str]]]) -> bool:
    """One refresh with every failure caught, because a bad night must not end
    the loop. Returns whether the fleet on disk was replaced."""
    try:
        changes = refresh_once(collect)
        log.info("fleet refreshed: %s", "; ".join(changes[:6]))
        return True
    except RefreshRejected as exc:
        STATE.last_error = str(exc)
        _report_failure("fleet refresh REJECTED, keeping the previous fleet: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - reported, never raised onward
        STATE.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        _report_failure("fleet refresh failed, keeping the previous fleet", trace=True)
    return False


def _report_failure(message: str, trace: bool = False) -> None:
    """Loud once, then quieter — an hourly retry against a site that is down
    should not bury the rest of the log in the same error sixteen times."""
    STATE.consecutive_failures += 1
    if STATE.consecutive_failures == 1:
        log.exception(message) if trace else log.error(message)
        return
    taken = snapshot_taken_at()
    log.warning(
        "%s [attempt %d, fleet unchanged since %s]",
        message,
        STATE.consecutive_failures,
        taken.isoformat() if taken else "unknown",
    )


def seconds_until(hour: int, timezone: str, now: datetime | None = None) -> float:
    now = now or datetime.now(ZoneInfo(timezone))
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def previous_fire(hour: int, timezone: str, now: datetime | None = None) -> datetime:
    """The most recent moment the nightly run should already have happened."""
    now = now or datetime.now(ZoneInfo(timezone))
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target > now:
        target -= timedelta(days=1)
    return target


def snapshot_taken_at() -> datetime | None:
    """When the fleet on disk was last imported, or None if nothing says.

    None covers a hand-written fleet, a fresh clone, and the first run after
    the refresh is switched on — all of which are genuinely of unknown age.
    """
    path = _fleet_path()
    if not path.exists():
        return None
    try:
        stamp = json.loads(path.read_text(encoding="utf-8")).get(STAMP)
        return datetime.fromisoformat(stamp) if stamp else None
    except (ValueError, OSError):
        return None


def next_delay(succeeded: bool, hour: int, timezone: str, now: datetime | None = None) -> float:
    """How long to wait after an attempt.

    A success waits for the next nightly run. A failure waits an hour instead —
    but never past that nightly run, which would only be the same attempt a
    moment later.
    """
    nightly = seconds_until(hour, timezone, now)
    return nightly if succeeded else min(_retry_seconds(), nightly)


def is_overdue(hour: int, timezone: str, now: datetime | None = None) -> bool:
    """Did the process miss a scheduled run while it was not running?

    Asked at boot. Comparing against the last scheduled fire rather than a flat
    age is what makes it right at both ends: booting at 03:00 with last night's
    snapshot waits the hour for the nightly run, while booting at 05:00 having
    missed 04:00 catches up immediately — the same snapshot age, opposite answers.
    """
    taken = snapshot_taken_at()
    if taken is None:
        return True
    return taken < previous_fire(hour, timezone, now)


def start(collect: Callable[[], tuple[dict, list[str]]] | None = None) -> bool:
    """Begin refreshing nightly, in the background. Returns whether it started.

    Off unless `FLEET_REFRESH=1`. A scheduled job that quietly starts editing
    configuration on a developer's laptop is not a pleasant surprise, and the
    import takes several minutes of requests against the operator's server.
    """
    if os.getenv("FLEET_REFRESH", "0") != "1":
        return False

    if collect is None:
        from .delta import collect as delta_collect

        collect = delta_collect

    hour, timezone = _hour(), _tz_name()

    def loop() -> None:
        # A missed night is caught up at boot rather than left until the next
        # one. The process not running at 04:00 — a restart, a deploy, a closed
        # laptop — is the likeliest reason a refresh never happened, and the
        # fleet would otherwise stay a day older than anybody realises.
        if is_overdue(hour, timezone):
            taken = snapshot_taken_at()
            log.info(
                "fleet snapshot is overdue (last refreshed %s) — catching up now",
                taken.isoformat() if taken else "never, or by hand",
            )
            delay = 0.0
        else:
            delay = seconds_until(hour, timezone)

        while True:
            STATE.next_attempt = datetime.now(_tz()) + timedelta(seconds=delay)
            time.sleep(delay)
            delay = next_delay(attempt(collect), hour, timezone)

    STATE.enabled = True
    threading.Thread(target=loop, name="fleet-refresh", daemon=True).start()
    log.info(
        "fleet refresh scheduled daily at %02d:00 %s, retrying every %d min on failure",
        hour,
        timezone,
        _retry_seconds() // 60,
    )
    return True
