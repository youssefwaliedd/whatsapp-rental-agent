# Chatbot fixes and launch readiness

Reviewed against `Whatsapp Bot.docx` supplied on 5 September 2026. The document
is a requirements reference. This report covers the application in
`WhatsApp Rental Agent copy`; a server launched from the original directory
will not pick up these changes.

The prototype is substantially better protected, but it is not ready for
unattended customer bookings. Real inventory integration and operator decisions
remain necessary, and the configured model's daily quota interrupted live tests.

## Changes

| Failure observed | Implemented correction |
|---|---|
| Flight date changed when arrival time was supplied later | Persist partial dates, preserve them during time updates, accept explicit corrections and duration changes. A clock time alone no longer becomes tomorrow. |
| Budget or colour mismatch described as a booked-out car | Search distinguishes preference exclusions, unavailable inventory and unknown availability. |
| Earlier vehicle preferences survive a change of mind | Explicit switches clear the old model, make, category and unrestricted colour filters. |
| Urgent incident downgraded by a later model decision | Compound reasons retain urgency. Clear injury, fire and smoke reports receive fixed emergency guidance without a model call. An unresolved emergency does not restart sales. |
| Unknown fees promised after supplying dates | Explicit missing-fee questions create an answer request. Staff notification is triggered without pausing the sales conversation. |
| Invented prices pass using a budget, count, unrelated car or daily rate | Input arguments and metadata cannot support monetary claims. Added checks for wrong-vehicle prices, deposit evidence and Arabic monetary notation. Customer budget echoes remain permitted. |
| A corrected reply bypasses an earlier check | Every rewrite runs through the combined checks; failed proposals remain visible to the evaluator. |
| Cancellation receipt replaced with “nothing booked” | Cancellation language is distinguished from booking confirmation; a successful cancellation returns a receipt from the actual service result. |
| Colour and year inferred from image filenames | Detail responses no longer expose image filenames to the model. Unknown-specification claims are checked, and photo captions use the catalogue vehicle name. |
| Free delivery advertised without its hour restriction | Added delivery-claim checks and a location fallback that retains the known office address. |
| Rejected summary discards a valid quote | Preview quotes now produce an engine-rendered breakdown that survives rejected prose or a subsequent provider failure. |
| Long retries and invalid Gemini deadlines | Shared turn budget, bounded SDK timeout, SDK retries disabled, exhausted daily quotas skipped, no new request when less than Gemini's minimum deadline remains. |
| Arabic booking deferral lacks a deterministic gate | Added common Arabic “do not book yet” phrases. |
| Evaluator misses rejected drafts or mislabels legitimate escalation | Persist outbound validation findings; corrected cancellation, explicit colleague request, budget-echo and emergency-number handling. |

These checks cover the reproduced patterns. They are not a proof that every
possible phrasing, language or model output will be handled correctly.

## Verification

- Automated suite: **1,086 passed** (1,082 in the main run and four local-port
  tests separately). This includes 67 added regression cases. The only warning
  was an existing Starlette/httpx deprecation warning.
- All tests use isolated databases, fictional fixtures and simulated payment or
  booking providers. WhatsApp delivery tests use a recording transport.
- The booking demonstration completed its five scenes: successful booking,
  inventory changing before confirmation, competing requests for one car,
  provider timeout/retry, and another customer's reference.
- Live Gemini retests used synthetic customers and separate SQLite databases.
  No real WhatsApp messages, customer records, payments or reservations were used.
- Live evidence confirmed the AED 3,567.90 two-day G63 quote, preservation of the
  15 September flight date, unknown-fee case creation, corrected delivery-hour
  wording, photos without an invented year, and emergency handling.
- The model still attempted an unsupported reservation claim. The guard blocked
  it. Further wording variants and an overly generic location fallback found
  during retesting were fixed and covered by local regressions.
- Daily quota prevented completion of the latest live booking-confirmation and
  cancellation journey. Those paths passed local tests; they are not claimed as
  fully live-verified after the final changes.
- Response time remains above the requested 2–5 seconds on several successful
  turns. A bounded timeout limits waiting; it does not establish the target.

