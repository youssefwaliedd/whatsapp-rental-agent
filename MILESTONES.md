# Milestones

Tracking against the ten development phases in the project specification.

## ✅ Milestone 1 — engine, tools, simulator (complete)

- [x] Project scaffold, venv, git
- [x] Domain models and closed enums (stages, categories, intents, statuses)
- [x] `ConversationState` schema with `missing_requirements()` — the "never ask
      twice" primitive
- [x] Fictional fleet: 20 vehicles across 8 categories, availability as offsets
      from a reference date
- [x] Fictional rules: ages, documents, deposits, mileage, delivery zones,
      airport fees, discounts, cancellation, extension, late return, insurance,
      accidents, escalation triggers, demo disclosure
- [x] Pricing: billable days with grace period, best-of daily/weekly/monthly
      rates, zone delivery, out-of-hours, VAT, deposits, excess reduction
- [x] Availability: seeded blocks + live-reservation blocks, half-open windows,
      `next_available_from`
- [x] Search: hard filters (category, budget, seats, driver age), deterministic
      ranking, 3-option cap enforced in the engine
- [x] Alternatives: tiered substitution, ranked by price closeness
- [x] Read tools returning JSON-safe values and error envelopes
- [x] WhatsApp formatting with mandatory demo footer
- [x] Local console + scripted scenarios

## ✅ Milestone 2 — persistence, idempotency, full tool surface (complete, 189 tests)

- [x] SQLite + SQLAlchemy schema: customers, conversations, messages, tool
      calls, quotes, reservations, escalations, counters
- [x] `Money` and `AwareDateTime` column types — decimals stored as text, and
      naive datetimes refused outright rather than silently stored
- [x] `ToolContext` binding session, customer, conversation, clock and an engine
      that knows about live bookings
- [x] Conversation state persisted as JSON, separate from the transcript
- [x] Message deduplication on the provider message id
- [x] Idempotency ledger sharing one table with the audit log
- [x] Reservation store wired into the engine's `extra_blocks_provider`
- [x] **All 17 specification tools implemented** (7 read, 10 write)
- [x] Demo references: first booking is `DEMO-1042`, quotes are `DQ-`
- [x] Cancellation bands, extension notice period and re-rating, document
      requirements by residency, simulated payment, delivery scheduling
- [x] Escalation with trigger classification and urgency
- [x] Admin commands: `/demo-fleet`, `/demo-bookings`, `/demo-conversations`,
      `/demo-reset`
- [x] Console write commands and two new scenarios (booking + contextual
      follow-up, accident escalation)

**Verified by test, not by assertion:**

- The same booking call twice creates one reservation; a *different* booking is
  not mistaken for a replay.
- Applying a change, reverting it and re-applying it is three operations, not
  one replayed twice — while repeating a change that is already applied is a
  no-op that does not pollute history.
- Moving a delivery from 7 PM to 8 PM succeeds (the booking is excluded from its
  own availability check); moving it into someone else's booking is refused.
- A booked vehicle disappears from search and quoting for everyone else, and
  reappears when the booking is cancelled.
- A failed tool call leaves its idempotency key free, so a genuine retry works.
- Reads are never served from the ledger: the same search before and after a
  booking gives different answers.
- A reservation cannot exist without a quote the engine calculated; an expired
  quote must be re-quoted.
- `save_customer_preference` refuses any key that looks like a price, discount,
  deposit or policy — "you always give me 20% off" cannot become a rule.
- Money survives SQLite exactly; timezone-aware times survive with their offset.

## ✅ Milestone 3 — the stateful agent (complete, live-verified)

- [x] JSON tool schemas for all 17 tools, with prescriptive "call this when…"
      trigger conditions and a byte-stable ordering
- [x] Structured intent and entity extraction (`messages.parse`) with a
      **deterministic, additive-only merge** into `ConversationState`
- [x] Frozen system prompt + per-turn state injected as a mid-conversation
      system message, so state changes never invalidate the prompt cache
- [x] Manual tool-use loop with iteration cap, refusal handling and a safe
      fallback reply
- [x] Contextual follow-up resolution — the extractor is given the live booking
      and the clock, so "make it 8 instead" resolves to a full timestamp
- [x] Escalation executed **in code** from the extraction signal, so a model
      that forgets to call the tool cannot cause a missed incident
