# Sample-data pilot validation, 7 September 2026

The later [8 September checkout corrections](checkout-flow-validation-2026-09-08.md)
add mandatory eligibility and terms gates, browser attachments and updated tests.
The evidence and limitations below describe the earlier implementation stage.

## Implemented

- JPG, PNG and PDF attachment collection in the WhatsApp conversation, with
  authenticated media downloads, file bounds and private encrypted storage.
- Automatic Gemini document extraction and code checks for unreadable images,
  missing/expired dates and inconsistent names across uploaded documents.
  Customers receive the result in chat. Extracted names are stored only as keyed
  digests for comparison. Identity numbers are not requested from the reader.
- Local encrypted storage and a private S3 backend, plus worker-driven file and
  name-digest retention. The sample retention period is 30 days.
- PostgreSQL inbound/outbound jobs, deduplication, per-customer ordering,
  exponential retries, dead-job recovery and worker readiness monitoring.
- Stable Stripe checkout idempotency keys and atomic payment-event notification
  jobs, retaining signature, amount and currency validation.
- Docker Compose, Render configuration and CI regression/PostgreSQL/build checks.

The new hosted mode requires `WHATSAPP_DURABLE=1`,
`WHATSAPP_DOCUMENT_CHECKS=auto`, encryption and a running worker. Existing local
environment settings were preserved. These changes have not been committed or
deployed.

## Evidence

- 1,205 automated tests passed in the main regression run. Two opt-in PostgreSQL
  tests were skipped there and passed separately against a temporary real
  PostgreSQL server. Four local socket tests also passed separately.
  Total: **1,211 automated tests passed across those runs**.
- Following the Gemini request-schema fix, all 43 focused document, queue,
  payment-durability and deployment tests passed again.
- Five live Gemini synthetic cases passed with `gemini-3.1-flash-lite`: clear PNG,
  expired date extraction, different-name extraction, unreadable blurred PNG and
  clear PDF. Local tests separately verify rejection and mismatch decisions.
- Live testing discovered an unsupported `additionalProperties` schema field.
  The API schema now omits that field while local parsing remains strict.
- Compilation, whitespace checks and Docker Compose configuration validation
  passed. One existing Starlette/httpx deprecation warning remains.

These five artificial samples are integration smoke tests, not a measured
accuracy benchmark for real passports or licences. No real identity documents,
customer WhatsApp messages or real charges were used.

## Remaining before a usable WhatsApp pilot

1. Complete Meta number verification, credentials, app configuration and HTTPS
   webhook subscription. Test inbound messages, replies and attachments on a phone.
2. Provision PostgreSQL, web/worker hosting and private storage. Supply secrets
   and test backup/restore, alerts and cloud storage permissions. Docker could
   not be built locally because the daemon was not running; S3 used a fake client
   in tests. The temporary PostgreSQL test server was stopped afterwards.
3. Complete a Stripe test checkout through the hosted webhook and verify the
   WhatsApp receipt. This change was checked with mocked Stripe HTTP responses
   and signed test webhook events, not a fresh hosted card-payment flow.
4. Supply the company's fleet, fees, document/driver eligibility rules and real
   reservation integration. Bookings remain simulated. Document checks do not
   establish authenticity, eligibility or booking approval.
5. Define handling for different name spellings/scripts, missing expiry dates,
   additional drivers and documents that expire during a rental. Approve document
   retention and backup/version deletion policies before using real IDs.

Replies outside the customer service window wait for a new customer message.
Proactive reminders need configured approved templates. Outbound delivery is at
least once, so a provider accepting a message before losing its response can
occasionally produce a duplicate on retry.

Configuration and recovery commands: [pilot deployment](pilot-deployment.md).

## Follow-up: pricing request failure

The browser recorded a provider-failure reply after 9.2 seconds, but its original
provider error was not retained. That event's exact upstream cause remains
unconfirmed. Investigation identified and reproduced these recovery defects:

- A 15-second cap applied to all adapter attempts together prevented another
  request after a nine-second failure, despite time left in the 30-second turn.
  Each HTTP attempt is now capped separately within the shared turn deadline.
- Timeout exceptions with empty messages could bypass retry classification.
  Classification now includes the exception type.
- Worker backoff was measured from job start. Long failed requests consumed
  their own retry delay. Backoff now starts at failure and honours provider delay.

Safe error categories and HTTP codes are retained in browser presentation records
and failed provider jobs. Provider bodies, customer prompts and credentials are
excluded from these diagnostics. The inspector displays the saved category after
a reload. Retry limits and dead-job monitoring still apply during longer outages.

Validation: 1,212 regressions passed, with two opt-in PostgreSQL tests skipped in
this follow-up run and the four socket tests excluded. Seven added tests cover
slow failover, deadline limits, rate-limit delay, empty timeout messages, diagnostic
redaction, failure-time worker scheduling and inspector persistence. A fresh live
conversation used only fictional fleet fixtures and an in-memory database. Its
pricing comparison succeeded without provider errors; its opening response was
blocked by the existing factual guard, so this is not a clean end-to-end sales test.

The saved customer conversation was not replayed to Gemini. Automatic approval
review rejected that proposed replay because its history was not proven synthetic;
the live test instead used newly generated inputs. The local tester was restarted
on port 8101 with the existing conversation database preserved.