Temporary audit transcripts and tool traces are in `/tmp/rental-chat-audit`,
`/tmp/rental-chat-audit-v2`, `/tmp/rental-chat-audit-v3` and
`/tmp/rental-chat-audit-v4`. They may be removed by the operating system.

## What is needed for a working production chatbot

1. **An authoritative fleet and booking source, plus its adapter.** The 113-car
   catalogue is imported from a public site. Real availability, booking writes,
   amendments and cancellations are not connected. `booking_provider/delta.py`
   is an intentional stub. Supply API access and inventory identifiers, then
   implement atomic reservation, idempotency and timeout reconciliation. If the
   source cannot settle a booking, keep human confirmation in the flow.
2. **Approved business data.** Resolve the 12 entries in
   `config/rules.json::_pending_confirmation`, including deposits, no-deposit
   fees, extra mileage, outside-Dubai delivery, and weekly/monthly terms.
   Confirm office collection and the source conflicts recorded in the policy
   questionnaire. Maintain pricing and specifications in an approved source.
3. **Model capacity and acceptance testing.** Configure adequate provider quota,
   then complete the English/Arabic live regression matrix, sustained-load
   latency testing and failure recovery. Quota, latency and wording quality are
   still launch blockers for an unattended service.
4. **WhatsApp and staff operations.** Configure the business number, Meta
   credentials, public HTTPS webhook, staff number and required approved
   templates. Test an actual customer-to-bot-to-staff-to-customer exchange,
   including an answer outside the messaging window and failed delivery.
5. **Payment and document workflows.** Configure the operator's payment account,
   validate checkout/webhook reconciliation in test mode, and verify that payment
   state agrees with booking state. Agree how eligibility documents are checked
   before delivery. Current demonstration document handling is not verification.
6. **Production operation.** Deploy the selected build with a maintained
   database, migrations, backups and access controls. Add durable inbound work
   and outbound/staff-notification retry handling: the webhook currently uses
   in-process background tasks, which can be lost on restart. Test restart,
   duplicate-message and concurrent-customer recovery before launch.
7. **Resolve the CRM option in the document.** The present implementation has
   one staff escalation number. Integration with the existing CRM, or a custom
   inbox with staff availability and automatic assignment, requires a chosen
   workflow and further integration work. These are alternatives in the brief.

Conversation logs, rule-based evaluation and reviewed lesson promotion already
exist. Phase 2's broader analytics and prompt/knowledge management need additional
work. Fine-tuning is a later option, not a prerequisite for Phase 1. The system
does not automatically rewrite its business rules or train its model weights.


## Manual testing follow-up

The user reported that “okay i like this. what are the fees”, after seeing
CLA250 and C200 photos, generated an unrelated S500 L quote. No reservation
was created, but the selection and fee wording were incorrect.

- Track the currently presented cars separately from historical search results.
- Ask which car a vague reference means when multiple options remain.
- Reject preview quotes, persisted quotes and bookings for a conflicting vehicle.
- Remember an explicit car choice for the next turn, including ordinal choices.
- Treat interest and fee questions as insufficient booking consent.
- Clear old vehicle and quote preferences on “nevermind”.
- Detect general fee questions and the observed “confirmed figure at the next
  step” wording; create a staff answer request for missing fees.

Fourteen additional regression cases pass. A replay of the reported conversation
in an isolated database asked “Mercedes CLA250 or Mercedes C200?” with no model
call, quote or booking. The live Gemini follow-up for the explicitly chosen
CLA250 opened an unconfirmed-fee request without a wrong-car quote or a promise
of an amount at a later step. The replay had no provider error.


## Option-count follow-up

The reply announcing three Urus options while listing two now has its count
corrected from the visible vehicle rows before delivery. Prices, dates, seat
counts and earlier inventory totals are preserved. The same correction runs on
rewritten replies and adds no model requests. The exact recorded browser reply
replayed successfully, with only “three” changed to “two”.

Fifteen additional option-count regression cases pass. The latest main suite
passes 1,097 tests; the four local-port tests were last verified during the
preceding fix.

## Payment, conversation and preview follow-up (6 September 2026)

Implemented:

- Explicit, simple English/Arabic payment requests for an existing booking now
  return an engine-generated payment receipt without depending on a model reply.
  Requests that also change rental details use the normal conversation flow.