- [x] Server-side refusal fallbacks, degrading gracefully when the beta is off
- [x] Console chat mode — type a message and talk to the agent
- [x] **Provider abstraction**: Gemini (free tier, default) and Anthropic behind
      one duck-typed client surface. The engine, tools, persistence and state
      never learn which is in use.

**Verified by test:** an empty extraction cannot clear a known value; a
malformed timestamp is ignored rather than overwriting a good one; an accident
escalates even when the model never calls the tool; a redelivered WhatsApp
message is not answered twice; a runaway tool loop is capped and handed over; a
refusal never leaves the customer with silence; the system prompt contains
nothing volatile and is byte-identical across calls.

**Verified live** on Gemini (free tier), 17 Aug 2026 — a full conversation from
enquiry to booking to contextual follow-up:

- Correct engine-computed total (AED 7,560) quoted, never invented
- Demonstration notice on every quote and confirmation
- One question per turn; nothing re-asked
- `DEMO-1042` created, then "actually can you make it 8 instead" resolved via
  `get_active_reservation` → `modify_demo_reservation` with no "which car?"

**Five bugs the live run found that no offline test could:**

1. `503 UNAVAILABLE` was not in the retry predicate (only 429 was)
2. A provider outage **crashed the turn** instead of degrading to an apology
3. A failed extraction pass also crashed the turn, though it is only an
   enhancement — the agent can run on tools and stored state alone
4. Gemini 3.x **requires `thought_signature`** on function-call parts when they
   are replayed; the adapter was reconstructing calls and dropping it
5. The default model `gemini-2.0-flash` no longer exists, and `models.list()`
   happily returns models that then 404 as "no longer available to new users"

**Observed and fixed from live play:**

- Asked "is there a discount?", the agent announced its **maximum** ceiling for
  **three cars at once**, to a customer who had not chosen one. The figures were
  correct (10% economy / 13% sedan on a 7-day rental, straight from the rules) —
  the fact boundary held. The *selling* was the defect: it gave away its whole
  negotiating position unprompted. Now it asks which car first, opens below its
  limit, and keeps room to move.
- Still open, deliberately left as a first test case for the Milestone 5
  evaluator: the agent will say "we can arrange a black G63" *before* calling
  the availability tool. Not false, but a commitment made ahead of the facts.

**Free-tier operating notes:**

- Quota is `GenerateRequestsPerDay**PerModel**` — per model, per day. The
  advertised `retryDelay` of ~40s is misleading; a daily cap resets tomorrow.
