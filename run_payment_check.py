"""Create one real payment link, to see the thing work.

    .venv/bin/python run_payment_check.py

Needs a Stripe key in .env:

    PAYMENT_PROVIDER=stripe
    STRIPE_API_KEY=sk_test_...

Test keys are free and self-serve at dashboard.stripe.com/test/apikeys — nothing
here needs the operator's account, and a sk_test_ key cannot move real money.

This deliberately goes through the same service the agent uses rather than
calling Stripe directly, so what you see is what a customer would get: the amount
comes from a stored quote, and there is no way to pass one in.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rental_agent.context import ToolContext
from rental_agent.env import load_dotenv
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.tools.registry import execute_tool

load_dotenv()
TZ = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 9, 1, 10, tzinfo=TZ)


def main() -> None:
    provider = os.getenv("PAYMENT_PROVIDER", "simulated")
    key = os.getenv("STRIPE_API_KEY", "")
    print(f"\n  provider : {provider}")
    if provider == "stripe":
        kind = "LIVE — this can take real money" if key.startswith("sk_live_") else "test mode"
        print(f"  key      : {key[:12]}… ({kind})" if key else "  key      : MISSING")
        if key.startswith("sk_live_"):
            print("\n  Refusing to run against a live key. Use sk_test_ to see how it works.\n")
            return
    else:
        print("  (simulated — set PAYMENT_PROVIDER=stripe in .env for a real link)")

    factory = init_db(create_db_engine("payment_check.db"))
    with factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda: NOW, reference_date=NOW.date())
        customer, _ = ctx.customers.get_or_create("+971500000123", NOW)
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, NOW)
        ctx.customer_id, ctx.conversation_id = customer.customer_id, conversation.conversation_id
        session.flush()

        vehicle = next(v for v in ctx.engine.list_fleet() if "RS3" in v.model)
        quote = execute_tool(ctx, "create_demo_quote", {
            "vehicle_id": vehicle.id,
            "pickup_at": (NOW + timedelta(days=9)).isoformat(),
            "return_at": (NOW + timedelta(days=11)).isoformat(),
            "delivery_location": "Dubai Marina",
        })
        if "error" in quote:
            print(f"\n  could not quote: {quote}\n")
            return
        print(f"\n  quote    : {quote['quote_id']} — {vehicle.display_name}")
        print(f"  total    : {quote['currency']} {quote['total_charge']}  (from the engine)")

        booking = execute_tool(ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]})
        if booking.get("status") == "held":
            # This operator holds until a person confirms; a held car is not
            # asked for money, so confirm it here to reach the payment step.
            from rental_agent.services.booking import apply_hold_decision

            apply_hold_decision(ctx, booking["reservation_id"], "approved")
            print(f"  booking  : {booking['reservation_id']} (hold confirmed for this check)")
        else:
            print(f"  booking  : {booking['reservation_id']}")

        link = execute_tool(ctx, "create_payment_link", {
            "reservation_id": booking["reservation_id"]
        })
        session.commit()

        if "error" in link:
            print(f"\n  refused  : {link['error']} — {link.get('message')}\n")
            return

        print(f"\n  charging : {link['currency']} {link['amount']}  ({link['purpose']})")
        print(f"  reference: {link['payment_reference']}")
        print(f"\n  OPEN THIS:\n    {link['payment_url']}\n")
        if provider == "stripe":
            print("  Test card 4242 4242 4242 4242, any future expiry, any CVC.\n")


if __name__ == "__main__":
    main()