- Outbound validation catches immediate promises to send links, quotes, photos or
  availability later when there is no scheduled follow-up. The prompt requests
  completed actions or a question for the specific missing information.
- Stripe callbacks reconcile the issued checkout, amount, currency, payment mode,
  booking status and payment purpose. Unmatched receipts cannot settle a booking;
  mismatched or additional receipts are recorded for staff review. A holding or
  deposit payment does not mark the rental total paid. Processor mode cannot use
  the simulation tool to mark a payment authorised.
- Duplicate receipts do not produce duplicate payment records or messages. Failed
  and expired checkout events cannot overwrite a received payment. Partial/full
  refund notifications update the payment record; paid cancellations create a
  refund review rather than claiming that money was returned. Refund execution
  remains a staff action in Stripe, subject to the operator's decision.
- Replacement Stripe links expire the previous checkout. Completed checkouts
  require reconciliation before issuing another link. Simulated bookings cannot
  collect real money with a live Stripe key; unknown provider names fail closed.
- The browser accepts signed callbacks at `/payments/stripe` and verifies an
  authenticated Stripe read at `/payments/return?session_id=...`. A redirect or
  customer statement alone never marks payment received. `run_chat.py` supplies
  return URLs from `CHAT_BASE_URL` or `CHAT_PORT` unless explicitly overridden.
- A new additive `message_presentations` table stores response parts, quote cards,
  photographs and reactions. Refresh uses original timestamps and date separators,
  preserves formatting and checkout URLs, and displays new payment receipts via
  polling. Old messages retain their text/timestamps; media never saved by the old
  build cannot be restored from that transcript alone.

Verification:

- Full suite: 1,141 passed, including local socket tests. After the final mixed
  request guard and receipt checks, all 126 affected tests passed, including the
  additional mixed-request regression.
- Live Stripe test account: stored rental amount matched checkout; authenticated
  return of an unpaid session stayed unpaid; replacement expired the previous
  checkout. No real payment was submitted.
- Live Gemini: English and Arabic requests without a booking asked for missing
  rental details without creating a booking or leaving an empty promise.
- Browser: photographs, quote cards, bold/italic text, exact checkout hrefs and
  original timestamps survived refresh. A signed synthetic payment notification
  appeared without customer input and survived another refresh; no browser errors.

Operational limits remain: real inventory/booking integration, approved business
fees, production infrastructure and public payment-webhook delivery. Local
checkout return verification covers a customer returning to this browser, while
reliable background payment updates require Stripe to reach the webhook. Existing
links retain their original return URLs; generate a new link to use the new page.

## Documents attached directly in WhatsApp (6 September 2026)

- Customers send JPG, PNG or PDF attachments in the chat, with no upload link.
  Authenticated Meta downloads validate type, signature, size and SHA-256.
  Files use private local storage with random filenames and restricted permissions.
- Receipts are distinct from staff approval. Failed downloads request a resend;
  duplicate webhook delivery does not store another file. Pending staff handovers
  do not discard incoming attachments. File contents, filenames and captions are
  not sent to the language model by this collection path.
- The configured staff number can use `DOCS`, `DOC <reference> VIEW`,
  `DOC <reference> APPROVE <type>`, and `DOC <reference> REJECT <reason>`.
  Staff must receive a review copy before recording a decision. Customer messages
  and the demonstration document tool cannot approve these files.
- Review outcomes are persisted separately from payment and reservation status.
  Notifications outside the customer reply window wait for customer input.
- All WhatsApp POST webhooks now fail closed without an app secret, protecting
  both attachment ingestion and staff access to private files.
- Verification: **1,166 tests passed**, including 24 new document cases and the
  local socket tests. All document files and Meta responses were synthetic;
  no real identity files or WhatsApp messages were sent. Existing Starlette/httpx
  deprecation warning remains.

See `docs/whatsapp-documents.md` for staff commands and storage configuration.
Real WhatsApp testing remains blocked on phone verification/configuration.
Production storage encryption, retention/deletion policy, durable processing and
automatic staff notifications still require work. The demonstration asks for
fictional sample documents only.