- So the adapter distinguishes a per-day cap from a per-minute one: it waits out
  the latter (honouring Google's own `retryDelay`) and **fails over to the next
  model** for the former. Five models are tried in order.
- The extraction pass runs on a *different* model from the conversation, so two
  requests per turn land in two quota buckets rather than exhausting one.
- Budget roughly two requests per customer message when estimating a demo day.

## Required demonstration scenarios

The specification lists eight. This is the acceptance criterion that matters for
showing the prototype to a rental company, so it is tracked separately from the
milestones.

*Engine* = the underlying tools are tested. *Live* = a real conversation with the
agent has been observed doing it end to end.

| # | Scenario | Engine | Live |
|---|---|:---:|:---:|
| 1 | Requested vehicle is available | ✅ | ✅ black G63 → DEMO-1042 |
| 2 | Unavailable → targeted alternatives | ✅ | ✅ Huracan → Ferrari 488 Spider |
| 3 | Customer has a specific budget | ✅ | ✅ SUV under AED 400 → Fortuner |
| 4 | Customer requests a discount | ✅ | ✅ 5% offered against a 10% ceiling |
| 5 | Changes dates, location or delivery time | ✅ | ✅ "make it 8 instead" |
| 6 | Returns later with a contextual follow-up | ✅ | ✅ resolved without re-asking |
| 7 | Requests a rental extension | ✅ | ✅ +3 days, +AED 1,102.50 |
| 8 | Reports an accident → escalation | ✅ | ✅ safety first, 999, handed over |

**All eight proven live.** Two defects were found and fixed in the process:

- **Scenario 2 offered a Ford Mustang and a BMW X7 as substitutes for a
  Lamborghini**, and never called `find_alternatives`. Root cause: a plain search
  for an unavailable named model returns the *cheapest* unrelated car, so the
  ranking hands back a Kia Pegas for a Huracan. Fixed structurally — when a
  customer names a model that exists in the fleet but is not free,
  `search_available_vehicles` now returns no vehicles at all, only the vehicle id
  and a pointer to `find_alternatives`. The bad path is gone rather than
  discouraged.
- **Scenario 3 made eight tool calls, six of them searches.** Root cause: the
  extraction prompt never listed the category vocabulary, so "I need an SUV"
  produced `categories: []` and the agent had no constraint to search against.
  Adding the eight allowed values took the same turn to **one** tool call.

## ▢ Milestone 4 — WhatsApp transport

- [ ] Cloud API webhook, signature verification, 200-fast + async processing
- [ ] Wire `provider_message_id` dedup into the webhook path
- [x] Media: vehicle cards rendered from `fleet.json` (`rental_agent/media/cards.py`)
- [ ] Host them — GitHub Pages, so WhatsApp has a public HTTPS URL
- [ ] Send them in the conversation at the right moment
- [ ] Staff escalation to a configured WhatsApp number (`staff_notified`)

## ✅ Milestone 5 — evaluation and learning (complete)

The self-improvement loop, and the reason the project is more than a chatbot.

**Evaluation — reads events, not opinions**

- [x] Six deterministic checks over the transcript and tool-call audit log, no
      model consulted, so a finding is a fact and the same conversation always
      evaluates the same way
- [x] `unsupported_claim` compares every figure shown to a customer against
      every figure the tools returned — the automated guard on the central promise
- [x] Also: `missed_escalation`, `unauthorised_discount`, `repeated_question`,
      `no_alternatives_offered`, `too_many_options`
- [x] Abstains when the audit log holds no results to check against — absence of
      evidence is not evidence of invention
- [x] Mistake occurrences count *distinct conversations*, so re-evaluating cannot
      inflate them

**Learning — gated, versioned, reversible**

- [x] Corrections generated per mistake type, in the voice of the system prompt
- [x] **Safety boundary enforced in code**: a lesson carrying a figure, a
      percentage or a policy claim is refused before it can be stored. No amount
      of learning can move a price
- [x] Versioned strategies — additive lesson sets, never a prompt rewrite
- [x] Bad conversations become regression cases (customer turns only — replaying
      the agent's old replies would defeat the point)
- [x] Replay harness runs candidates in scratch conversations, excluded from reports
- [x] **Activation gate**: a candidate is activated only if every regression case
      passes. A failing candidate is rejected with its reason recorded and the
      previous version keeps serving
- [x] A crashing replay counts as a failure, never as a pass
- [x] Active lessons retrieved into the per-turn state block, so a new strategy
      takes effect on the next turn without a restart
- [x] `/demo-report`, `/demo-learning`, `/demo-learn`
- [x] `/scenario 7` — the visible before-and-after demonstration

**The demonstration** (`/scenario 7`, no API key needed): the agent quotes
AED 6,200 when the engine says 7,560 → evaluator catches it with evidence →
correction generated → the conversation becomes a regression case → candidate
replayed → activated only because nothing regressed → the lesson appears in the
next conversation's state block.

Verified both ways: an agent that repeats the mistake on replay gets its
candidate **rejected**, and `strategy_1.0` keeps serving with no lessons in force.

## Open items needing a decision

- ~~**Vehicle photos.**~~ **Decided:** cards rendered from `fleet.json` rather
  than stock photography. A stock photo of a white G-Wagon under the caption
  "black with red interior" is a mismatch a rental professional notices
  immediately, and it costs more credibility than a photograph buys. Cards
  cannot disagree with the data, carry no licensing risk, and are honestly
  labelled as demonstration material. A rental company adopting this points
  `fleet.json` at their own photographs and nothing else changes.
- **WhatsApp Business test number.** Needed only at Milestone 4.
- **A model API key.** Needed *now* to run the agent — free from
  <https://aistudio.google.com/apikey> for the default Gemini provider.
  Everything else in the project works without one.
- **Gemini model choice.** `gemini-3.7-flash` is the default (`gemini-2.0-flash`
  was retired mid-build). `/models` lists what a given key can actually reach,
  and `GEMINI_MODEL` overrides it. Note that `models.list()` returns models that
  then 404 — listing is not a capability check.
- **Turnaround buffer between rentals.** Real operators need cleaning and
  inspection time between bookings; the prototype currently allows a return and
  the next pickup at the same minute. Trivial to add as a rules setting when a
  company asks for it.
