# Milestones

Tracking against the ten development phases in the project specification.

## ✅ Milestone 1 — engine, tools, simulator (complete, 76 tests passing)

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
- [x] Alternatives: tiered substitution (identical model → same brand/class →
      same class → adjacent class), ranked by price closeness
- [x] Tool layer: `search_available_vehicles`, `get_vehicle_details`,
      `calculate_quote`, `find_alternatives`, `get_allowed_discount`, all
      returning JSON-safe values and error envelopes
- [x] WhatsApp formatting with mandatory demo footer
- [x] Local console + 4 scripted scenarios

**Verified by test, not by assertion:** quote line items reconcile to the total;
no rental length is ever priced above the plain daily rate; discounts above the
configured ceiling are refused at the engine; the shortlist cannot exceed three;
an underage driver cannot be shown a supercar; live reservations block a vehicle.

## ▢ Milestone 2 — persistence and the state machine

- [ ] SQLite schema + SQLAlchemy: customers, conversations, messages, state,
      quotes, reservations, tool-call audit log
- [ ] Idempotency keys on every state-changing tool; replaying a tool call
      returns the original result instead of creating a second reservation
- [ ] Reservation store wired into the engine's `extra_blocks_provider`
- [ ] State-changing tools: `create_demo_quote`, `create_demo_reservation`,
      `modify_demo_reservation`, `extend_demo_rental`, `cancel_demo_reservation`,
      `record_demo_documents`, `simulate_payment`, `schedule_demo_delivery`,
      `get_customer`, `save_customer_preference`, `get_active_reservation`,
      `escalate_conversation`
- [ ] Demo reference generator (`DEMO-1042`)
- [ ] Admin commands: `/demo-reset`, `/demo-bookings`, `/demo-conversations`

## ▢ Milestone 3 — the stateful agent

- [ ] Intent and entity extraction into `ConversationState`
- [ ] Stage machine and the "consult state before asking" rule
- [ ] Anthropic tool-use loop with the typed tool surface
- [ ] Contextual follow-up resolution ("make it 8 instead")
- [ ] Escalation detection against the configured triggers
- [ ] Scenarios 5–8 replacing scripted wording with generated wording

## ▢ Milestone 4 — WhatsApp transport

- [ ] Cloud API webhook, signature verification, 200-fast + async processing
- [ ] Message deduplication on `message_id`, retry handling
- [ ] Media (vehicle photos) — needs publicly reachable image URLs, currently
      placeholder paths in `fleet.json`
- [ ] Staff escalation to a configured WhatsApp number

## ▢ Milestone 5 — evaluation and learning

- [ ] Event-based conversation evaluator (recorded tool calls and state
      transitions first, model judgement second)
- [ ] Mistake classification and structured corrections
- [ ] Regression test generation from real conversations
- [ ] Replay harness over historical scenarios
- [ ] Versioned strategies, activated only when replay passes
- [ ] Lesson retrieval into future conversations
- [ ] `/demo-report`, `/demo-learning`
- [ ] The visible before/after learning demonstration

## Open items needing a decision

- **Vehicle photos.** WhatsApp needs publicly reachable URLs or uploaded media
  ids. `fleet.json` currently holds placeholder paths. Options: commit ~20
  licensed images and serve them, or upload once to the WhatsApp media API and
  store the ids. Needed before Milestone 4, not before.
- **WhatsApp Business test number.** Needed only at Milestone 4. No credentials
  required until then.
