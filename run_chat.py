"""Local chat harness for testing the agent.

    .venv/bin/python run_chat.py     then open http://localhost:8100

Development tooling, not a product surface — customers only ever reach this
agent through WhatsApp. What this adds over the console is the inspector panel:
which tools ran, and what the agent currently believes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import uvicorn

from rental_agent.agent.loop import build_agent
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.webchat.app import create_app

logging.basicConfig(level=logging.WARNING)

#: The demo fleet's seeded availability is anchored to a reference date, so the
#: clock is frozen here for the same reason the scenarios freeze it: a demo
#: should look identical whenever it is run.
TZ = ZoneInfo("Asia/Dubai")
REFERENCE_DATE = date(2026, 9, 1)
FROZEN_NOW = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)

_agent = None


def agent_factory():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


app = create_app(
    session_factory=init_db(create_db_engine()),
    agent_factory=agent_factory,
    reference_date=REFERENCE_DATE,
    now_fn=lambda: FROZEN_NOW,
)

if __name__ == "__main__":
    print("\n  Sandline Rentals — test chat")
    # Pay the provider's first-call cost now rather than making the first
    # message of the session absorb it.
    print("  warming up the model…", end="", flush=True)
    took = agent_factory().warm_up()
    print(f" {took:.1f}s" if took else " unavailable (it will retry on your first message)")
    print("  http://localhost:8100\n")
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
