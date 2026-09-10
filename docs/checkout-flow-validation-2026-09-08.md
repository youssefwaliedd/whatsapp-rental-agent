# Checkout flow corrections, 8 September 2026

The server now requires the following before a reservation or payment link:

1. Current vehicle, rental dates and customer-stated pickup and return times.
2. Driver age and residency stated by the customer.
3. Uploaded, readable documents required for that residency and vehicle category.
4. Matching document names and birth date, valid expiry through the rental end,
   correct licence issuing country and configured licence history.
5. Delivery address or company-approved collection, included in the quote.
6. Known deposit, extra-mileage rate and final insurance excess.
7. Presented rental terms and explicit acceptance of that exact quote and policy.

These requirements are repeated at service entry points and before cached tool
results are reused. A model statement or simulated document flag cannot approve
the paperwork. Changing the quote, policy or document evidence invalidates its
acceptance. Saying not to book revokes acceptance and leaves checkout.

A new enquiry clears the old reservation selection. Changes to existing bookings
require the customer to state the reference and requested change. New uploads
are not attached to an unrelated old reservation. Paid reservations are not
silently requoted or charged again. Delivery changes cannot reuse acceptance of
a different time or address. Held bookings recheck prerequisites on confirmation.

## Verification

- Final regression run: 1,237 tests passed and two opt-in PostgreSQL tests were
  skipped. Four loopback-port tests passed separately. No new PostgreSQL service
  was started for this checkout-specific change. Whitespace validation passed.
- A subsequent focused run passed all 26 strict checkout tests, including a
  regression for the unsupported claim that office collection had been approved.
- Scripted agent integration covers qualification, rejection of a document-skip
  request, document checks, delivery, terms, reservation and payment link.
- Regression cases cover missing, expired and mismatched document facts, unknown
  fees, unconfirmed collection, premature acceptance, policy changes, stale
  bookings, delivery changes, hold confirmation and malformed model dates.
- Browser API checks cover upload, duplicate prevention, reload, required headers,
  file signatures and persistent encryption-key recovery.
- Live Gemini `gemini-3.1-flash-lite` correctly read a newly generated fictional
  Emirates ID PNG and UAE driving-licence PDF, including birth date, expiry date,
  issue date and issuing country. These are two integration samples, not a real
  document accuracy benchmark.
- A fresh isolated checkout passed with live Gemini document reading, the
  fictional fleet, an empty database and simulated payment links. Earlier runs
  exposed provider 503/504 errors and delivery-zone normalization dropping the
  exact address. Accepted-quote progression now runs directly through the backend
  without another model call, and quotes preserve the customer-supplied address.
  General AI conversation and document reading can still encounter provider outages.
- Safari showed the reloaded chat with the attachment button and sample-only
  notice. Existing conversation history was preserved.

## Limits and remaining inputs

The real company configuration has unconfirmed deposit, extra-mileage and final
insurance excess amounts, plus unconfirmed collection rules. It therefore blocks
checkout instead of guessing. Company confirmation of all eligibility, fee and
fulfilment rules is required. Bank-card ownership verification for categories
that require it is not configured, and card images must not be collected.

Bookings remain simulated. Payment tests in this change use a simulated provider
and existing Stripe test regression coverage, without a new hosted card payment.
Meta number verification and hosted end-to-end WhatsApp testing remain pending.
AI reading is not an authenticity or government identity verification service.
Name variations, additional drivers and document types without expiry dates need
an approved company policy before automatic handling can be expanded.

Changes are local and uncommitted. No customer history or real identity documents
were sent to Gemini in these live checks.

## Document and date recovery, 9 September 2026

Fixed the reported conversation loop: document uploads previously appended the
checkout gate, so an expired quote hid the remaining-document checklist. Ordinary
questions could also invoke quote tools against a legacy reservation whose dates
differed from the enquiry, repeatedly returning a date mismatch.

Document-type, required-document and extracted-name questions now have direct
record-backed answers before model/tool execution. Both browser and WhatsApp
upload receipts identify the detected type and show the checklist for the current
enquiry. They do not refresh quotes or create reservations. Readable names are
not retained in the structured check; names are compared using a protected
fingerprint, while the original document remains encrypted. The response now
explains this accurately.

Date-status questions distinguish enquiry dates from an existing reservation.
Date-only replies in legacy chats acknowledge the dates and explain how to start
a new booking or explicitly request a change. No paid reservation is silently
changed. Document expiry dates and questions about saved dates do not replace
rental date anchors. Changed dates cannot inherit an unconfirmed clock time.

Validation uses fictional documents and isolated databases, with no customer
history sent to a model: the strict checkout suite includes the reported question
sequence and separate browser/WhatsApp upload tests with an expired quote.

## Search recovery after AI outages, 9 September 2026

A successful search now survives an unavailable response model. The agent renders
up to three current-turn search results with their recorded daily rates, a clear
statement that no booking exists, and the demonstration availability limitation.
It retains provider diagnostics and successful tool names. Failed or empty later
searches, results from previous turns, and booking/payment attempts cannot use
this fallback. Recovery also works inside durable WhatsApp turns without forcing
an otherwise completed search to be retried.

Existing bounded Gemini retries and failover remain in place. A new transport test
simulates a 503 followed by a 504 and verifies automatic recovery on the next
attempt. Validation: 1,252 regression tests passed with two optional PostgreSQL
tests skipped; an additional browser HTTP/persistence test passed afterward.
No customer conversation was replayed to an external model.

## Age and residency answers, 9 September 2026

The bare answer `25` previously depended on model extraction when the model had
asked the age question without setting the checkout `asked` field. Explicit age
questions in the previous outbound message now establish that context. Simple
age/residency answers advance prerequisite checks without calling a model, even
before booking consent. This does not grant consent, create a quote or booking,
or bypass document checks. Tests cover a model-written age question, residency
followed by age with uploaded documents, and an unrelated bare number.

## Broader timeout resilience, 9 September 2026

- Optional extraction waits at most 250 ms when joining the conversational
  response and has its own 12-second request budget. Nested budgets cannot extend
  the parent turn deadline. Late extraction is discarded instead of blocking the
  answer or mutating state later.
- Browser state/learning reads for existing sessions no longer update customer
  activity timestamps. A concurrent SQLite writer test verifies that these reads
  succeed while a chat transaction holds the write lock. This does not make
  SQLite support multiple concurrent writers; hosted deployment still needs the
  configured PostgreSQL setup.
- Standalone rental dates/times, discussed-car price lists, best-car preference
  clarification, and straightforward checkout follow-ups have deterministic
  responses. Searches preserve the selected car and stored filters. No booking
  consent is inferred from these steps.
- Document extraction now uses the shared bounded retry implementation, remaining
  on the configured document model. HTTP attempts are capped at 15 seconds,
  nested SDK retries are disabled, and the overall budget is 30 seconds.

Validation: 1,263 regression tests passed; two optional PostgreSQL tests skipped.
Targeted tests were rerun after preserving the selected car in routine searches.
All tests used synthetic data and mocked external services. External outages can
still prevent free-form answers; the changes reduce dependencies and bound waits,
not guarantee external service availability.
