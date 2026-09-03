"""Two customers confirming one car at the same moment, on real Postgres.

Not a pytest test on purpose. The suite runs on SQLite, which takes a write lock
for the whole transaction and therefore *cannot* fail this — passing there would
prove the database serialises writes, not that the provider checks and takes
inventory atomically. The interesting version needs real connections racing.

    .venv/bin/python tests/concurrency_booking_check.py

Reports and exits non-zero if more than one confirmation succeeds. If Postgres
is not reachable it says so and exits non-zero rather than claiming a pass:
"concurrency verified" is a claim about a database that was actually there.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Runs as a script from anywhere, so the repo root has to be on the path — a
# check that needs an environment variable to start is a check nobody runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rental_agent.booking_provider import Outcome
from rental_agent.context import ToolContext
from rental_agent.store.db import create_db_engine, database_url, init_db, is_sqlite

TZ = ZoneInfo("Asia/Dubai")
RACERS = 6

#: Fresh keys per run. Reusing them would make the ledger replay the previous
#: run's answers — correct behaviour, and it would quietly turn this into a
#: check of the ledger rather than of the lock.
RUN = uuid.uuid4().hex[:8]


def _free_window(ctx, vehicle_id: str) -> tuple[datetime, datetime]:
    """A window this car is actually free for.

    Run twice and the second run would otherwise race for a car the first run
    booked, every racer would be refused, and a repeatable check would report a
    failure that is really yesterday's success.
    """
    start = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) + timedelta(days=30)
    for offset in range(0, 400, 3):
        pickup = start + timedelta(days=offset)
        ret = pickup + timedelta(days=2)
        if ctx.provider.check_availability(vehicle_id, pickup, ret).available:
            return pickup, ret
    raise RuntimeError(f"no free window for {vehicle_id} in the next year")


def _quote_for(session_factory, handle: str, vehicle_id: str, pickup, ret) -> tuple[str, str]:
    with session_factory() as session:
        ctx = ToolContext(session=session)
        customer, _ = ctx.customers.get_or_create(handle, ctx.now())
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, ctx.now())
        ctx.customer_id = customer.customer_id
        ctx.conversation_id = conversation.conversation_id
        answer = ctx.provider.quote(vehicle_id=vehicle_id, pickup_at=pickup, return_at=ret)
        session.commit()
        return answer.quote_id, customer.customer_id


def _confirm(session_factory, quote_id: str, customer_ref: str, key: str) -> Outcome:
    """One racer, in its own session and its own transaction."""
    with session_factory() as session:
        ctx = ToolContext(session=session)
        ctx.customer_id = customer_ref
        try:
            answer = ctx.provider.reserve(
                quote_id=quote_id, customer_ref=customer_ref, idempotency_key=key
            )
            session.commit()
            return answer.outcome
        except Exception as exc:  # noqa: BLE001 - a refused racer is a pass, not a crash
            session.rollback()
            print(f"    racer {key}: {type(exc).__name__}: {str(exc)[:90]}")
            return Outcome.UNAVAILABLE


def main() -> int:
    url = database_url()
    print(f"database: {url}")
    if is_sqlite(url):
        print(
            "\n  NOT RUN — this needs PostgreSQL.\n"
            "  SQLite serialises writes for the whole transaction, so a pass here\n"
            "  would say nothing about whether the provider takes inventory atomically.\n"
            "  Set DATABASE_URL to a Postgres instance and run again."
        )
        return 2

    try:
        session_factory = init_db(create_db_engine())
        with session_factory() as probe:
            ctx = ToolContext(session=probe)
            vehicle = ctx.engine.list_fleet()[0]
    except Exception as exc:  # noqa: BLE001
        print(f"\n  NOT RUN — could not reach the database: {exc}")
        return 2

    with session_factory() as probe:
        pickup, ret = _free_window(ToolContext(session=probe), vehicle.id)
    print(f"vehicle:  {vehicle.id} ({vehicle.display_name})")
    print(f"window:   {pickup:%a %d %b %H:%M} → {ret:%a %d %b %H:%M}")
    print(f"racers:   {RACERS}, each in its own session\n")

    prepared = [
        _quote_for(session_factory, f"+9715000009{index:02d}", vehicle.id, pickup, ret)
        for index in range(RACERS)
    ]

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        outcomes = list(pool.map(
            lambda job: _confirm(session_factory, job[1][0], job[1][1], f"race-{RUN}-{job[0]}"),
            list(enumerate(prepared)),
        ))

    confirmed = [o for o in outcomes if o is Outcome.CONFIRMED]
    for index, outcome in enumerate(outcomes):
        print(f"    racer {index}: {outcome.value}")

    with session_factory() as session:
        ctx = ToolContext(session=session)
        live = [
            r for r in ctx.reservations.all()
            if r.vehicle_id == vehicle.id and r.status == "confirmed"
            and r.pickup_at < ret and pickup < r.return_at
        ]

    print(f"\n  confirmations: {len(confirmed)}   overlapping bookings in the database: {len(live)}")
    if len(confirmed) == 1 and len(live) == 1:
        print("  PASS — exactly one customer got the car.")
        return 0
    print("  FAIL — the car was promised to more than one customer.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
