# WhatsApp pilot setup

This build supports synthetic documents, automatic document checks, a PostgreSQL
inbox/outbox and Stripe test payments. Bookings remain simulated. It is not yet
approved for real customer IDs or real charges.

## What runs

- Web: `uvicorn run_webhook:app --host 0.0.0.0 --port 8000`.
- Worker: `python run_worker.py`. Run one worker initially; PostgreSQL locking also
  supports multiple workers without overtaking another message from the same customer.
- PostgreSQL: shared by web and worker. It holds durable jobs, so Redis is not
  required by this implementation. This supersedes the separate Redis proposal
  in the earlier hosting brief.
- Documents: an encrypted shared filesystem for Docker Compose, or encrypted
  objects in a private S3 bucket for Render. Web and worker must share the same
  encryption key and storage configuration.

## Configuration

Install `requirements.txt`. Use `.env.example` as the settings reference. Configure
`WHATSAPP_DURABLE=1`, `WHATSAPP_DOCUMENT_CHECKS=auto`, and a Fernet key generated with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Save the result as `DOCUMENT_ENCRYPTION_KEY` in secret settings, never Git. Back up
this key separately. Losing it makes the documents unreadable. Do not rotate it
without migrating existing objects. `DOCUMENT_MATCH_KEY` optionally supplies a
separate stable secret for name matching; otherwise the encryption key is used.

Legacy unencrypted files are not silently accepted by the encrypted backend.
Use a fresh isolated pilot database and storage directory, or explicitly migrate
old files before switching an existing deployment. Existing `.env` and customer
files were not changed by this implementation.

Set `DOCUMENT_RETENTION_DAYS=30` for sample testing. The worker deletes retained
files and stored name digests after that interval; the company must approve the
actual retention policy. Configure backup retention, S3 version expiry and any
copies already sent to staff separately. Deleting a current S3 object does not
automatically delete historical versions or backups.

### Docker Compose

Set a generated URL-safe `POSTGRES_PASSWORD`, Gemini credentials, Meta settings
and the document key in `.env`. Stripe can stay simulated or use `PAYMENT_PROVIDER=stripe`
with `STRIPE_API_KEY=sk_test_...`, a webhook secret and HTTPS return URLs.

```sh
docker compose up --build -d
```

PostgreSQL is private to the container network. The web port is bound only to
localhost. Configure a public HTTPS reverse proxy or a development tunnel to
forward to port 8000. `APP_ENV=production` deliberately refuses live Stripe keys
in this synthetic pilot.

### Render

`render.yaml` defines web, worker and PostgreSQL. Its `rental-pilot` environment
group generates a shared document encryption key. Add the following secrets to
that group before expecting the processes to start successfully:

- `GEMINI_API_KEY`.
- `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`,
  `WHATSAPP_VERIFY_TOKEN`.
- `DOCUMENT_S3_BUCKET`, `AWS_DEFAULT_REGION` and scoped AWS credentials, or a
  supported workload role. Optional `DOCUMENT_S3_ENDPOINT` must be HTTPS.
- `STRIPE_API_KEY` using a test key, `STRIPE_WEBHOOK_SECRET`,
  `PAYMENT_SUCCESS_URL` and `PAYMENT_CANCEL_URL` using actual HTTPS return pages.

Block all public bucket access. Restrict the storage identity to list the
`documents/` prefix and get, put and delete objects under that prefix. Enable
bucket encryption and lifecycle rules for historical versions. This code writes
application-encrypted bytes and requests AES256 server-side encryption; no public
ACL or document URL is created. Cloud credentials and bucket access still need
an actual integration test.

## WhatsApp and Stripe

Register and verify the Meta number, publish as required by the app dashboard,
configure the HTTPS `/webhook` callback and matching verification token, and
subscribe to messages. Connect Stripe's `/payments/stripe` endpoint using the
correct signing secret. These account steps remain necessary before actual
WhatsApp testing.

Stripe signature, amount, currency, checkout reference, duplicate event and
refund-status checks already exist. Checkout creation now uses a stable Stripe
idempotency key for repeated identical requests. The payment change and its
notification job commit together. A website redirect is never proof of payment.
Use only Stripe's published test payment details in test mode.

## Document behaviour

Only the dedicated document reader receives the attachment; ordinary chat history
and tool prompts receive a reference and status. Filenames and captions do not
approve a document. Gemini extracts type, readability, full name, expiry and
confidence. Code checks image bounds, missing/expired dates, confidence and exact
normalized name agreement across previously passing documents. Names are compared
through a keyed digest; raw extracted names and identity numbers are not stored
in the check table.

- `checks_passed`: the limited automatic checks passed.
- `needs_replacement`: ask for a clearer, current or matching upload.
- `pending_review`: legacy staff mode, when automatic checks are disabled.
- `deleted`: retention removed the stored document.

Automatic checks do not prove authenticity, confirm rental eligibility, change a
booking, or authorise payment. The first document has no second name to compare.
Transliteration, different scripts, alternative driver names and documents without
expiry need an explicit policy. A model's confidence is not a calibrated identity
verification score. The company eligibility rules still need to be supplied.

## Reliability and operations

The webhook acknowledges only after committing inbound jobs. The worker commits
conversation changes and outgoing payloads in one transaction. A lost connection
or process crash rolls back partial work. Provider failures retry with exponential
backoff. Six failed attempts leave a dead job, preserve its payload for recovery,
and block later work in that lane. Other customers can continue.

`/health` is process liveness. Monitor `/ready` separately: it returns 503 if the
database is unavailable, the worker heartbeat is older than three minutes, or a
job has exhausted retries. Configure an external alert on that endpoint.

```sh
python run_worker.py --failed
python run_worker.py --retry JOB_ID
```

A reply whose 24-hour service window has closed waits for a new customer message.
No unapproved template is fabricated. Outbound delivery is at least once: Meta
can accept a message before the network loses its response, so a retry can rarely
duplicate a sent message. Successful queue payloads are cleared; deduplication
keys remain. Dead/pending job payloads require an operational retention policy.

The worker runs hold/outcome maintenance and document retention without waiting
for incoming messages. It does not invent company-specific marketing campaigns
or scheduled reminder templates.

## Tests

```sh
python -m pytest -q
TEST_POSTGRES_URL=postgresql+psycopg://USER:PASS@HOST/TEST_DB python -m pytest tests/test_postgres_jobs.py -q
python -m tests.live_document_check
```

The PostgreSQL test creates and drops its own randomly named schema and terminates
only a connection it creates. The live document script explicitly calls Gemini
with generated fictional images. It is not part of the ordinary offline suite.
The GitHub workflow runs regressions, PostgreSQL checks and a container build.
No tests send customer WhatsApp messages or charge a real card.
