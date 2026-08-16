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

## ▢ Milestone 3 — the stateful agent

- [ ] JSON tool schemas for the Anthropic tool-use API (deferred from M2 to
      where they are actually consumed)
- [ ] Intent and entity extraction into `ConversationState`
- [ ] Stage machine and the "consult state before asking" rule
- [ ] Anthropic tool-use loop over the existing tool surface
- [ ] Contextual follow-up resolution ("make it 8 instead")
- [ ] Escalation detection from message content, not just explicit tool calls
- [ ] System prompt carrying the fact boundary and demo disclosure rules
- [ ] Scenarios replaced with generated rather than scripted wording

## ▢ Milestone 4 — WhatsApp transport

- [ ] Cloud API webhook, signature verification, 200-fast + async processing
- [ ] Wire `provider_message_id` dedup into the webhook path
- [ ] Media (vehicle photos) — needs publicly reachable image URLs
- [ ] Staff escalation to a configured WhatsApp number (`staff_notified`)

## ▢ Milestone 5 — evaluation and learning

- [ ] Event-based conversation evaluator reading the tool-call audit log first,
      model judgement second
- [ ] Mistake classification and structured corrections
- [ ] Regression test generation from real conversations
- [ ] Replay harness over historical scenarios
- [ ] Versioned strategies, activated only when replay passes
- [ ] Lesson retrieval into future conversations
- [ ] `/demo-report`, `/demo-learning`
- [ ] The visible before/after learning demonstration

## Open items needing a decision

- **Vehicle photos.** WhatsApp needs publicly reachable URLs or uploaded media
  ids. `fleet.json` holds placeholder paths. Options: commit ~20 licensed images
  and serve them, or upload once to the WhatsApp media API and store the ids.
  Needed before Milestone 4.
- **WhatsApp Business test number.** Needed only at Milestone 4. No credentials
  required until then.
- **Turnaround buffer between rentals.** Real operators need cleaning and
  inspection time between bookings; the prototype currently allows a return and
  the next pickup at the same minute. Trivial to add as a rules setting when a
  company asks for it.
