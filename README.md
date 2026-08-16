# WhatsApp Rental Agent — prototype

A self-improving WhatsApp agent for car-rental companies. This repository is the
**demonstration prototype**: a fictional operator, a fictional fleet, simulated
bookings and payments. Nothing here is a real rental system.

> Operator name, vehicles, prices, availability and policies are invented. They
> belong to no real company. Every booking the prototype creates is labelled as a
> demonstration.

## The one rule that shapes the architecture

The language model never computes or invents a fact. Availability, prices,
totals, deposits, discount ceilings, booking status, payment status and company
policy come from the **rental engine** reading **approved configuration**. The
model decides what to say and which tool to call; the engine decides what is
true.

A customer saying *"you always give me 20% off"* cannot become a business rule.

## Layout

```
config/
  fleet.json          20 fictional vehicles; availability as offsets from a
                      reference date, so the demo never goes stale
  rules.json          the only source of policy truth — ages, documents, fees,
                      discounts, insurance, cancellation, escalation triggers
rental_agent/
  config.py           loads and caches the approved configuration
  context.py          ToolContext: session + customer + conversation + clock
  domain/             enums (closed vocabularies) and Pydantic models
  engine/
    pricing.py        billable days, rate selection, fees, discounts, quotes
    availability.py   calendar logic; seeded blocks + live demo reservations
    search.py         hard filters, deterministic ranking, alternative tiers
    locations.py      "marina" -> "Dubai Marina"
    engine.py         the facade the tool layer calls
  store/
    models.py         SQLAlchemy schema (Postgres-compatible)
    types.py          decimals as text, datetimes with their offset
    repositories.py   every read and write; the blocking-status rule lives here
    db.py             engine, sessions, reset, demo-reference sequences
  services/
    booking.py        quotes, reservations, modify, extend, cancel, documents,
                      simulated payment, delivery, customer memory
    escalation.py     trigger classification and urgency
    idempotency.py    the ledger that stops duplicate reservations
  tools/
    rental_tools.py   read tools
    state_tools.py    write tools
    registry.py       one dispatch path: errors, audit, idempotency
  formatting.py       WhatsApp rendering of engine facts
  simulator/cli.py    local console + scripted scenario replay
tests/                189 tests, frozen clock, fixed reference date
```

All 17 tools from the specification are implemented: 7 read, 10 write. Every
write is idempotent and audited.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m rental_agent.simulator.cli
```

Freeze the clock for a reproducible demo:

```bash
.venv/bin/python -m rental_agent.simulator.cli --date 2026-09-01
.venv/bin/python -m rental_agent.simulator.cli --date 2026-09-01 --run "scenario 1"
```

Useful console commands:

- **Read:** `/fleet`, `/search models=G63 color=black`, `/quote veh_13 days=3
  location=Marina`, `/alts veh_18`, `/discount veh_13`
- **Write:** `/book veh_13 days=3 location=Marina`, `/modify DEMO-1042
  pickup=2026-09-04T20:00`, `/extend DEMO-1042 <iso>`, `/cancel DEMO-1042`
- **Inspect:** `/state`, `/demo-fleet`, `/demo-bookings`, `/demo-conversations`
- **Raw:** `/tool <name> <json>`, `/tools`
- **Reset:** `/demo-reset` wipes the demo database and reloads config

Write commands persist to `demo.db` (override with `--db`).

## What the scripted scenarios are and are not

`/scenario 1..6` replay fixed customer/agent wording while pulling every number
from the engine and the database live. They demonstrate that the stack can
supply everything the conversation needs, and they are **not** the agent — there
is no model in the loop yet. The stateful agent (Milestone 3) replaces the
scripted wording.

Scenarios that write (booking, escalation) run against a scratch database, so
they are reproducible on demand and never pollute the demo data.

## Status

See `MILESTONES.md`. Milestones 1 and 2 are complete and tested: the engine,
the full tool surface, persistence, customer memory and idempotency all work.

Not built yet: the agent loop (no language model is in the loop at all), the
WhatsApp transport, and the evaluation and learning loop.
