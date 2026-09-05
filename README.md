# WhatsApp Rental Agent

A WhatsApp sales agent for Delta Rentals Dubai, built against their written
scope. **Integration-ready with a simulated booking provider — not connected to
Delta's systems.**

The fleet is real: 113 vehicles, their prices and their photographs, imported
nightly from their public catalogue. Availability, bookings and payments are
simulated, every reservation is labelled a demonstration, and twelve figures
they have never published are deliberately left blank rather than guessed.

## The rule that shapes everything

**The model never computes or invents a fact.** Availability, prices, totals,
deposits, discount ceilings, booking status, payment status and policy come from
the engine reading approved configuration. The model chooses what to say and
which tool to call; the engine decides what is true.

A customer saying *"you always give me 20% off"* cannot become a business rule.

Outbound checks run after the model writes its reply and before the customer
sees it. They cover these known failure patterns:

| Guard | Refuses |
|---|---|
| `agent/figures.py` | a price no tool produced |
| `agent/availability.py` | a car called free when nothing checked |
| `agent/holds.py` | a booking called done that nothing confirms |
| `agent/facts.py` | unsupported staff-contact, specification, vehicle-price and delivery claims |

The checks share one rewrite attempt. Every rewrite passes all the checks again.
Rejected proposals remain in the evaluation record, and verified quote cards
survive rejected summaries and provider outages.

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in GEMINI_API_KEY at minimum
```

| Command | What it does |
|---|---|
| `.venv/bin/python run_chat.py` | the agent in a browser at :8100, with an inspector and a learning panel |
| `.venv/bin/python run_booking_demo.py` | the booking journey including conflicts and timeouts — no model, ~1s |
| `.venv/bin/python run_whatsapp_sim.py` | the WhatsApp behaviour *and the owner's side* — `/cases`, `/owner`, `/approve` |
| `.venv/bin/python run_webhook.py` | the real Meta webhook, for a live number |
| `.venv/bin/python run_learning.py` | what it has learned; `--replay`, `--promote`, `--rollback`, `--lost`, `--asked` |
| `.venv/bin/python run_benchmark.py` | the agent against Delta's own thirteen conversations, side by side |
| `.venv/bin/python run_payment_check.py` | a real Stripe test checkout, end to end |
| `.venv/bin/python -m pytest -q` | the suite |

Only `run_chat`, `run_whatsapp_sim`, `run_webhook`, `run_learning --replay` and
`run_benchmark` need a model.

## Layout

```
config/
  fleet.json           Delta's 113 vehicles, refreshed nightly. `_assumed` lists
                       what is inferred rather than known — the de-demo pass is
                       gated on that list being empty
  rules.json           the only source of policy truth. `_pending_confirmation`
                       holds the twelve figures they have not confirmed
  playbook.md          how they actually sell, written from their real chats
  policy/              their published terms, indexed for retrieval. The indexer
                       REFUSES a document containing a figure

rental_agent/
  agent/               the conversation loop, the prompt, outbound checks,
                       model providers (gemini, anthropic)
  booking_provider/    the booking system behind an interface: seven operations,
                       six outcomes, a simulated provider, a delta.py stub
  domain/              consent and incident gates — decisions too important to
                       leave to the model
  engine/              availability, pricing, search. Owns every number
  evaluation/          the learning loop: checks, review, regression cases,
                       replay, versioned strategies
  knowledge/           BM25 retrieval over the policy documents
  services/            booking, escalation, handover, outcomes, idempotency
  store/               SQLAlchemy models and repositories
  whatsapp/            the Cloud API transport, pacing, reactions, the webhook
```

## How the learning loop works

Automatic when a conversation ends: it is judged by twelve deterministic checks
and (with an Anthropic key) a reading pass, each failure is kept as a permanent
regression test, and the proposed lesson set is rebuilt.

Deliberate: proving a candidate against every stored test (`--replay`), and
putting it in front of customers (`--promote`). `--rollback` withdraws one.

**A lesson can never contain a figure or a policy claim** — `reject_unsafe_lesson`
refuses it, whatever produced it. So the loop can change how the agent sells and
never what it charges. The model's weights never change; what changes is a short
numbered list appended to its instructions.

Nothing runs on a schedule. A loop that fires unattended is one nobody audits.

## What is simulated, and what is not

| Real | Simulated |
|---|---|
| Vehicles, prices, photographs | Availability |
| Delta's published terms | Every booking and reference |
| Their sales approach | Payments outside Stripe test mode |
| | Deposits and eleven other figures |

`BOOKING_PROVIDER=delta` raises rather than running: a connector that answered
some questions from Delta and the rest from demo data would be the most
dangerous thing in this repository.

## Documentation

- [Latest fixes, test evidence and launch gaps](docs/chatbot-audit-2026-09-05.md)

- `docs/on-whatsapp.md` — what is left to get it onto a real number, in order
- `docs/go-live.md` — the full runbook, every screen and value
- `docs/policy-questionnaire.md` — the 33 questions Delta still has to answer
- `docs/client-requests.md` — what is needed from them, and what each item blocks
- `rental_agent/booking_provider/delta.py` — what a real connector must implement
- `MILESTONES.md` — the acceptance table

## Known limits

- Gemini daily quota interrupted the latest live tests. Model calls share a
  configurable 30-second turn budget, with bounded Gemini requests. The 2–5 second
  response target remains unproven; benchmark the chosen provider under real load.
- **Availability comes from nowhere real.** Delta publishes none. Every booking
  is a request until a provider that can settle it exists.
- One escalation number is implemented. CRM integration, staff availability and
  automatic assignment need additional work if those options in the requirements
  document are selected. Phase 2 analytics and management are not complete.
