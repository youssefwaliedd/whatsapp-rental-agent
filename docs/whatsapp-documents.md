# Documents sent in WhatsApp

Customers attach JPG, PNG or PDF files to the same WhatsApp conversation. There
is no external upload page. The webhook downloads each attachment from Meta
with authentication, validates its size, type and hash, and stores it outside
the web server's public assets. The application limit is 10 MB per attachment;
Meta may enforce a lower limit for a particular media type.

A successful download creates a receipt. With `WHATSAPP_DOCUMENT_CHECKS=auto`,
the dedicated Gemini reader checks readability, expiry and name consistency.
Code records `checks_passed` or `needs_replacement`, and the bot replies in the
same chat. These checks run without routine staff review. The checkout service
then checks the configured document set, age, issuing country, licence history,
name consistency and validity through the rental end date. Company approval of
these rules is still required for real use. Reading a document does not prove
identity authenticity, forgery detection or the driver's physical identity.

Only the dedicated document reader receives file bytes. Ordinary conversation
prompts receive document references and statuses, without file contents or
storage locations. Attachments are handled even during an existing handover.
In durable mode, temporary download/provider failures retry; permanent download
failures ask the customer to resend and do not count as received paperwork.

See [pilot setup](pilot-deployment.md) for the automatic mode, encrypted storage,
retention, worker and deployment configuration.

## Optional legacy staff review

When automatic checks are disabled, receipts remain `pending_review` and the
existing staff workflow is available:

Set `WHATSAPP_STAFF_NUMBER` to the authorised reviewer number. From that number,
send these messages to the bot:

- `DOCS`: list the oldest ten attachments awaiting review.
- `DOC DOC-<reference> VIEW`: receive the file privately in WhatsApp, together
  with the customer, linked reservation if one exists, and available document types.
- `DOC DOC-<reference> APPROVE passport`: record approval for the specified type.
- `DOC DOC-<reference> REJECT The image is blurry`: request a replacement.

Use the complete `DOC-...` reference returned by the bot. The reviewer must
request and successfully receive a review copy before recording a decision.
Delivery of a review copy does not prove it was read; staff are responsible for
checking identity, validity, completeness and rental eligibility. Approval is
per attachment and does not confirm a reservation, complete all paperwork, or
authorise a payment. There is no model tool that can approve these attachments.

The review decision is sent immediately when the customer's reply window is
open. Otherwise it stays pending until the customer messages again. Failed
decision delivery also stays pending. This feature does not schedule reminders
or initiate template messages outside the window. Staff check the queue using
`DOCS`; automatic staff notifications are not part of this implementation.

## Storage and operation

- `WHATSAPP_DOCUMENT_DIR` selects a private persistent directory. The default
  `.private_documents` directory is excluded from Git. Directories use mode
  `0700` and files use mode `0600`, with random server-generated filenames.
- No public document route is mounted. Set `DOCUMENT_ENCRYPTION_KEY` to enable
  application encryption. `DOCUMENT_STORAGE=s3` uses a private bucket and requires
  this key. Production validation requires encryption for local storage too.
  Web and worker must share their storage and encryption configuration.
- The new `customer_documents` table records the owning customer/conversation,
  optional current reservation, file receipt and staff review. Existing tables
  do not require a destructive migration; `init_db` creates the additional table.
- `document_facts` stores encrypted birth, expiry and issue dates and issuing
  country. Ordinary conversation prompts do not receive these facts. The checkout
  service decrypts them to enforce the rules. `document_checks` holds the keyed
  name digest and automated check result.
- The worker automatically deletes stored files, encrypted facts and name digests after
  `DOCUMENT_RETENTION_DAYS` (30 for sample testing). Establish the company policy
  before accepting real IDs. Backups, S3 historical versions and copies received
  by staff need separate deletion policies.
- All incoming webhooks are refused when the Meta app secret is missing. This
  protects customer uploads and staff commands that access private files.
- This is still a demonstration deployment. Use fictional sample attachments.
  Live WhatsApp collection and staff delivery require the registered number,
  API credentials and a reachable webhook. No real identity documents were used
  in the implementation tests.

Set `WHATSAPP_DURABLE=1` and run `run_worker.py` to enable the PostgreSQL inbox,
outbox and retries. Without that setting, the legacy in-process background path
remains available for local compatibility. Hosted pilot configuration requires
durable mode. Delivery is at least once; an ambiguous provider response can cause
a duplicate message on retry.

## Local browser testing

The chat at `http://127.0.0.1:8101/` has a plus attachment button accepting PDF,
JPG and PNG up to 10 MB. It uses the same storage and document checker. Reload
the HTTP page to get the button; opening `index.html` directly is unsupported.
When no encryption key is configured, the local tester creates a private key
in `.private_documents/.encryption-key` and restores it after a restart.
This local harness is not a public upload service.

Fictional files are available in `tests/fixtures/sample_documents/`. The driver
is 31 during 2026 and a UAE resident. Real company fee gaps still block checkout
even when those sample documents pass. Test fixtures contain fictional complete
fees for automated successful-payment scenarios.
