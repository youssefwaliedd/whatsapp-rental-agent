# Getting it onto WhatsApp

Where to pick this up. `go-live.md` is the full runbook with every screen and
value; this is the short version of what is actually left, in the order it makes
sense to do it.

**Steps 1 to 3 need nobody but you.** At the end of them the agent is answering
on real WhatsApp, on Delta's real fleet, with Delta's real photographs, and you
can hand someone a number to message.

> **Never register your personal number with the Cloud API.** It removes that
> number from WhatsApp on the phone, permanently, and there is no undo. Meta's
> free test number is the development path; Delta's business number is the
> production one.

---

## 1 — Meta app and a test number · about an hour

developers.facebook.com → create an app → add the **WhatsApp** product. A test
number comes with it, free, able to message up to five numbers you nominate.
Nominate your own.

Collect four values and put them in `.env`:

    WHATSAPP_PHONE_NUMBER_ID=
    WHATSAPP_ACCESS_TOKEN=
    WHATSAPP_APP_SECRET=
    WHATSAPP_VERIFY_TOKEN=          # any string you invent, used once

`go-live.md` section 2 says exactly where each one is.

## 2 — Let Meta reach your laptop · 15 minutes

Meta will not call `localhost`. A tunnel gives it a public HTTPS address, the
same way the Stripe CLI forwarded payment events.

    brew install cloudflared
    cloudflared tunnel --url http://localhost:8000

It prints an `https://something.trycloudflare.com` address. In the Meta dashboard,
under WhatsApp → Configuration → Webhook, paste that address with `/webhook` on
the end, paste your verify token, and subscribe to **messages**.

Then, in another window:

    .venv/bin/python run_webhook.py

Message the test number from your phone. That is the moment it becomes real.

**The tunnel address changes every restart.** Fine for a demo, not for anything
left running — see step 5.

## 3 — Decide the provider before showing anyone · 30 minutes

On Gemini's free tier a reply can take fifty seconds, and the quota dies partway
through a day. Both were measured. Either would misrepresent an otherwise fast
system in front of the person you are trying to convince.

    RENTAL_AGENT_PROVIDER=anthropic
    ANTHROPIC_MODEL=claude-haiku-4-5-20251001    # set it; the default is Opus

Roughly AED 400-550 a month at their expected volume. This is also the answer to
their 2-5 second requirement, so it closes two lines of their scope at once.

## 4 — What only Delta can do

**Their WhatsApp Business number**, or an invitation to their Meta Business
Manager. Until then the test number is the whole demo.

**The `case_update` template**, requested by them in their own Business Manager,
category UTILITY, body text already written in `config/rules.json` under
`human_in_the_loop.reopen_template`. **Meta takes a day or two**, which is why it
is worth asking for before anything else on this page.

Without it: a customer messages at 11pm, the owner answers at 9am, and WhatsApp
will not let the agent deliver that answer. The reply simply never arrives.

**A number for escalations** — `WHATSAPP_STAFF_NUMBER` — pointed at whoever
actually answers, and the hours they are reachable.

## 5 — When it stops being a demo

A tunnel from a laptop is not where this runs. Railway, Render or Fly each give
you a Postgres on the same network:

- Set `DATABASE_URL` to the hosted database. That is the entire migration.
- Set `FLEET_REFRESH=1` so the nightly re-import runs on a machine that stays awake.
- On any database that already exists, the columns added since it was created:

      ALTER TABLE quotes ALTER COLUMN deposit DROP NOT NULL;
      ALTER TABLE reservations ALTER COLUMN deposit DROP NOT NULL;
      ALTER TABLE conversations ADD COLUMN IF NOT EXISTS sales_outcome VARCHAR;
      ALTER TABLE strategies ADD COLUMN IF NOT EXISTS replay_detail JSON DEFAULT '[]'::json;
      CREATE TABLE IF NOT EXISTS booking_operations (
        id SERIAL PRIMARY KEY,
        idempotency_key VARCHAR UNIQUE NOT NULL,
        provider VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        customer_ref VARCHAR,
        reference VARCHAR,
        outcome VARCHAR,
        detail JSON,
        created_at TIMESTAMPTZ NOT NULL,
        settled_at TIMESTAMPTZ
      );

  A brand new database needs none of this — `create_all` builds it correctly.
- Point Meta's webhook at the deployed URL instead of the tunnel.
- **The hosting account should be Delta's.** It will hold their customers' names,
  numbers and complete conversation histories.

## 6 — Before a real customer

The checklist in `go-live.md` section 8. The two that matter most:

- **The demo footer off and the real fleet on, together.** A real booking still
  carrying "this is a demonstration" looks broken; fictional data without it is a
  fake booking presented as real. `Operator.is_demonstration` is the single switch.
- **`WHATSAPP_STAFF_NUMBER` pointed at a person**, not a placeholder.

---

## Things that are not on this list, and why

**Hosting the photographs.** Not needed. All 336 images in `config/fleet.json`
are absolute URLs on Delta's own server, and Meta fetches image URLs itself, so
their pictures stay where they already live. `WHATSAPP_MEDIA_BASE_URL` matters
only for the fictional demo fleet.

**Payment links.** Done and verified end to end. `PAYMENT_PROVIDER=stripe` with a
`sk_test_` key gives real Checkout pages; `STRIPE_WEBHOOK_SECRET` plus
`stripe listen --forward-to localhost:8000/payments/stripe` records the payment
when it arrives.

**Availability.** Still nobody's to solve but Delta's. Until there is a source to
read, a booking is a hold and one tap from the owner confirms it.
