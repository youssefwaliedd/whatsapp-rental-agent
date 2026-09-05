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
