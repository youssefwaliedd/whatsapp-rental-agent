"""Local chat simulator — a console over the engine, the tool layer and the
demo database.

No WhatsApp, no language model. This exists so the whole booking stack can be
driven and inspected by hand long before either is connected.

The scenario transcripts are *scripted*, not generated: the wording is fixed and
only the facts come from the engine and the database. They prove the stack can
supply everything the target conversation needs. The real agent replaces the
scripted wording in Milestone 3.

    python -m rental_agent.simulator.cli
"""

from __future__ import annotations

import argparse
import json
import shlex
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .. import formatting as fmt
from ..config import load_rules, reload as reload_config
from ..context import ToolContext
from ..domain.enums import Category
from ..domain.models import SearchCriteria
from ..store.db import create_db_engine, init_db, reset_db
from ..tools.registry import STATE_CHANGING, execute_tool, tool_names

#: The console always speaks as one demo customer.
CONSOLE_WHATSAPP_ID = "+971500000000"

BANNER = """
Sandline Rentals — DEMO console
Fictional fleet, fictional prices, simulated bookings. Nothing here is real.
Type /help for commands, /quit to exit.
""".strip()

HELP = """
Fleet & pricing
  /fleet [category]          List the demo fleet
  /vehicle <id>              Full vehicle detail
  /search <k=v> ...          e.g. /search models=G63 color=black days=3
  /alts <id>                 Alternatives when <id> is unavailable
  /quote <id> [k=v]          Priced demo quote (calculation only, not stored)
  /discount <id>             Maximum discount the engine permits

Booking (writes to the demo database)
  /book <id> [k=v]           Quote and reserve in one step
  /modify <ref> [k=v]        e.g. /modify DEMO-1042 pickup=2026-09-04T20:00
  /extend <ref> <iso>        Extend to a new return time
  /cancel <ref> [reason]     Cancel and release the vehicle
  /state                     The agent's stored conversation state

Raw tools (exactly what the agent will call)
  /tool <name> <json args>   e.g. /tool get_vehicle_details {"vehicle_id":"veh_13"}
  /tools                     List available tools

Scenarios
  /scenario                  List scripted demo scenarios
  /scenario <n>              Replay one

Admin
  /demo-fleet                Availability snapshot for the next 14 days
  /demo-bookings             Every demo reservation
  /demo-conversations        Conversations, stages and tool activity
  /demo-reset                Wipe the demo database and reload config
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


def window_from_args(ctx: ToolContext, args: dict[str, str]) -> tuple[datetime, datetime]:
    """Accept either explicit ISO datetimes or a simple `days=N` shorthand."""
    if "pickup" in args:
        pickup = datetime.fromisoformat(args["pickup"]).replace(tzinfo=ctx.engine.tz)
    else:
        pickup = next_weekday(ctx.now(), 4, 19)  # next Friday, 7 PM
    if "return" in args:
        ret = datetime.fromisoformat(args["return"]).replace(tzinfo=ctx.engine.tz)
    else:
        ret = pickup + timedelta(days=int(args.get("days", 3)))
    return pickup, ret


def show(result: dict) -> None:
    if "error" in result:
        print(f"  [{result['error']}] {result['message']}")
        for key in ("hint", "next_available_from", "still_missing"):
            if result.get(key):
                print(f"  {key}: {result[key]}")
        return
    print(json.dumps(result, indent=2))


# --------------------------------------------------------------------------
# Read commands
# --------------------------------------------------------------------------


def cmd_fleet(ctx: ToolContext, parts: list[str]) -> None:
    wanted = parts[0] if parts else None
    for vehicle in ctx.engine.list_fleet():
        if wanted and vehicle.category.value != wanted:
            continue
        flag = "" if vehicle.status.value == "active" else f"  [{vehicle.status.value}]"
        print(
            f"  {vehicle.id}  {vehicle.display_name:<34} {vehicle.category.value:<13}"
            f" {vehicle.color:<7} {fmt.amount(vehicle.daily_price):>10}/day{flag}"
        )


def cmd_vehicle(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /vehicle <id>")
        return
    show(execute_tool(ctx, "get_vehicle_details", {"vehicle_id": parts[0]}))


def cmd_search(ctx: ToolContext, parts: list[str]) -> None:
    args = parse_kv(parts)
    pickup, ret = window_from_args(ctx, args)
    criteria = SearchCriteria(
        pickup_at=pickup,
        return_at=ret,
        models=[args["models"]] if "models" in args else None,
        makes=[args["makes"]] if "makes" in args else None,
        categories=[Category(args["category"])] if "category" in args else None,
        color=args.get("color"),
        max_daily_price=Decimal(args["budget"]) if "budget" in args else None,
        min_passenger_capacity=int(args["seats"]) if "seats" in args else None,
        driver_age=int(args["age"]) if "age" in args else None,
        delivery_location=args.get("location"),
    )
    matches = ctx.engine.search(criteria)
    print(f"  {fmt.when(pickup)} → {fmt.when(ret)}")
    if not matches:
        print("  no vehicles matched — the agent would call find_alternatives here")
        return
    print()
    print(fmt.option_list(matches))


def cmd_alts(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /alts <vehicle_id>")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(ctx, args)
    matches = ctx.engine.find_alternatives(parts[0], pickup, ret)
    if not matches:
        print("  no alternatives — this is an escalation case, not a dead end")
        return
    for match in matches:
        print(f"  {match.vehicle.id}  {match.vehicle.display_name:<34} {match.match_reasons[0]}")


def cmd_quote(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /quote <vehicle_id> [days=3] [location=Marina] [discount=10]")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(ctx, args)
    result = execute_tool(
        ctx,
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
        show(result)
        return
    quote = ctx.engine.calculate_quote(
        vehicle_id=parts[0],
        pickup_at=pickup,
        return_at=ret,
        delivery_location=args.get("location"),
        discount_percent=Decimal(args["discount"]) if "discount" in args else Decimal("0"),
        excess_reduction=args.get("excess") == "yes",
    )
    print()
    print(fmt.quote_message(quote, ctx.engine.rules))


def cmd_discount(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /discount <vehicle_id> [days=3]")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(ctx, args)
    show(
        execute_tool(
            ctx,
            "get_allowed_discount",
            {
                "vehicle_id": parts[0],
                "pickup_at": pickup.isoformat(),
                "return_at": ret.isoformat(),
            },
        )
    )


# --------------------------------------------------------------------------
# Write commands
# --------------------------------------------------------------------------


def cmd_book(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /book <vehicle_id> [days=3] [location=Marina]")
        return
    args = parse_kv(parts[1:])
    pickup, ret = window_from_args(ctx, args)

    quote = execute_tool(
        ctx,
        "create_demo_quote",
        {
            "vehicle_id": parts[0],
            "pickup_at": pickup.isoformat(),
            "return_at": ret.isoformat(),
            "delivery_location": args.get("location", "Dubai Marina"),
        },
    )
    if "error" in quote:
        show(quote)
        return
    show(execute_tool(ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}))


def cmd_modify(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /modify <DEMO-ref> [pickup=<iso>] [return=<iso>] [location=...]")
        return
    args = parse_kv(parts[1:])
    payload: dict = {"reservation_id": parts[0]}
    if "pickup" in args:
        payload["pickup_at"] = args["pickup"]
    if "return" in args:
        payload["return_at"] = args["return"]
    if "location" in args:
        payload["delivery_location"] = args["location"]
    show(execute_tool(ctx, "modify_demo_reservation", payload))


def cmd_extend(ctx: ToolContext, parts: list[str]) -> None:
    if len(parts) < 2:
        print("  usage: /extend <DEMO-ref> <new-return-iso>")
        return
    show(
        execute_tool(
            ctx, "extend_demo_rental", {"reservation_id": parts[0], "new_return_at": parts[1]}
        )
    )


def cmd_cancel(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /cancel <DEMO-ref> [reason]")
        return
    show(
        execute_tool(
            ctx,
            "cancel_demo_reservation",
            {"reservation_id": parts[0], "reason": " ".join(parts[1:]) or None},
        )
    )


def cmd_state(ctx: ToolContext, parts: list[str]) -> None:
    state = ctx.load_state()
    print(json.dumps(state.model_dump(mode="json"), indent=2))
    missing = state.missing_requirements()
    print(f"\n  still needed before searching: {missing or 'nothing'}")


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------


def cmd_demo_fleet(ctx: ToolContext, parts: list[str]) -> None:
    """Fourteen-day availability grid, including live demo bookings."""
    engine = ctx.engine
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
    print("\n  . free   x unavailable (seeded block or live demo booking)")


def cmd_demo_bookings(ctx: ToolContext, parts: list[str]) -> None:
    reservations = ctx.reservations.all()
    if not reservations:
        print("  no demo reservations yet")
        return
    for r in reservations:
        vehicle = ctx.engine.get_vehicle(r.vehicle_id)
        print(
            f"  {r.reservation_id:<11} {r.status:<10} {vehicle.display_name:<32}"
            f" {r.pickup_at:%d %b %H:%M} → {r.return_at:%d %b %H:%M}"
            f" {fmt.amount(r.total_charge):>12}  pay:{r.payment_status}"
        )
        if len(r.history) > 1:
            for entry in r.history[1:]:
                print(f"                 ↳ {entry['event']} at {entry['at'][:16]}")
    print(f"\n  {len(reservations)} demo reservation(s) — none of them real")


def cmd_demo_conversations(ctx: ToolContext, parts: list[str]) -> None:
    conversations = ctx.conversations.all()
    if not conversations:
        print("  no conversations yet")
        return
    for conversation in conversations:
        customer = ctx.customers.get(conversation.customer_id)
        calls = ctx.tool_calls.for_conversation(conversation.conversation_id)
        messages = ctx.messages.for_conversation(conversation.conversation_id)
        flag = "  ESCALATED" if conversation.escalated else ""
        print(
            f"  {conversation.conversation_id}  {customer.whatsapp_id:<15}"
            f" stage={conversation.stage:<22} msgs={len(messages):<3}"
            f" tools={len(calls)}{flag}"
        )
        failures = [c for c in calls if c.status == "error"]
        if failures:
            print(f"      tool failures: {[c.error_code for c in failures]}")


def perform_reset(ctx: ToolContext, db_path: str | None) -> None:
    """Wipe the demo database and reload config, then re-establish the console
    customer so the next command has somewhere to write."""
    reload_config()
    ctx.session.close()
    factory = reset_db(create_db_engine(db_path))
    ctx.session = factory()
    ctx._engine_cache.clear()
    customer, _ = ctx.customers.get_or_create(CONSOLE_WHATSAPP_ID, ctx.now())
    conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
    ctx.customer_id = customer.customer_id
    ctx.conversation_id = conversation.conversation_id
    ctx.session.commit()
    print("  demo database wiped and config reloaded")


# --------------------------------------------------------------------------
# Scripted scenarios
# --------------------------------------------------------------------------


def _turn(speaker: str, text: str) -> None:
    prefix = "Customer" if speaker == "c" else "Agent   "
    for i, line in enumerate(text.split("\n")):
        print(f"  {prefix if i == 0 else '        '} │ {line}")
    print()


def _note(text: str) -> None:
    print(f"  · {text}\n")


def scenario_available(ctx: ToolContext) -> None:
    """1. The requested vehicle is available."""
    pickup = next_weekday(ctx.now(), 4, 19)
    ret = pickup + timedelta(days=3)

    _turn("c", "I need a black G63 this weekend")
    _turn("a", "I can help with that. What time would you like it delivered on Friday?")
    _turn("c", "About 7 PM in Marina")
    _turn("a", "Perfect — Dubai Marina at 7 PM on Friday. When would you like to return it?")
    _turn("c", "Monday evening, same time")

    matches = ctx.engine.search(
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
    quote = ctx.engine.calculate_quote(
        vehicle_id=matches[0].vehicle.id,
        pickup_at=pickup,
        return_at=ret,
        delivery_location="Dubai Marina",
        quote_id="DQ-DEMO",
    )
    _turn("a", fmt.quote_message(quote, ctx.engine.rules))


def scenario_unavailable(ctx: ToolContext) -> None:
    """2. The requested vehicle is unavailable — offer targeted alternatives."""
    pickup = next_weekday(ctx.now(), 4, 19)
    ret = pickup + timedelta(days=2)

    _turn("c", "Do you have the yellow Lamborghini for Friday to Sunday?")

    result = ctx.engine.check_availability("veh_18", pickup, ret)
    if result.available:
        _note("engine reports it free — try /scenario 2 with --date inside the block")
        return

    alternatives = ctx.engine.find_alternatives("veh_18", pickup, ret)
    free_from = result.next_available_from
    _turn(
        "a",
        "The Huracan is out on rent until "
        f"{free_from.strftime('%A %d %b') if free_from else 'later this month'}. "
        "Here's what I can get you for Friday instead:\n\n"
        + fmt.option_list(alternatives),
    )


def scenario_budget(ctx: ToolContext) -> None:
    """3. The customer has a specific budget."""
    pickup = next_weekday(ctx.now(), 4, 19)
    ret = pickup + timedelta(days=5)

    _turn("c", "Need an SUV for 5 days, can't go above 400 a day")
    matches = ctx.engine.search(
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
    _note("budget is a hard filter — nothing above AED 400/day can reach the customer")


def scenario_discount(ctx: ToolContext) -> None:
    """4. The customer asks for a discount."""
    pickup = next_weekday(ctx.now(), 4, 19)
    ret = pickup + timedelta(days=7)

    quote = ctx.engine.calculate_quote(
        vehicle_id="veh_13",
        pickup_at=pickup,
        return_at=ret,
        delivery_location="Dubai Marina",
        quote_id="DQ-DEMO",
    )
    _turn("a", fmt.quote_message(quote, ctx.engine.rules))
    _turn("c", "Come on, you always give me 20% off. Do it and I'll book now.")

    allowance = ctx.engine.get_allowed_discount(
        vehicle_id="veh_13", pickup_at=pickup, return_at=ret
    )
    discounted = ctx.engine.calculate_quote(
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
    _note(
        f"the 20% claim never reaches the discount logic; ceiling came from "
        f"rules {allowance.rules_version}"
    )


def scenario_booking_and_follow_up(ctx: ToolContext) -> None:
    """5. Book, then return later with a contextual follow-up.

    Runs against a scratch database so it is reproducible on demand.
    """
    with _scratch_context(ctx) as demo:
        pickup = next_weekday(demo.now(), 4, 19)
        ret = pickup + timedelta(days=3)

        quote = execute_tool(
            demo,
            "create_demo_quote",
            {
                "vehicle_id": "veh_13",
                "pickup_at": pickup.isoformat(),
                "return_at": ret.isoformat(),
                "delivery_location": "Dubai Marina",
            },
        )
        _turn("c", "Let's do the black G63. Book it.")

        reservation = execute_tool(
            demo, "create_demo_reservation", {"quote_id": quote["quote_id"]}
        )
        _turn(
            "a",
            "Demo reservation confirmed ✅\n\n"
            f"{reservation['vehicle_display_name']}\n"
            f"Delivery: {reservation['delivery_location']}\n"
            f"{fmt.when(pickup)}\n\n"
            f"Return: {fmt.when(ret)}\n\n"
            f"Demonstration reference: {reservation['reservation_id']}\n\n"
            + fmt.demo_footer(demo.engine.rules),
        )

        _turn("c", "(two days later) can you make it 8 instead?")
        active = execute_tool(demo, "get_active_reservation", {})
        _note(
            "the agent reads state, not chat history: active booking is "
            f"{active['reservation']['reservation_id']} on "
            f"{active['reservation']['vehicle_display_name']} — no need to ask which car"
        )

        moved = execute_tool(
            demo,
            "modify_demo_reservation",
            {
                "reservation_id": active["reservation"]["reservation_id"],
                "pickup_at": pickup.replace(hour=20).isoformat(),
            },
        )
        _turn(
            "a",
            f"Done — delivery moved to {fmt.when(pickup.replace(hour=20))}. "
            f"Everything else stays the same.\n\n" + fmt.demo_footer(demo.engine.rules),
        )
        _note(f"changed fields: {moved['changed']}; price difference {moved['price_difference']}")

        duplicate = execute_tool(
            demo,
            "create_demo_reservation",
            {"quote_id": quote["quote_id"]},
        )
        _note(
            "a duplicate booking call returns the same reservation "
            f"({duplicate['reservation_id']}, replay={duplicate.get('idempotent_replay')}) — "
            "one reservation exists, not two"
        )


def scenario_escalation(ctx: ToolContext) -> None:
    """8. The customer reports an accident and is escalated."""
    with _scratch_context(ctx) as demo:
        _turn("c", "I've had an accident on Sheikh Zayed Road, the front is damaged")

        result = execute_tool(
            demo,
            "escalate_conversation",
            {"reason": "accident", "detail": "front-end damage, Sheikh Zayed Road"},
        )
        _turn(
            "a",
            "I'm sorry — are you and your passengers safe?\n\n"
            "Please move to a safe place and call 999 if anyone is injured. "
            "You'll need a police report before the car can be moved.\n\n"
            "I've passed this to a colleague who is taking over now.",
        )
        _note(
            f"escalation recorded: reason={result['reason']}, urgent={result['urgent']}, "
            f"stage now {demo.load_state().stage.value}; the agent stops selling"
        )
        _note("staff WhatsApp notification is wired in Milestone 4 (staff_notified=False)")


class _scratch_context:
    """A throwaway database so write scenarios are reproducible on demand."""

    def __init__(self, template: ToolContext):
        self._template = template
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> ToolContext:
        factory = init_db(create_db_engine(Path(self._tmp.name) / "scenario.db"))
        self._session = factory()
        ctx = ToolContext(
            session=self._session,
            now_fn=self._template.now_fn,
            reference_date=self._template.reference_date,
        )
        customer, _ = ctx.customers.get_or_create("+971500000123", ctx.now())
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
        ctx.customer_id = customer.customer_id
        ctx.conversation_id = conversation.conversation_id
        return ctx

    def __exit__(self, *exc) -> None:
        self._session.close()
        self._tmp.cleanup()


SCENARIOS: list[tuple[str, Callable[[ToolContext], None]]] = [
    ("Requested vehicle is available", scenario_available),
    ("Requested vehicle is unavailable → alternatives", scenario_unavailable),
    ("Customer has a budget", scenario_budget),
    ("Customer asks for a discount", scenario_discount),
    ("Booking, then a contextual follow-up two days later", scenario_booking_and_follow_up),
    ("Accident reported → escalation", scenario_escalation),
]


def cmd_scenario(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        for index, (title, _) in enumerate(SCENARIOS, start=1):
            print(f"  {index}. {title}")
        print("\n  Scenarios for date changes and extensions arrive with the agent"
              " in Milestone 3.")
        return
    try:
        title, runner = SCENARIOS[int(parts[0]) - 1]
    except (ValueError, IndexError):
        print("  no such scenario")
        return
    print(f"\n  ── {title} ──\n")
    runner(ctx)


def cmd_tool(ctx: ToolContext, parts: list[str]) -> None:
    if not parts:
        print("  usage: /tool <name> <json>")
        return
    raw = " ".join(parts[1:]) or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  bad JSON: {exc}")
        return
    show(execute_tool(ctx, parts[0], args))


def cmd_tools(ctx: ToolContext, parts: list[str]) -> None:
    for name in tool_names():
        marker = "  (writes)" if name in STATE_CHANGING else ""
        print(f"  {name}{marker}")


COMMANDS: dict[str, Callable[[ToolContext, list[str]], None]] = {
    "fleet": cmd_fleet,
    "vehicle": cmd_vehicle,
    "search": cmd_search,
    "alts": cmd_alts,
    "quote": cmd_quote,
    "discount": cmd_discount,
    "book": cmd_book,
    "modify": cmd_modify,
    "extend": cmd_extend,
    "cancel": cmd_cancel,
    "state": cmd_state,
    "tool": cmd_tool,
    "tools": cmd_tools,
    "scenario": cmd_scenario,
    "demo-fleet": cmd_demo_fleet,
    "demo-bookings": cmd_demo_bookings,
    "demo-conversations": cmd_demo_conversations,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_context(reference: str | None, db_path: str | None) -> tuple[ToolContext, object]:
    factory = init_db(create_db_engine(db_path))
    session = factory()

    now_fn = None
    ref_date = None
    if reference:
        ref_date = date.fromisoformat(reference)
        tz = ZoneInfo(load_rules().timezone)
        frozen = datetime.combine(ref_date, datetime.min.time(), tzinfo=tz).replace(hour=10)
        now_fn = lambda: frozen  # noqa: E731

    ctx = ToolContext(session=session, now_fn=now_fn, reference_date=ref_date)
    customer, _ = ctx.customers.get_or_create(CONSOLE_WHATSAPP_ID, ctx.now())
    conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
    ctx.customer_id = customer.customer_id
    ctx.conversation_id = conversation.conversation_id
    session.commit()
    return ctx, factory


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo rental console")
    parser.add_argument(
        "--date", help="Freeze the clock at this date (YYYY-MM-DD) for reproducible demos"
    )
    parser.add_argument("--db", help="Path to the demo database (default: ./demo.db)")
    parser.add_argument("--run", help="Run one command and exit, e.g. --run 'scenario 1'")
    args = parser.parse_args()

    ctx, _factory = build_context(args.date, args.db)

    def dispatch(line: str) -> None:
        parts = shlex.split(line)
        if parts[0] == "demo-reset":
            perform_reset(ctx, args.db)
            return
        handler = COMMANDS.get(parts[0])
        if handler is None:
            print(f"  unknown command '{parts[0]}' — /help for the list")
            return
        try:
            handler(ctx, parts[1:])
            ctx.session.commit()
        except Exception as exc:  # noqa: BLE001 - a console should not die on a typo
            ctx.session.rollback()
            print(f"  error: {type(exc).__name__}: {exc}")

    if args.run:
        dispatch(args.run)
        return

    print(BANNER)
    print(f"\n  Today (engine): {ctx.engine.reference_date}   Customer: {CONSOLE_WHATSAPP_ID}\n")
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
