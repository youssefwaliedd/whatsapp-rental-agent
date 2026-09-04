# What is left, and what is needed

Status against `ai sales agent scope for developers.pdf`, as of 4 September 2026.
983 tests, 97 commits.

The agent runs on Delta's real fleet, answers from their published terms, sells
in the patterns taken from their own conversations, refuses to state a figure
nobody produced, escalates what it cannot answer with the full context of the
conversation, takes a payment, and reviews every conversation the moment it ends.
**Sections 2, 3 and 4 of their scope are complete.**

What remains is three decisions, a short list of code, and four things only they
can hand over.

---

## Where each requirement stands

**Section 1 — Conversational AI Layer**
Knowledge-base driven and never inventing a figure: done, and the strongest part
of the build. Three guards now sit between the model and the customer — a price
no tool produced, a car called free when nothing checked, and a booking called
done that nothing confirms are each refused on the way out, with one retry and
then wording that promises nothing. Sales script and objection handling: done,
written from their thirteen conversations. **Not done: it runs on Gemini's free
tier rather than Claude or GPT, and that tier measured 54 seconds against their
2–5 second target.** One decision fixes both.

**Section 2 — WhatsApp-native behaviour** — complete. Direct Cloud API, typing
indicators, reactions, photo sets, natural pacing, and payment links verified end
to end against a real Stripe test checkout.

**Section 3 — Escalation and human in the loop** — complete, and beyond what they
asked. They assumed two kinds of case; there are three — a decision the owner
picks, a handover where a person takes over, and an *answer*, where the owner
supplies a figure nobody else has and the agent relays it exactly. As of 3
September the owner also receives a proper briefing before the question: who the
customer is and how to reach them, which car for which dates, what was quoted,
the standing of any booking, and the last eight messages. Deliberately no
recommended action — on a figure this operator has never published, a suggested
number would anchor the one person who actually knows it.

**Section 4 — Improvement over time** — complete, and explicitly not a day-one
requirement. Every conversation is tagged booked, dropped or escalated and
**judged automatically the moment it ends**: twelve deterministic checks, each
failure kept as a permanent regression test, the habit counts updated and the
proposed lesson set rebuilt. Proving those lessons and putting them in front of
customers stay deliberate, which is the distinction their bullet four was written
to test for. A reading pass covers the four items no fixed check can see — weak
objection handling, a close never attempted, questions customers keep asking,
objections they raise — and is **dormant until an Anthropic key exists**.

**Section 5 — Data source of truth** — the one section that cannot be closed from
this side. Their scope rules out working from a public website, which is exactly
where the fleet comes from, and availability is published nowhere at all.

Since 4 September the whole booking path runs through a **connector**: seven
operations, six outcomes, a simulated provider that is authoritative for demo
inventory, and a stub for Delta's system that raises rather than ever falling
back to demo availability. Their system arrives as a class, not a rewrite. What
it cannot do is invent the data — see part three, item 3.

---

## Part one — decisions only you can make

**1. Who pays the running cost.** Roughly AED 400-550 a month for the model, plus
hosting. Nothing else in the project is blocked on money, and this one blocks two
requirements at once: moving off the free tier is what makes it Claude and what
makes it hit 2-5 seconds. Their scope leaves the engagement structure "to be
discussed" — this belongs in that conversation rather than being absorbed quietly.

**2. Where it runs, and in whose account.** Railway, Render or Fly each provide a
database on the same network. The account should be theirs: it will hold their
customers' names, numbers and complete conversation histories.

**3. Whether to answer their four questions now.** Their scope asks four
questions directly and expects them answered in the reply. Three can now be
answered with a working system rather than a claim — see part four.

## Part two — code left on your side

**Live now, nothing blocking:**

- **Finish a learning cycle.** The loop has proposed, proved and *rejected* —
  which is the gate working — but nothing has ever been promoted, so no lesson is
  serving customers. One replay stands between here and a completed cycle. 85
  minutes on the free tier, about 7 on a paid one.
  *One caveat before reading the last verdict:* it counted dropped connections
  as regressions. Fixed 4 September, but the 13/21 that rejected `strategy_1.6`
  predates the fix and should be re-run rather than believed.

- **One lesson genuinely does not work.** `repeated_question` came back on
  replay. Worth reading before the next promote.

**Waiting on their answers, deliberately:**

- **The de-demo pass** — 262 references, seven tool names, the `DEMO-`/`DQ-`
  prefixes and the demonstration footer. Half a day, and it must happen *after*
  their figures are confirmed: `fleet.json` gates the flip on every entry in
  `_assumed` being answered, and the disclosure and the real data have to move
  together. Real fleet with the footer still on looks broken; simulated
  availability without it is a fake booking presented as real.

- **A payments table.** Payment is a field on a booking, which fits one payment
  per booking. Their own conversations show a holding payment and then a balance,
  which does not. Cheap now, awkward once real payments exist. Question 22
  decides it.

