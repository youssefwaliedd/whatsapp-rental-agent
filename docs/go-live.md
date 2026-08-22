# Going live on WhatsApp

Everything in this system is built and tested against simulated Meta payloads.
What is missing is credentials. This is the path from "we have a number" to a
customer talking to the agent, in order, with a way to check each step actually
worked before moving to the next.

Budget **about 45 minutes** the first time, most of it waiting on Meta.

---

## Before you start

> **Never register a personal WhatsApp number with the Cloud API.**
>
> Doing so removes that number from the WhatsApp app on the phone. It is a
> one-way door and there is no undo. Use Meta's free test number for
> development, and the client's Business number for production.

You need one of:

- **Meta's free test number** — instant, no client involvement, can only message
  five recipients you nominate. Right for everything up to a real demo.
- **The client's WhatsApp Business number** — for production. Ask them to add you
  to their Meta Business Manager rather than sending you credentials.

---

## 1. Create the Meta app — 10 minutes

1. Go to <https://developers.facebook.com/apps> and sign in.
2. **Create App** → type **Business** → name it (the client will not see this).
3. On the app dashboard, find **WhatsApp** and click **Set up**.
4. It will ask you to select or create a **Business portfolio**. For a test
   number, your own is fine. For production this must be the client's.

You now have a **WhatsApp → API Setup** page. Leave it open; the next three
values come from it.

---

## 2. Collect four values

Three come from Meta, one you invent. Put them in `.env` at the repo root —
which is gitignored, and where they must stay.

| Setting | Where it comes from |
|---|---|
| `WHATSAPP_PHONE_NUMBER_ID` | **API Setup** page. A long number, *not* the phone number itself |
| `WHATSAPP_ACCESS_TOKEN` | **API Setup** → temporary token. **Expires in 24 hours** — see step 6 |
| `WHATSAPP_APP_SECRET` | **App settings → Basic → App secret**, click Show |
| `WHATSAPP_VERIFY_TOKEN` | Any string you make up. It only has to match in two places |

Two more, both optional but both worth setting:

| Setting | What it does |
|---|---|
| `WHATSAPP_STAFF_NUMBER` | Where escalations go. Full international form, no `+`, e.g. `971501234567` |
| `WHATSAPP_MEDIA_BASE_URL` | Public base URL for vehicle photos. Without it, photos are skipped and logged |

```
WHATSAPP_PHONE_NUMBER_ID=123456789012345
WHATSAPP_ACCESS_TOKEN=EAAG...
WHATSAPP_APP_SECRET=a1b2c3...
WHATSAPP_VERIFY_TOKEN=delta-rentals-2026
WHATSAPP_STAFF_NUMBER=971566699228
```

**Check it took:**

```bash
.venv/bin/python run_webhook.py
curl localhost:8000/health
```

`missing_settings` should be empty and `signature_verification` should be
`true`. If a value is present in `.env` but still listed as missing, the file is
not being read — check you are in the repo root.

---

## 3. Give Meta a public URL — 2 minutes

Meta must reach your machine over HTTPS. In a second terminal:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a URL like `https://something-random.trycloudflare.com`. Free, no
account, and it changes every time you restart it — fine for testing, and step 8
covers production.

Leave both terminals running.

---

## 4. Point Meta at it — 5 minutes

On the app dashboard: **WhatsApp → Configuration → Webhook → Edit**.

- **Callback URL:** your tunnel URL with `/webhook` on the end
- **Verify token:** exactly what you put in `WHATSAPP_VERIFY_TOKEN`

Click **Verify and save**. Meta sends a GET immediately and the app echoes the
challenge back. If it fails, the token does not match or the tunnel is down.

Then **Manage** the webhook fields and subscribe to **`messages`**. This is
easy to miss and nothing arrives without it.

**Check it took:** the terminal running `run_webhook.py` logs the verification
request. Nothing in the log means Meta never reached you.

---

## 5. Send the first message — 2 minutes

