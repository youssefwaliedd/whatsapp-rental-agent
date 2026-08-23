"""What the database thinks about a booking's payment, and what Stripe thinks.

    .venv/bin/python check_payment_status.py DEMO-1045
"""

from __future__ import annotations

import os
import sys

import httpx

from rental_agent.env import load_dotenv
from rental_agent.store.db import create_db_engine, init_db

load_dotenv()


def main(reservation_id: str) -> None:
    from rental_agent.context import ToolContext

    factory = init_db(create_db_engine())
    with factory() as session:
        ctx = ToolContext(session=session)
        reservation = ctx.reservations.get(reservation_id)
        if reservation is None:
            print(f"\n  {reservation_id} is not in this database\n")
            return
        print(f"\n  YOUR DATABASE")
        print(f"    booking  : {reservation.reservation_id} · {reservation.status}")
        print(f"    payment  : {reservation.payment_status}")
        print(f"    reference: {(reservation.payment_reference or '—')[:34]}")
        for entry in reservation.history or []:
            if entry.get("event") in {"payment_link_created", "payment_received"}:
                print(f"    history  : {entry['event']} {entry.get('amount', '')}")
        session_id = reservation.payment_reference or ""

    key = os.getenv("STRIPE_API_KEY", "")
    if key and session_id.startswith("cs_"):
        data = httpx.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                         auth=(key, ""), timeout=20).json()
        print(f"\n  STRIPE")
        print(f"    status   : {data.get('status')} · payment {data.get('payment_status')}")
        print(f"    amount   : {data.get('amount_total')} {str(data.get('currency','')).upper()}")
    print()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "DEMO-1045")