**Needs an Anthropic key rather than a decision about Delta:**

- **The reading pass is built and dormant.** Without it, four of the eight things
  their section 4 asks to analyse produce no data at all: frequently asked
  questions, customer objections, weak responses, and conversations where the
  agent failed to close. `run_learning.py --asked` correctly reports nothing and
  says why.

**Not needed, and worth recording as such:**

- ~~Hosting the vehicle photographs.~~ All of Delta's images are absolute URLs on
  their own server and Meta fetches them itself, so they stay where they live.
- ~~Quoting accuracy.~~ The specific failures the benchmark found — a three-day
  total for a two-day rental, a car called available and booked out two messages
  later — are now refused structurally rather than corrected case by case.

## Part three — what is needed from Delta

**Three of these are things their own scope says they would provide.**

**1. WhatsApp Business account access** — promised in their scope, not yet sent.
Without it there is no live product. Never register a personal number with the
Cloud API; it removes that number from WhatsApp permanently.

**2. One approved message template, named `case_update`.** Only they can request
it, and Meta takes a day or two, which is why it should be asked for first.
Without it, an owner answering an escalated case the next morning cannot reach
that customer at all.

**3. A maintained source for pricing and availability** — their section 5. Their
team checks availability by hand; whatever they check, we need to read. This is
the single largest unknown in the project: a spreadsheet is an afternoon, a fleet
management system with an API is an integration nobody can size unseen.

**4. The figures their website does not publish** — `policy-questionnaire.pdf`,
now written as confirmation rather than blanks, because their own conversations
answer most of it. Their team quotes AED 500 + VAT for the no-deposit option,
AED 5,000 deposit held 28 working days, an excess "from AED 3,500" where their
terms say 5,000, and AED 15 per extra kilometre where their terms say the range
starts at 25.

**5. Which of their two rental agreements is current.** Their terms page carries
two, disagreeing on about twelve figures. One reply resolves all of them. It is
question 26, and it is the highest-value question on the list.

**6. Their sales playbook and objection script** — promised, not sent. The
playbook in use was written from their conversations instead, which is a decent
substitute and not the same thing.

**7. Their Stripe account, or the answer that they do not want payment links.**
Their scope asks for links; their terms say payment happens at collection and in
thirteen conversations nobody ever sent one. Question 22.

**8. Sign-off on the escalation rules.** Their scope asks for configurable rules
for what escalates. Configurable is built; deciding what the agent may settle
alone is a business call nobody has made. Every discount ceiling is currently
zero, which is safe and will exhaust them within a week.

**9. Who answers escalations** — a number, and the hours a person is reachable.

## Part four — the four questions their scope asks you

Unanswered so far, and the only part of their document with nothing produced
against it.

**"Have you built directly on Meta's Cloud API before, not just through a no-code
tool?"** — the answer is this system: raw webhook, signature verified on the raw
body, own send client, typing indicators, reactions, interactive buttons, the
24-hour window and template re-opening.

**"How would you architect the escalate-to-owner flow?"** — three kinds of case,
a decision recorded against the customer it belongs to, the owner's answer
relayed verbatim, timeouts that tell the customer honestly, and the 24-hour
window handled.

**"What is your honest take on the learns-over-time requirement?"** — they wrote
this one as a filter. The answer is a gated, replay-verified, human-reviewable
loop, plus a benchmark that replays their own conversations so a change is
measured instead of asserted. Nothing about it is autonomous, which is the point.

It is worth answering with the run that failed rather than the design that
succeeded: five sensible-looking lessons were proposed, replayed against every
past conversation, and rejected. That is the difference between a system that
improves and one that hopes — and a claim nobody can make by describing an
architecture.

**"Timeline and cost for phase 1 versus phase 2?"** — the only one of the four
that needs a decision from you rather than a description. Both phases are
substantially built.

---

## The order that actually unblocks things

1. **Send the questionnaire, and ask for the number and the template.** Meta's
   template approval is the longest lead time in the project, and question 26
   settles about twelve figures in one reply.
2. **Answer their four questions** while waiting. Three are descriptions of a
   working system; the fourth needs a price from you.
3. **Decide the running cost**, then switch the provider. That closes two lines
   of their section 1 at once, and makes a replay seven minutes rather than
   eighty-five.
4. **Finish a learning cycle** — one replay, one promote — so "it learns from its
   conversations" is something demonstrated rather than described.
5. **When their figures arrive:** encode them, set the discount ladder, de-demo,
   deploy.

Nothing on that list past step one waits on anything but a message being sent.

---

## What this is not

It has never run on a real WhatsApp number, and no real customer has spoken to
it. Every test so far has been through a local window on a laptop.

Availability is simulated, and no amount of work on this side changes that. The
booking connector makes Delta's system a class rather than a rewrite; it cannot
invent the data that class would read.

Call it **integration-ready with a simulated provider**, never *connected to
Delta*.
