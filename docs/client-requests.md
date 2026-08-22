# What we need from the operator

Everything in this file is blocked on someone at the rental company, not on
code. It is ordered by what it blocks rather than by effort: the first three
items mean there is no live product without them, the next three mean the agent
will state figures nobody approved, and the last two decide whether it sells
the way they sell.

Every item is something their own scope document already commits them to
providing. This is a collection list, not a wish list.

There is a ready-to-send version at the bottom.

---

## 1 — Nothing runs without these

### 1.1 The WhatsApp Business number

Access to the number itself, or an invitation to their Meta Business Manager
with the WhatsApp Business account attached.

Without it the agent is a demonstration. Every behaviour is built and tested
against simulated Meta payloads, so this is the single switch between a
convincing demo and a customer talking to it.

> **Never register a personal number with the Cloud API.** It removes that
> number from the WhatsApp app on the phone and there is no undo. Meta's free
> test number is the development path. See [go-live.md](go-live.md).

### 1.2 One approved message template, named `case_update`

They must request it from Meta themselves, under their own business account.
Approval takes a day or two, which is why it is on this list early rather than
on the day of launch.

**What breaks without it:** WhatsApp closes the service window 24 hours after a
customer's last message. A customer reports a problem at 11pm, the agent
escalates, the owner answers at 9am — and the agent is not allowed to deliver
that answer. The customer simply never hears back, and nothing in the system
can work around it.

The body text is already written and lives in `config/rules.json` under
`human_in_the_loop.reopen_template`. They submit it as-is; the name must match
exactly.

### 1.3 A Stripe account, or test keys to begin with

Their core requirements ask for payment links generated from the quoted price.
That cannot be built against nothing, and the link amount must come from the
stored engine quote rather than any figure a model produced — so this needs
their account, not ours.

---

## 2 — Without these the agent states figures nobody approved

### 2.1 A maintained source for pricing and availability

Their own requirement is explicit: pricing and availability must come from a
maintained knowledge source or API, **not scraped from a public website, which
may lag real inventory.**

At present the fleet is exactly that — 113 vehicles read from their public
catalogue, with prices parsed out of the individual vehicle pages, refreshed
nightly. It was the only source that existed. It is a stopgap and it does not
satisfy their requirement.

**Availability is the sharper half of this.** Their site publishes none at any
frequency, so what the agent currently works from is seeded placeholder data.
An agent that cannot tell whether a car is free is not a booking agent.

So the question for them is: what do they actually keep up to date? A fleet
management system, an API, a shared spreadsheet — anything maintained by a
person whose job it is. Whatever it is, we read from it.

### 2.2 The figures their website does not publish

Currently sitting at `0` in the configuration, which means the agent will state
them as zero to a customer:

- **Deposits.** Their listings say none; their terms say AED 5,000–20,000
  varying by vehicle, driver and duration. Both cannot be true.
- **Extra-kilometre charges.** Their terms indicate AED 25–150/km by vehicle.
  Left at zero rather than guessed.
- **Included mileage per day**, where it differs by vehicle.
- **Weekly and monthly rates**, listed publicly as "on request".

Also worth confirming while they are in the file: minimum driver age by
category, and the security-deposit release period.

This is the shortest item on the list and the one that most directly protects
them. Their stated number-one fear is the agent inventing a price; quoting a
deposit of zero on a Ferrari is that failure with a straight face.

### 2.3 Their terms and conditions document

Goes into `config/policy/` as markdown, indexed for retrieval, so the agent can
answer licence acceptance, fines, Salik, cross-border travel and documentation
questions instead of escalating each one to a person.

**Every figure gets stripped out of the prose and into `config/rules.json`.**
The indexer refuses a policy document containing an amount or a percentage —
deliberately, because a price reaching a customer through retrieved prose would
bypass the pricing engine entirely, and that is the one hole this system exists
not to have.

---

## 3 — So it sells the way they sell

### 3.1 The sales playbook and objection-handling script
### 3.2 Sample high-performing conversations

Both are promised in their brief. The agent's current sales behaviour — when to
offer alternatives, how hard to hold a price, what to say to "that's too
expensive" — is our best guess at a good salesperson. Theirs is based on what
actually converts for them.

The sample conversations are worth more than the script, because they also
become the reference the evaluator measures new prompt versions against.

---

## 4 — One decision, not a deliverable

**Where this runs, and whose account it lives in.**

The recommendation is that the hosting account belongs to them, not to us. It
will hold their customers' names, phone numbers and complete conversation
histories. That should not sit in a developer's personal account — for their
protection and ours.

Railway, Render or Fly all provide a Postgres database on the same network,
which is the simplest path and the lowest latency. A VPS means running or
managing the database separately. Once they pick, pointing `DATABASE_URL` at it
is the entire migration.

Related: the machine must stay awake. The nightly fleet refresh, the escalation
timeouts and the agent itself all stop when the process stops — which on a
laptop means when the lid closes.

---

## The message to send

> Hi — a few things I need from your side to move this forward. Most are things
> the scope document already has you providing, so this is really a collection
> list.
>
> **To go live at all:**
> 1. Access to the WhatsApp Business number, or an invite to your Meta Business
>    Manager.
> 2. One message template approved by Meta, named exactly `case_update` — I'll
>    send you the body text to submit. Without it, when you answer an escalated
>    case the next morning, WhatsApp will not let the assistant deliver your
>    answer to that customer.
> 3. A Stripe account (test keys are fine to start) for the payment links.
>
> **So it quotes correctly:**
> 4. Where do you keep pricing and availability up to date — a system, an API, a
>    spreadsheet? Your scope rightly rules out working from the public website,
>    and availability isn't published there at all. I need to read from whatever
>    you actually maintain.
> 5. The figures the site doesn't show: deposits by vehicle, extra-km charges,
>    included mileage, and weekly/monthly rates. They're currently blank, and
>    blank means the assistant would quote them as zero.
> 6. Your terms and conditions document.
>
> **So it sells like you:**
> 7. The sales playbook and objection-handling script.
> 8. A handful of real conversations that went well.
>
> One decision too: where this should be hosted. I'd recommend the account be in
> your name rather than mine, since it will hold your customers' details and
> full chat history.
>
> Items 1 and 2 are the long poles — Meta takes a couple of days to approve a
> template, so those are worth starting today.

---

Related: [go-live.md](go-live.md) for the launch sequence once the number and
template arrive.
