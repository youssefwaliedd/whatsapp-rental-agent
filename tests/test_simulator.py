"""Console smoke tests.

The scenarios are what gets shown to a rental company. They must not rot
silently when the engine or the tool layer changes underneath them.
"""

from __future__ import annotations

import pytest

from rental_agent.simulator import cli
from tests.conftest import FROZEN_NOW, REFERENCE_DATE, dt


@pytest.fixture
def console_ctx(booking_ctx):
    return booking_ctx


@pytest.mark.parametrize("index", range(1, len(cli.SCENARIOS) + 1))
def test_every_scenario_runs_and_produces_a_transcript(console_ctx, capsys, index):
    _, runner = cli.SCENARIOS[index - 1]
    runner(console_ctx)
    output = capsys.readouterr().out
    assert output.strip(), "scenario produced no output"
    assert "Traceback" not in output


def test_write_scenarios_do_not_touch_the_console_database(console_ctx, capsys):
    """Scenario 5 books a car. It must do so in a scratch database, or running
    the demo twice would fail the second time."""
    _, runner = cli.SCENARIOS[4]
    runner(console_ctx)
    capsys.readouterr()
    assert console_ctx.reservations.all() == []


def test_the_booking_scenario_shows_a_demo_reference_and_disclaimer(console_ctx, capsys):
    _, runner = cli.SCENARIOS[4]
    runner(console_ctx)
    output = capsys.readouterr().out
    assert "DEMO-1042" in output
    assert "demonstration booking" in output
    assert "replay=True" in output  # the duplicate-booking guard is demonstrated


def test_the_escalation_scenario_reports_it_is_not_yet_notifying_staff(console_ctx, capsys):
    _, runner = cli.SCENARIOS[5]
    runner(console_ctx)
    output = capsys.readouterr().out
    assert "urgent=True" in output
    assert "staff_notified=False" in output


@pytest.mark.parametrize(
    "command",
    [
        "fleet",
        "vehicle veh_13",
        "search models=G63 color=black",
        "alts veh_18",
        "quote veh_13 days=3 location=Marina",
        "discount veh_13",
        "state",
        "tools",
        "demo-fleet",
        "demo-bookings",
        "demo-conversations",
        "scenario",
    ],
)
def test_console_commands_run(console_ctx, capsys, command):
    parts = command.split()
    cli.COMMANDS[parts[0]](console_ctx, parts[1:])
    output = capsys.readouterr().out
    assert "Traceback" not in output


def test_booking_through_the_console_persists(console_ctx, capsys):
    cli.COMMANDS["book"](console_ctx, ["veh_13", "days=3", "location=Marina"])
    capsys.readouterr()
    assert len(console_ctx.reservations.all()) == 1
    assert console_ctx.reservations.all()[0].reservation_id == "DEMO-1042"


def test_next_weekday_never_returns_today(engine):
    """A demo run on a Friday must not offer delivery in the past."""
    friday = dt(4, 10)  # 4 September 2026 is a Friday
    assert friday.weekday() == 4
    assert cli.next_weekday(friday, 4, 19).day == 11
