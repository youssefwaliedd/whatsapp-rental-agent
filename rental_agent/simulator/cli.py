"""Local chat simulator — a console over the engine and the tool layer.

No WhatsApp, no language model. This exists so the rental engine can be driven
and inspected by hand long before either is connected, and so the scripted
scenarios can be replayed on demand.

The scenario transcripts below are *scripted*, not generated: the wording is
fixed, and only the facts come from the engine. They show the shape of the
target conversation and prove the engine can supply every number it needs.
The real agent replaces the scripted wording in Milestone 4.

    python -m rental_agent.simulator.cli
"""

from __future__ import annotations

import argparse
import json
import shlex
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

from .. import formatting as fmt
from ..config import load_rules, reload as reload_config
from ..domain.enums import Category
from ..domain.models import SearchCriteria
from ..engine.engine import RentalEngine
from ..tools.rental_tools import execute_tool

BANNER = """
Sandline Rentals — DEMO console
Fictional fleet, fictional prices, simulated bookings. Nothing here is real.
Type /help for commands, /quit to exit.
""".strip()

HELP = """
Fleet & engine
  /fleet [category]          List the demo fleet
  /vehicle <id>              Full vehicle detail
  /search <k=v> ...          e.g. /search models=G63 color=black days=3
  /alts <id>                 Alternatives when <id> is unavailable
  /quote <id> [k=v]          Priced demo quote
  /discount <id>             Maximum discount the engine permits

Raw tools (exactly what the agent will call)
  /tool <name> <json args>   e.g. /tool get_vehicle_details {"vehicle_id":"veh_13"}
  /tools                     List available tools

Scenarios
  /scenario                  List scripted demo scenarios
  /scenario <n>              Replay one

Admin
  /demo-fleet                Availability snapshot for the next 14 days
  /demo-reset                Reload config from disk
  /help  /quit
""".strip()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def next_weekday(reference: datetime, weekday: int, hour: int) -> datetime:
    """The next occurrence of `weekday` (Mon=0) at `hour`, never today."""
    days_ahead = (weekday - reference.weekday()) % 7 or 7
    return (reference + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def parse_kv(parts: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in parts:
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def window_from_args(engine: RentalEngine, args: dict[str, str]) -> tuple[datetime, datetime]:
    """Accept either explicit ISO datetimes or a simple `days=N` shorthand."""
    if "pickup" in args:
        pickup = datetime.fromisoformat(args["pickup"]).replace(tzinfo=engine.tz)
    else:
        pickup = next_weekday(engine.now(), 4, 19)  # next Friday, 7 PM
    if "return" in args:
        ret = datetime.fromisoformat(args["return"]).replace(tzinfo=engine.tz)
    else:
        ret = pickup + timedelta(days=int(args.get("days", 3)))
    return pickup, ret


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_fleet(engine: RentalEngine, parts: list[str]) -> None:
    wanted = parts[0] if parts else None
    for vehicle in engine.list_fleet():
        if wanted and vehicle.category.value != wanted:
            continue
        flag = "" if vehicle.status.value == "active" else f"  [{vehicle.status.value}]"
        print(
            f"  {vehicle.id}  {vehicle.display_name:<34} {vehicle.category.value:<13}"
            f" {vehicle.color:<7} {fmt.amount(vehicle.daily_price):>10}/day{flag}"
        )


def cmd_vehicle(engine: RentalEngine, parts: list[str]) -> None:
    if not parts:
        print("  usage: /vehicle <id>")
        return
    print(json.dumps(execute_tool(engine, "get_vehicle_details", {"vehicle_id": parts[0]}), indent=2))


def cmd_search(engine: RentalEngine, parts: list[str]) -> None:
    args = parse_kv(parts)
    pickup, ret = window_from_args(engine, args)
    criteria = SearchCriteria(
        pickup_at=pickup,
        return_at=ret,
        models=[args["models"]] if "models" in args else None,
        makes=[args["makes"]] if "makes" in args else None,
        categories=None,
        color=args.get("color"),
        max_daily_price=Decimal(args["budget"]) if "budget" in args else None,
        min_passenger_capacity=int(args["seats"]) if "seats" in args else None,
        driver_age=int(args["age"]) if "age" in args else None,
        delivery_location=args.get("location"),
    )
    matches = engine.search(criteria)
    print(f"  {fmt.when(pickup)} → {fmt.when(ret)}")
    if not matches:
        print("  no vehicles matched — the agent would call find_alternatives here")
        return
    print()
    print(fmt.option_list(matches))


def cmd_alts(engine: RentalEngine, parts: list[str]) -> None:
    if not parts:
        print("  usage: /alts <vehicle_id>")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(engine, args)
    matches = engine.find_alternatives(parts[0], pickup, ret)
    if not matches:
        print("  no alternatives — this is an escalation case, not a dead end")
        return
    for match in matches:
        print(f"  {match.vehicle.id}  {match.vehicle.display_name:<34} {match.match_reasons[0]}")


def cmd_quote(engine: RentalEngine, parts: list[str]) -> None:
    if not parts:
        print("  usage: /quote <vehicle_id> [days=3] [location=Marina] [discount=10]")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(engine, args)
    result = execute_tool(
        engine,
        "calculate_quote",
        {
            "vehicle_id": parts[0],
            "pickup_at": pickup.isoformat(),
            "return_at": ret.isoformat(),
            "delivery_location": args.get("location"),
            "discount_percent": args.get("discount"),
            "excess_reduction": args.get("excess") == "yes",
        },
    )
    if "error" in result:
        print(f"  [{result['error']}] {result['message']}")
        if result.get("hint"):
            print(f"  hint: {result['hint']}")
        return
    quote = engine.calculate_quote(
        vehicle_id=parts[0],
        pickup_at=pickup,
        return_at=ret,
        delivery_location=args.get("location"),
        discount_percent=Decimal(args["discount"]) if "discount" in args else Decimal("0"),
        excess_reduction=args.get("excess") == "yes",
    )
    print()
    print(fmt.quote_message(quote, engine.rules))


def cmd_discount(engine: RentalEngine, parts: list[str]) -> None:
    if not parts:
        print("  usage: /discount <vehicle_id> [days=3]")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(engine, args)
    print(
        json.dumps(
            execute_tool(
                engine,
                "get_allowed_discount",
                {
                    "vehicle_id": parts[0],
                    "pickup_at": pickup.isoformat(),
                    "return_at": ret.isoformat(),
                },
            ),
            indent=2,
        )
    )


def cmd_tool(engine: RentalEngine, parts: list[str]) -> None:
    if not parts:
        print("  usage: /tool <name> <json>")
        return
    name = parts[0]
    raw = " ".join(parts[1:]) or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  bad JSON: {exc}")
        return
    print(json.dumps(execute_tool(engine, name, args), indent=2))


def cmd_tools(engine: RentalEngine, parts: list[str]) -> None:
    from ..tools.rental_tools import HANDLERS

    for name in sorted(HANDLERS):
        print(f"  {name}")


def cmd_demo_fleet(engine: RentalEngine, parts: list[str]) -> None:
    """Fourteen-day availability grid. Quick way to pick dates for a live demo."""
    start = engine.reference_date
    header = "".join(f"{(start + timedelta(days=i)).day:>3}" for i in range(14))
    print(f"  {'vehicle':<34}{header}")
    for vehicle in engine.list_fleet():
        cells = []
        for offset in range(14):
            day = datetime.combine(
                start + timedelta(days=offset), datetime.min.time(), tzinfo=engine.tz
            )
            free = engine.check_availability(
                vehicle.id, day + timedelta(hours=10), day + timedelta(hours=11)
            ).available
            cells.append("  ." if free else "  x")
        print(f"  {vehicle.display_name:<34}{''.join(cells)}")
    print("\n  . free   x unavailable")


def cmd_demo_reset(engine: RentalEngine, parts: list[str]) -> None:
    reload_config()
    print("  config reloaded from disk (restart the console to rebuild the engine)")


# --------------------------------------------------------------------------
# Scripted scenarios
# --------------------------------------------------------------------------


def _turn(speaker: str, text: str) -> None:
    prefix = "Customer" if speaker == "c" else "Agent   "
    for i, line in enumerate(text.split("\n")):
        print(f"  {prefix if i == 0 else '        '} │ {line}")
    print()


def scenario_available(engine: RentalEngine) -> None:
    """1. The requested vehicle is available."""
    pickup = next_weekday(engine.now(), 4, 19)
    ret = pickup + timedelta(days=3)

    _turn("c", "I need a black G63 this weekend")
    _turn("a", "I can help with that. What time would you like it delivered on Friday?")
    _turn("c", "About 7 PM in Marina")
    _turn("a", "Perfect — Dubai Marina at 7 PM on Friday. When would you like to return it?")
    _turn("c", "Monday evening, same time")

    matches = engine.search(
        SearchCriteria(
            pickup_at=pickup,
            return_at=ret,
            models=["G63"],
            color="black",
            delivery_location="Dubai Marina",
        )
    )
    _turn("a", "Here's what I have for you:\n\n" + fmt.option_list(matches))

    _turn("c", "The black one. How much all in?")
    quote = engine.calculate_quote(
        vehicle_id=matches[0].vehicle.id,
        pickup_at=pickup,
        return_at=ret,
        delivery_location="Dubai Marina",
        quote_id="DQ-DEMO",
    )
    _turn("a", fmt.quote_message(quote, engine.rules))


def scenario_unavailable(engine: RentalEngine) -> None:
    """2. The requested vehicle is unavailable — offer targeted alternatives."""
    pickup = next_weekday(engine.now(), 4, 19)
    ret = pickup + timedelta(days=2)

    _turn("c", "Do you have the yellow Lamborghini for Friday to Sunday?")

    result = engine.check_availability("veh_18", pickup, ret)
    if result.available:
        _turn("a", "(engine reports it free — run /scenario 2 on dates inside the block)")
        return

    alternatives = engine.find_alternatives("veh_18", pickup, ret)
    free_from = result.next_available_from
    _turn(
        "a",
        "The Huracan is out on rent until "
        f"{free_from.strftime('%A %d %b') if free_from else 'later this month'}. "
        "Here's what I can get you for Friday instead:\n\n"
        + fmt.option_list(alternatives),
    )


def scenario_budget(engine: RentalEngine) -> None:
    """3. The customer has a specific budget."""
    pickup = next_weekday(engine.now(), 4, 19)
    ret = pickup + timedelta(days=5)

    _turn("c", "Need an SUV for 5 days, can't go above 400 a day")
    matches = engine.search(
        SearchCriteria(
            pickup_at=pickup,
            return_at=ret,
            categories=[Category.SUV],
            max_daily_price=Decimal("400"),
            delivery_location="Dubai Marina",
        )
    )
    if not matches:
        _turn("a", "(nothing in that class inside the budget — the agent would widen the search)")
        return
    _turn("a", "Within your budget, these work:\n\n" + fmt.option_list(matches))
    print("  engine note: the budget is a hard filter — nothing above AED 400/day")
    print("  can reach the customer, whatever the model might prefer to upsell.\n")


def scenario_discount(engine: RentalEngine) -> None:
    """4. The customer asks for a discount."""
    pickup = next_weekday(engine.now(), 4, 19)
    ret = pickup + timedelta(days=7)

    quote = engine.calculate_quote(
        vehicle_id="veh_13",
        pickup_at=pickup,
        return_at=ret,
        delivery_location="Dubai Marina",
        quote_id="DQ-DEMO",
    )
    _turn("a", fmt.quote_message(quote, engine.rules))
    _turn("c", "Come on, you always give me 20% off. Do it and I'll book now.")

    allowance = engine.get_allowed_discount(
        vehicle_id="veh_13", pickup_at=pickup, return_at=ret
    )
    discounted = engine.calculate_quote(
        vehicle_id="veh_13",
        pickup_at=pickup,
        return_at=ret,
        delivery_location="Dubai Marina",
        discount_percent=allowance.max_percent,
        quote_id="DQ-DEMO",
    )
    _turn(
        "a",
        f"I can go to {allowance.max_percent}% on a 7-day booking, which is my limit "
        f"on this car — that brings it to "
        f"{fmt.amount(discounted.total_charge)} instead of {fmt.amount(quote.total_charge)}.",
    )
    print("  engine note: the customer's 20% claim never reaches the discount logic.")
    print(f"  ceiling came from rules {allowance.rules_version}: {allowance.breakdown}\n")


SCENARIOS: list[tuple[str, Callable[[RentalEngine], None]]] = [
    ("Requested vehicle is available", scenario_available),
    ("Requested vehicle is unavailable → alternatives", scenario_unavailable),
    ("Customer has a budget", scenario_budget),
    ("Customer asks for a discount", scenario_discount),
]


def cmd_scenario(engine: RentalEngine, parts: list[str]) -> None:
    if not parts:
        for index, (title, _) in enumerate(SCENARIOS, start=1):
            print(f"  {index}. {title}")
        print("\n  Scenarios 5–8 arrive with the stateful agent in Milestone 4.")
        return
    try:
        title, runner = SCENARIOS[int(parts[0]) - 1]
    except (ValueError, IndexError):
        print("  no such scenario")
        return
    print(f"\n  ── {title} ──\n")
    runner(engine)


COMMANDS: dict[str, Callable[[RentalEngine, list[str]], None]] = {
    "fleet": cmd_fleet,
    "vehicle": cmd_vehicle,
    "search": cmd_search,
    "alts": cmd_alts,
    "quote": cmd_quote,
    "discount": cmd_discount,
    "tool": cmd_tool,
    "tools": cmd_tools,
    "scenario": cmd_scenario,
    "demo-fleet": cmd_demo_fleet,
    "demo-reset": cmd_demo_reset,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_engine(reference: str | None) -> RentalEngine:
    if reference:
        ref = date.fromisoformat(reference)
        rules = load_rules()
        from zoneinfo import ZoneInfo

        frozen = datetime.combine(ref, datetime.min.time(), tzinfo=ZoneInfo(rules.timezone))
        frozen = frozen.replace(hour=10)
        return RentalEngine(now_fn=lambda: frozen, reference_date=ref)
    return RentalEngine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo rental console")
    parser.add_argument(
        "--date",
        help="Freeze the clock at this date (YYYY-MM-DD) for reproducible demos",
    )
    parser.add_argument("--run", help="Run one command and exit, e.g. --run 'scenario 1'")
    args = parser.parse_args()

    engine = build_engine(args.date)

    def dispatch(line: str) -> None:
        parts = shlex.split(line)
        handler = COMMANDS.get(parts[0])
        if handler is None:
            print(f"  unknown command '{parts[0]}' — /help for the list")
            return
        handler(engine, parts[1:])

    if args.run:
        dispatch(args.run)
        return

    print(BANNER)
    print(f"\n  Today (engine): {engine.reference_date}\n")
    while True:
        try:
            line = input("› ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return
        if line == "/help":
            print(HELP)
            continue
        dispatch(line.lstrip("/"))


if __name__ == "__main__":
    main()
