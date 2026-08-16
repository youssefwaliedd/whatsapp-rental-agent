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
  agent/
    providers/        model adapters — gemini (default, free tier), anthropic
    schemas.py        the 17 tools as JSON schemas the model can call
    prompt.py         frozen system prompt + per-turn state snapshot
    extraction.py     structured entity extraction + additive state merge
    loop.py           the tool-use loop
    settings.py       model, effort and limits (all env-overridable)
  formatting.py       WhatsApp rendering of engine facts
  simulator/cli.py    local console + scripted scenario replay
tests/                218 tests, frozen clock, fixed reference date,
                      scripted model responses (no API key needed)
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
- **Talk to it:** type anything not starting with `/` (needs `GEMINI_API_KEY`)
- **Check models:** `/models` lists what your key can actually reach
- **Reset:** `/demo-reset` wipes the demo database and reloads config

Write commands persist to `demo.db` (override with `--db`).

## What the scripted scenarios are and are not

`/scenario 1..6` replay fixed customer/agent wording while pulling every number
from the engine and the database live. They are **not** the agent — they exist to
show the stack can supply everything a conversation needs, and they still run
without an API key.

To talk to the actual agent, set a key and just type into the console.

## Model provider

The agent runs on **Google Gemini's free tier** by default. Get a key at
<https://aistudio.google.com/apikey>, then:

```bash
export GEMINI_API_KEY='...'
.venv/bin/python -m rental_agent.simulator.cli --date 2026-09-01
```

Everything below the agent — engine, tools, persistence, state, escalation — is
provider-agnostic. Only `agent/providers/` knows an SDK exists, so switching is
an environment variable:

```bash
RENTAL_AGENT_PROVIDER=anthropic   # needs ANTHROPIC_API_KEY
GEMINI_MODEL=gemini-2.5-flash     # or whatever `/models` shows you
```

Scenarios that write (booking, escalation) run against a scratch database, so
they are reproducible on demand and never pollute the demo data.

## Status

See `MILESTONES.md`. Milestones 1–3 are built and tested: the engine, the full
tool surface, persistence, customer memory, idempotency, and the stateful agent.

The agent is tested against scripted model responses — no live conversation has
run yet. Not built at all: the WhatsApp transport and the evaluation/learning
loop.
