"""Local chat harness for testing the agent.

    .venv/bin/python run_chat.py     then open http://localhost:8100

Development tooling, not a product surface — customers only ever reach this
agent through WhatsApp. What this adds over the console is the inspector panel:
which tools ran, and what the agent currently believes.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import uvicorn

from rental_agent.agent.loop import build_agent
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.webchat.app import DEFAULT_HANDLE, create_app
from rental_agent.whatsapp.port import refuse_if_taken

logging.basicConfig(level=logging.WARNING)

#: The clock was frozen here so the *fictional* demo fleet, whose availability
#: is a seeded calendar anchored to a reference date, looked the same whenever
#: it was run. Delta's real 113 vehicles carry no seeded windows, so the freeze
#: now buys nothing and costs something: every conversation, evaluation and
#: mistake was stamped 1 September 2026 at 10am, which means a finding cannot be
#: dated against the fix that should have prevented it.
#:
#: Real time by default. `RENTAL_AGENT_FROZEN_CLOCK=1` brings the freeze back
#: for the seeded fleet.
TZ = ZoneInfo("Asia/Dubai")
FROZEN = os.getenv("RENTAL_AGENT_FROZEN_CLOCK", "") not in ("", "0", "false", "no")
FROZEN_NOW = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)


def now() -> datetime:
    return FROZEN_NOW if FROZEN else datetime.now(TZ)


REFERENCE_DATE = FROZEN_NOW.date() if FROZEN else datetime.now(TZ).date()

PORT = 8100

_agent = None


def agent_factory():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


session_factory = init_db(create_db_engine())

app = create_app(
    session_factory=session_factory,
    agent_factory=agent_factory,
    reference_date=REFERENCE_DATE,
    now_fn=now,
)

def close_open_conversation(session_factory) -> bool:
    """Start each run of the harness on a clean conversation.

    The database outlives the process, so without this a new test session opens
    with the last one still in progress and the agent — correctly — carries it
    on. Someone testing a fresh scenario is then asked about a car they never
    mentioned, which reads as the bot hallucinating when it is remembering.

    A browser reload still resumes, because that is a reload rather than a new
    session. Closes rather than deletes: the transcript is what the evaluator
    reads. `--continue` picks up where the last run left off.
    """
    from rental_agent.context import ToolContext

    with session_factory() as session:
        ctx = ToolContext(session=session, now_fn=now,
                          reference_date=REFERENCE_DATE)
        customer, _ = ctx.customers.get_or_create(DEFAULT_HANDLE, FROZEN_NOW)
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
        if not ctx.messages.for_conversation(conversation.conversation_id):
            return False
        conversation.outcome = "chat_session_ended"
        session.commit()
        return True


if __name__ == "__main__":
    refuse_if_taken(PORT)

    print("\n  Sandline Rentals — test chat")
    # Pay the provider's first-call cost now rather than making the first
    # message of the session absorb it.
    print("  warming up the model…", end="", flush=True)
    took = agent_factory().warm_up()
    print(f" {took:.1f}s" if took else " unavailable (it will retry on your first message)")
    if "--continue" in sys.argv:
        print("  continuing the previous conversation")
    elif close_open_conversation(session_factory):
        print("  fresh conversation — the last one is kept; --continue resumes it")
    print("  http://localhost:8100\n")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
