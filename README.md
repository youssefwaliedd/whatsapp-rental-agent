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
  domain/             enums (closed vocabularies) and Pydantic models
  engine/
    pricing.py        billable days, rate selection, fees, discounts, quotes
    availability.py   calendar logic; seeded blocks + live demo reservations
    search.py         hard filters, deterministic ranking, alternative tiers
    locations.py      "marina" -> "Dubai Marina"
    engine.py         the facade the tool layer calls
  tools/
    rental_tools.py   the typed surface the model is allowed to touch
  formatting.py       WhatsApp rendering of engine facts
  simulator/cli.py    local console + scripted scenario replay
tests/                76 tests, frozen clock, fixed reference date
```

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

Useful console commands: `/fleet`, `/search models=G63 color=black`,
`/quote veh_13 days=3 location=Marina`, `/alts veh_18`, `/discount veh_13`,
`/demo-fleet`, `/tool <name> <json>`, `/scenario`.

## What the scripted scenarios are and are not

`/scenario 1..4` replay fixed customer/agent wording while pulling every number
from the engine live. They demonstrate that the engine can supply everything the
conversation needs, and they are **not** the agent — there is no model in the
loop yet. The stateful agent (Milestone 4) replaces the scripted wording.

## Status

See `MILESTONES.md`. Milestone 1 is complete and tested; the conversation state,
persistence, agent loop, WhatsApp transport and learning loop are not built yet.
