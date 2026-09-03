"""The booking journey, including the ways it goes wrong.

    .venv/bin/python run_booking_demo.py

Runs against a throwaway database so it starts clean and says the same thing
every time. No model, no network, no quota — this exercises the booking
connector, not the conversation.

Five scenes, each one a thing that will happen in production and that a demo
which only ever succeeds would never show you:

  1. a booking that works
  2. the phone rings between the availability check and the booking
  3. two customers confirming the same car at the same moment
  4. the booking system answering, then not answering
  5. somebody else's reference, used by the wrong customer
"""

from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from rental_agent.booking_provider import Outcome
from rental_agent.context import ToolContext
from rental_agent.store.db import create_db_engine, init_db

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GOOD, WARN, BAD = "\033[38;5;114m", "\033[38;5;179m", "\033[38;5;167m"


def scene(number: int, title: str) -> None:
    print(f"\n{BOLD}── {number}. {title} {'─' * max(0, 54 - len(title))}{OFF}")


def say(label: str, text: str, colour: str = "") -> None:
    print(f"   {colour}{label:<12}{OFF}{text}")


def customer(session, handle: str) -> ToolContext:
    ctx = ToolContext(session=session)
    who, _ = ctx.customers.get_or_create(handle, ctx.now())
    conversation, _ = ctx.conversations.get_or_create(who.customer_id, ctx.now())
    ctx.customer_id, ctx.conversation_id = who.customer_id, conversation.conversation_id
    session.flush()
    return ctx


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        factory = init_db(create_db_engine(Path(tmp) / "demo.db"))
        with factory() as session:
            ali = customer(session, "+971500000101")
            provider = ali.provider
            fleet = ali.engine.list_fleet()[0]
            pickup = ali.now() + timedelta(days=30)
            ret = pickup + timedelta(days=2)

            print(f"{DIM}provider: {provider.name} · authoritative: "
                  f"{provider.authoritative} · demonstration: {provider.is_demonstration}{OFF}")
            print(f"{DIM}car:      {fleet.display_name} ({fleet.id}){OFF}")
            print(f"{DIM}window:   {pickup:%a %d %b %H:%M} → {ret:%a %d %b %H:%M}{OFF}")

            # 1 ---------------------------------------------------------------
            scene(1, "a booking that works")
            free = provider.check_availability(fleet.id, pickup, ret)
            say("available", str(free.available), GOOD)
            quote = provider.quote(vehicle_id=fleet.id, pickup_at=pickup, return_at=ret)
            say("quoted", f"{quote.currency} {quote.total_charge}  ({quote.quote_id})")
            booked = provider.reserve(
                quote_id=quote.quote_id, customer_ref=ali.customer_id,
                idempotency_key="demo-ali-1",
            )
            say("outcome", booked.outcome.value, GOOD)
            say("reference", f"{booked.reference}   demonstration={booked.is_demonstration}")

            # 2 ---------------------------------------------------------------
            scene(2, "the phone rings between the check and the booking")
            second = ali.engine.list_fleet()[1]
            noor = customer(session, "+971500000102")
            free = noor.provider.check_availability(second.id, pickup, ret)
            say("available", f"{free.available}   {DIM}(Noor is told it is free){OFF}", GOOD)
            quote2 = noor.provider.quote(vehicle_id=second.id, pickup_at=pickup, return_at=ret)
            say("quoted", f"{quote2.currency} {quote2.total_charge}")

            taken = provider.record_external_booking(
                vehicle_id=second.id, pickup_at=pickup, return_at=ret, who="phone"
            )
            say("meanwhile", f"{DIM}someone books {second.id} by phone → "
                             f"{taken.reference}{OFF}", WARN)

            attempt = noor.provider.reserve(
                quote_id=quote2.quote_id, customer_ref=noor.customer_id,
                idempotency_key="demo-noor-1",
            )
            say("outcome", f"{attempt.outcome.value} — {attempt.message}", BAD)
            say("", f"{DIM}the agent offers alternatives; nothing was promised{OFF}")

            # 3 ---------------------------------------------------------------
            scene(3, "two customers confirming the same car")
            third = ali.engine.list_fleet()[2]
            omar = customer(session, "+971500000103")
            q_a = noor.provider.quote(vehicle_id=third.id, pickup_at=pickup, return_at=ret)
            q_b = omar.provider.quote(vehicle_id=third.id, pickup_at=pickup, return_at=ret)
            say("both quoted", f"{q_a.quote_id} and {q_b.quote_id} for {third.id}")
            first = noor.provider.reserve(
                quote_id=q_a.quote_id, customer_ref=noor.customer_id, idempotency_key="race-a"
            )
            other = omar.provider.reserve(
                quote_id=q_b.quote_id, customer_ref=omar.customer_id, idempotency_key="race-b"
            )
            say("Noor", first.outcome.value, GOOD if first.booked else BAD)
            say("Omar", other.outcome.value, GOOD if other.booked else BAD)
            say("", f"{DIM}exactly one; the check and the take are one operation{OFF}")

            # 4 ---------------------------------------------------------------
            scene(4, "the booking system stops answering")
            fourth = ali.engine.list_fleet()[3]
            q_c = ali.provider.quote(vehicle_id=fourth.id, pickup_at=pickup, return_at=ret)
            provider.fail_next = "timeout"
            hung = provider.reserve(
                quote_id=q_c.quote_id, customer_ref=ali.customer_id,
                idempotency_key="demo-timeout",
            )
            say("outcome", f"{hung.outcome.value} — {hung.message}", WARN)
            say("", f"{DIM}the customer is told it is still being confirmed — "
                    f"never that it worked or failed{OFF}")

            retry = provider.reserve(
                quote_id=q_c.quote_id, customer_ref=ali.customer_id,
                idempotency_key="demo-timeout",
            )
            say("retry", f"{retry.outcome.value}   {DIM}the same operation, not a second car{OFF}")
            booked_count = len([
                r for r in ali.reservations.for_customer(ali.customer_id)
                if r.vehicle_id == fourth.id
            ])
            say("bookings", f"{booked_count} for that car", GOOD if booked_count <= 1 else BAD)
            say("resolve", provider.resolve("demo-timeout").outcome.value)

            # 5 ---------------------------------------------------------------
            scene(5, "somebody else's reference")
            stolen = provider.get_reservation(
                booked.reference, customer_ref=omar.customer_id
            )
            say("Omar reads", f"{stolen.outcome.value} — {stolen.message}", GOOD)
            say("", f"{DIM}the same answer as a reference that does not exist{OFF}")

            session.commit()

    print(f"\n{BOLD}Every booking above is a demonstration.{OFF} "
          f"{DIM}No vehicle is reserved and no payment is taken.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
