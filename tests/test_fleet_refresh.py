"""The nightly fleet refresh, and what it does when a night goes wrong.

Two properties matter more than the schedule itself. **A bad import never
replaces good data** — an agent that has forgotten ninety cars is worse than one
quoting last week's prices, so a fleet that comes back empty, priceless or a
quarter shorter is refused and the previous file stays exactly where it was.

And **a refresh that did not happen is noticed.** The process being down at
04:00 is the likeliest reason the fleet is stale, which is precisely the case a
timer inside that process cannot see; the snapshot is stamped on disk so a
restarted process can tell, and a failure is retried within the hour rather than
left for a full day.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rental_agent import config as config_mod
from rental_agent.sources import refresh

TZ = ZoneInfo("Asia/Dubai")
DUBAI = "Asia/Dubai"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def fleet_dir(tmp_path, monkeypatch):
    """A config directory of its own, so a test never rewrites the real fleet."""
    source = json.loads((Path(__file__).parent / "fixtures/config/fleet.json").read_text())
    (tmp_path / "fleet.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
    monkeypatch.setenv("RENTAL_AGENT_CONFIG_DIR", str(tmp_path))
    config_mod.reload()
    refresh.STATE = refresh.RefreshState()
    yield tmp_path
    config_mod.reload()


def fleet_of(fleet_dir, count: int, price: int | None = None) -> dict:
    """The fixture fleet, cut to `count` vehicles and optionally re-priced."""
    data = json.loads((fleet_dir / "fleet.json").read_text())
    data["vehicles"] = [dict(v) for v in data["vehicles"][:count]]
    if price is not None:
        for vehicle in data["vehicles"]:
            vehicle["daily_price"] = price
    return data


def collector(fleet: dict):
    return lambda: (fleet, [])


# --- when the next run is ---------------------------------------------------


@pytest.mark.parametrize("hour", [0, 4, 12, 23])
def test_the_next_run_is_always_the_next_future_occurrence_of_that_hour(hour):
    now = at(1, 10, 30)
    landing = now + timedelta(seconds=refresh.seconds_until(hour, DUBAI, now))
    assert (landing.hour, landing.minute, landing.second) == (hour, 0, 0)
    assert landing > now


def test_the_schedule_does_not_drift_when_a_refresh_runs_long():
    # A run that starts at 04:00 and takes six minutes waits 23h54m, not 24h,
    # so 04:00 does not creep towards 05:00 over a month of refreshes.
    assert refresh.seconds_until(4, DUBAI, at(1, 4, 6)) == pytest.approx(23.9 * 3600, rel=0.01)


def test_previous_fire_is_yesterday_before_the_hour_and_today_after():
    assert refresh.previous_fire(4, DUBAI, at(2, 3, 0)) == at(1, 4)
    assert refresh.previous_fire(4, DUBAI, at(2, 5, 0)) == at(2, 4)


# --- catching up after the process was down ---------------------------------


def test_a_snapshot_from_last_nights_run_is_not_overdue_an_hour_before_tonights(fleet_dir):
    stamp(fleet_dir, at(1, 4))
    assert refresh.is_overdue(4, DUBAI, at(2, 3)) is False


def test_the_same_snapshot_is_overdue_once_tonights_run_has_been_missed(fleet_dir):
    # Identical snapshot age either side of 04:00, opposite answers: what makes
    # this right is that it asks "was a scheduled run missed", not "how old".
    stamp(fleet_dir, at(1, 4))
    assert refresh.is_overdue(4, DUBAI, at(2, 5)) is True


def test_a_fleet_with_no_stamp_is_treated_as_unknown_age_and_refreshed(fleet_dir):
    assert "_refreshed_at" not in json.loads((fleet_dir / "fleet.json").read_text())
    assert refresh.is_overdue(4, DUBAI, at(2, 5)) is True


def test_a_hand_written_fleet_is_not_read_as_a_crash(fleet_dir):
    (fleet_dir / "fleet.json").write_text("{not json", encoding="utf-8")
    assert refresh.snapshot_taken_at() is None


def test_a_successful_refresh_stamps_the_snapshot_so_the_next_boot_can_tell(fleet_dir):
    refresh.refresh_once(collector(fleet_of(fleet_dir, 20, price=150)))
    assert refresh.snapshot_taken_at() is not None
    assert refresh.is_overdue(4, DUBAI) is False


def test_the_stamp_is_not_written_into_the_callers_document(fleet_dir):
    new = fleet_of(fleet_dir, 20)
    refresh.refresh_once(collector(new))
    assert "_refreshed_at" not in new


# --- what a failure does ----------------------------------------------------


def test_a_failure_is_retried_within_the_hour_not_left_for_the_day():
    # 05:00, so the nightly run is 23 hours off and the retry is what decides.
    assert refresh.next_delay(False, 4, DUBAI, at(1, 5)) == 3600
    assert refresh.next_delay(True, 4, DUBAI, at(1, 5)) == 23 * 3600


def test_a_retry_never_outlives_the_nightly_run_it_would_duplicate():
    # 03:30: the nightly run is half an hour away, so the retry yields to it.
    assert refresh.next_delay(False, 4, DUBAI, at(1, 3, 30)) == 30 * 60


def test_a_rejected_import_leaves_the_previous_fleet_serving(fleet_dir):
    before = (fleet_dir / "fleet.json").read_text()
    assert refresh.attempt(collector(fleet_of(fleet_dir, 5))) is False
    assert (fleet_dir / "fleet.json").read_text() == before
    assert "broken import" in refresh.STATE.last_error


def test_a_collector_that_raises_is_caught_so_the_loop_survives_the_night(fleet_dir):
    def explode():
        raise TimeoutError("their server did not answer")

    assert refresh.attempt(explode) is False
    assert refresh.STATE.last_error.startswith("TimeoutError")


def test_consecutive_failures_are_counted_and_cleared_by_a_success(fleet_dir):
    refresh.attempt(collector(fleet_of(fleet_dir, 5)))
    refresh.attempt(collector(fleet_of(fleet_dir, 5)))
    assert refresh.STATE.consecutive_failures == 2

    assert refresh.attempt(collector(fleet_of(fleet_dir, 20, price=99))) is True
    assert refresh.STATE.consecutive_failures == 0
    assert refresh.STATE.last_error is None


# --- what validation refuses ------------------------------------------------


def test_an_empty_import_is_refused_rather_than_emptying_the_fleet(fleet_dir):
    with pytest.raises(refresh.RefreshRejected, match="no vehicles at all"):
        refresh.refresh_once(collector(fleet_of(fleet_dir, 0)))


def test_a_vehicle_that_came_back_without_a_rate_is_refused(fleet_dir):
    priceless = fleet_of(fleet_dir, 20)
    priceless["vehicles"][3]["daily_price"] = 0
    with pytest.raises(refresh.RefreshRejected, match="no daily rate"):
        refresh.refresh_once(collector(priceless))


def test_the_previous_fleet_is_kept_beside_the_new_one(fleet_dir):
    refresh.refresh_once(collector(fleet_of(fleet_dir, 20, price=333)))
    kept = json.loads((fleet_dir / "fleet.json.previous").read_text())
    assert kept["vehicles"][0]["daily_price"] == 120


def test_the_agent_serves_the_new_prices_without_a_restart(fleet_dir):
    # The config cache is why: without dropping it the process keeps serving
    # the fleet it started with and the whole refresh does nothing.
    assert config_mod.load_fleet()[1][0].daily_price == 120
    refresh.refresh_once(collector(fleet_of(fleet_dir, 20, price=175)))
    assert config_mod.load_fleet()[1][0].daily_price == 175


def stamp(fleet_dir, when: datetime) -> None:
    data = json.loads((fleet_dir / "fleet.json").read_text())
    data["_refreshed_at"] = when.isoformat()
    (fleet_dir / "fleet.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