On the **API Setup** page, add your own phone number under **To**. A test number
can only message recipients nominated there.

Message the test number from your phone. You should see, in order:

- the webhook log recording the inbound message
- two blue ticks and *typing…* on your phone
- a reply from the agent

**If nothing arrives**, work backwards: does the webhook log show the request?
If not it is the tunnel or the subscription. If yes but no reply, the log will
carry the exception.

---

## 6. Replace the temporary token — 10 minutes

The dashboard token dies after 24 hours and takes the bot with it, silently, at
whatever hour it was issued.

1. **Business Settings → Users → System users → Add**, name it, role **Admin**.
2. **Add assets** → your app → toggle **Manage**.
3. **Generate new token** → select the app → scopes `whatsapp_business_messaging`
   and `whatsapp_business_management` → choose **Never expires**.
4. Put it in `WHATSAPP_ACCESS_TOKEN` and restart the webhook.

Do this before any client demo. A demo that dies because a token expired
overnight is not recoverable in the room.

---

## 7. Get the template approved — minutes to a few hours

Required, and it is the one thing with a waiting time, so submit it early.

**WhatsApp → Message templates → Create template**

- **Category:** Utility
- **Name:** `case_update` — must match `rules.json` exactly
- **Language:** English
- **Body:**
  `Hi {{1}}, I have an update on the issue you raised with us. Reply to this message and I'll go through it with you.`

Without it, an owner answering an escalation more than 24 hours after the
customer last wrote will have the reply **rejected by Meta**, the case marked
resolved, and the customer told nothing. The system handles this correctly — but
only if the template exists.

If the client names it something else, change `human_in_the_loop.reopen_template.name`
in `rules.json` to match.

---

## 8. Before real customers

Everything above gets you a working bot on a test number. These are the things
that separate that from something a company can rely on.

- [ ] **A permanent URL.** The tunnel dies with the terminal. Deploy the app
      somewhere with a fixed hostname and update the webhook URL once.
- [ ] **`DATABASE_URL` on a hosted Postgres.** SQLite serves customers one at a
      time — measured at 10.7s for ten simultaneous customers against 1.1s on
      Postgres. Re-run `tests/concurrency_check.py` against the real database.
      The account should belong to the client; it holds their customers' data.
- [ ] **A paid model tier.** The free tier exhausts mid-day and every customer
      after that gets "send that again" until it resets.
- [ ] **`WHATSAPP_MEDIA_BASE_URL`** pointing somewhere public, or no photos.
- [ ] **`WHATSAPP_STAFF_NUMBER`** pointing at whoever actually answers
      escalations — not a number nobody watches.
- [ ] **The demo disclosure decision.** Every quote and confirmation currently
      carries "This is a demonstration booking." Real fleet with that footer
      still on looks broken; fictional fleet with it off is a fake booking
      presented as real. They flip together, once, deliberately.

---

## When something is wrong

| What you see | Where to look |
|---|---|
| Webhook verification fails | Verify token mismatch, or the tunnel is down |
| Meta accepts the webhook, nothing arrives | The `messages` field is not subscribed |
| Inbound logged, no reply | The webhook terminal has the exception |
| Replies stopped after a day | Token expired — step 6 |
| Photos never appear | `WHATSAPP_MEDIA_BASE_URL` unset, or not publicly reachable |
| Owner's decision never reaches the customer | 24-hour window; check the template exists and its name matches |
| Half of simultaneous customers get nothing | Still on SQLite — set `DATABASE_URL` |

`curl localhost:8000/health` answers most of the configuration questions
directly.

---

## Testing without any of this

None of the above is needed to exercise the agent:

```bash
.venv/bin/python run_whatsapp_sim.py    # the real webhook, only Meta faked
.venv/bin/python run_chat.py            # the sales conversation, in a browser
```

The simulator drives the real webhook with genuine signed Cloud API envelopes,
so anything that works there works on a number.
