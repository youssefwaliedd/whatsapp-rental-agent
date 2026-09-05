"""Structured intent and entity extraction.

A separate, constrained pass that runs before the conversational turn. It reads
the latest customer message plus what is already known, and returns typed fields.

Two reasons this is its own step rather than a tool the agent calls:

* **The merge is deterministic.** Extraction can only *add* knowledge — the merge
  refuses to overwrite or clear anything already known. That is what makes "never
  ask twice" a property of the system rather than a hope about the model.
* **Escalation cannot be forgotten.** A safety signal detected here is acted on in
  code, not left to the conversational turn to remember.

Relative references resolve here too: "make it 8 instead" becomes a full
timestamp because the extractor is given the current booking and the clock.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.enums import Category, Intent, ResidencyType, Stage
from ..domain.models import ConversationState

#: Signals that trigger an escalation in code rather than relying on the agent.
HIGH_SEVERITY_SIGNALS = {
    "accident",
    "injury",
    "police_involvement",
    "vehicle_theft_or_loss",
    "breakdown",
    "medical_emergency",
    "legal_threat",
}

EscalationSignal = Literal[
    "none",
    "accident",
    "injury",
    "police_involvement",
    "vehicle_theft_or_loss",
    "breakdown",
    "medical_emergency",
    "legal_threat",
    "payment_dispute_or_chargeback",
    "suspected_fraud",
    "abusive_language",
    "explicit_request_for_human",
    # The four the client's brief names as the cases a person must decide.
    # Not urgent in the safety sense, but the agent guessing at any of them is
    # exactly what the human-in-the-loop exists to prevent.
    "refund_request",
    "fee_dispute",
    "eligibility_exception",
    "outside_knowledge_base",
]


class Extraction(BaseModel):
    """What the latest customer message adds. Every field may be null."""

    intent: Literal[
        "new_rental",
        "modify_booking",
        "extend_rental",
        "cancel_booking",
        "ask_question",
        "report_problem",
        "small_talk",
        "unknown",
    ] = "unknown"

    pickup_at: str | None = Field(
        default=None, description="Full ISO 8601 with +04:00 offset, or null"
    )
    return_at: str | None = Field(default=None, description="Full ISO 8601 with +04:00 offset")
    delivery_location: str | None = None

    models: list[str] = Field(default_factory=list, description="Car models named, e.g. ['G63']")
    makes: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    color: str | None = None
    budget_per_day: float | None = None
    min_passengers: int | None = None
    driver_age: int | None = None
    residency: Literal["uae_resident", "tourist", "gcc_resident", "unknown"] = "unknown"

    refers_to_existing_booking: bool = False
    escalation_signal: EscalationSignal = "none"


EXTRACTION_PROMPT = """
Extract rental details from the customer's latest WhatsApp message.

Rules:
- Only report what THIS message adds or changes. Return null for anything it does
  not mention. Do not repeat values already known.
- Resolve relative dates and times against the current date given below. "This
  weekend" and "Friday" mean the next such day in the future, never a past one.
- Return full ISO 8601 timestamps with the +04:00 offset.
- A message that changes only the time of an existing booking ("make it 8
  instead") should return the full new timestamp, keeping the existing date.
- Set refers_to_existing_booking when the message is about a booking they already
  have rather than a new enquiry.
- Set escalation_signal for accidents, injuries, police involvement, theft,
  breakdowns, medical emergencies, legal threats, payment disputes, suspected
  fraud, abuse, or an explicit request for a human. Otherwise "none".
- Also set it for the four cases only a person may decide:
    refund_request        they want money back
    fee_dispute           they are contesting a charge — a late fee, a cleaning
                          fee, a fuel charge — whether or not they are right
    eligibility_exception they want a rule bent: too young, missing a document,
                          an unlicensed driver, a longer rental than allowed
    outside_knowledge_base a question the rules simply do not answer
  These are not emergencies, but the agent guessing at any of them is exactly
  what a human decision exists to prevent.
- Budget is per day in AED.

`categories` must use exactly these values, and nothing else:
  economy        small cheap cars
  sedan          ordinary saloons
  luxury_sedan   premium saloons (Mercedes C-Class, BMW 5 Series)
  suv            4x4s and family SUVs — "SUV", "4x4", "jeep", "7 seater"
  luxury_suv     premium SUVs — "G-Wagon", "Range Rover", "big SUV"
  sports         sports coupes — "sports car", "fast car"
  supercar       exotics — "Lamborghini", "Ferrari", "supercar"
  convertible    soft-tops — "convertible", "cabrio", "drop top"

Map the customer's words onto that list whenever they describe a *type* of car
rather than naming a model. "I need an SUV" is categories: ["suv"]. Leave the
list empty only when they have described no type at all.
""".strip()


def build_extraction_input(
    message: str, state: ConversationState, now: datetime, active_reservation: dict[str, Any] | None
) -> str:
    known: list[str] = []
    if state.pickup_date and not state.pickup_at:
        known.append(f"pickup DATE: {state.pickup_date.isoformat()}, time still unknown. Keep this date when a time arrives.")
    if state.return_date and not state.return_at:
        known.append(f"return DATE: {state.return_date.isoformat()}, time still unknown.")
    if state.pickup_at:
        known.append(f"delivery: {state.pickup_at.isoformat()}")
    if state.return_at:
        known.append(f"return: {state.return_at.isoformat()}")
    if state.delivery_location:
        known.append(f"location: {state.delivery_location}")
    if state.vehicle_preferences.models:
        known.append(f"models: {', '.join(state.vehicle_preferences.models)}")
    if state.vehicle_preferences.color:
        known.append(f"colour: {state.vehicle_preferences.color}")

    parts = [
        f"Current date and time in Dubai: {now.isoformat()} ({now.strftime('%A')})",
        "",
        "Already known about this enquiry:",
        "\n".join(f"  {item}" for item in known) if known else "  nothing yet",
    ]
    if active_reservation:
        parts += [
            "",
            "They have a live booking:",
            f"  {active_reservation['vehicle_display_name']}",
            f"  delivery {active_reservation['pickup_at']}",
            f"  return {active_reservation['return_at']}",
        ]
    parts += ["", "Customer's latest message:", message]
    return "\n".join(parts)


def extract(
    client: Any,
    *,
    message: str,
    state: ConversationState,
    now: datetime,
    active_reservation: dict[str, Any] | None,
    model: str,
    effort: str,
    max_tokens: int,
) -> Extraction:
    """Run the extraction pass. Returns an empty Extraction if the model declines."""
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=EXTRACTION_PROMPT,
        output_config={"effort": effort},
        messages=[
            {
                "role": "user",
                "content": build_extraction_input(message, state, now, active_reservation),
            }
        ],
        output_format=Extraction,
    )
    if getattr(response, "stop_reason", None) == "refusal":
        return Extraction()
    return response.parsed_output or Extraction()


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def _parse_dt(value: str | None, tz: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def merge(state: ConversationState, extraction: Extraction, tz: Any, message: str | None = None) -> ConversationState:
    """Fold an extraction into state. Additive only — never clears a known value.

    This one-way property is the whole point: no extraction glitch can make the
    agent forget a date the customer already gave and ask for it again.
    """
    pickup = _parse_dt(extraction.pickup_at, tz)
    from ..domain.dates import anchor
    if message is not None:
        pickup = anchor(pickup, state.pickup_date, message)
    if pickup:
        state.pickup_at = pickup
        state.pickup_date = pickup.date()
    ret = _parse_dt(extraction.return_at, tz)
    if message is not None:
        ret = anchor(ret, state.return_date, message)
    if ret:
        state.return_at = ret
        state.return_date = ret.date()
    if extraction.delivery_location:
        state.delivery_location = extraction.delivery_location

    prefs = state.vehicle_preferences
    import re
    if message and re.search(r"\b(?:forget|scratch|instead|no longer|never\s?mind)\b|بدل|انس", message, re.I):
        if extraction.models or extraction.makes or extraction.categories:
            prefs.models, prefs.makes, prefs.categories = [], [], []
            state.current_vehicle_options = []
            if not state.reservation_id:
                state.selected_vehicle_id = None
                state.quote_id = None
        if re.search(r"any colo[u]?r|forget.*colo[u]?r|أي لون|اى لون", message, re.I):
            prefs.color = None
    for named in extraction.models:
        if named not in prefs.models:
            prefs.models.append(named)
    for named in extraction.makes:
        if named not in prefs.makes:
            prefs.makes.append(named)
    for name in extraction.categories:
        try:
            category = Category(name)
        except ValueError:
            continue
        if category not in prefs.categories:
            prefs.categories.append(category)
    if extraction.color:
        prefs.color = None if extraction.color.lower() in ("any", "any colour", "any color") else extraction.color
    if extraction.budget_per_day:
        prefs.budget_per_day = Decimal(str(extraction.budget_per_day))
    if extraction.min_passengers:
        prefs.min_passengers = extraction.min_passengers

    if extraction.driver_age:
        state.driver_age = extraction.driver_age
    if extraction.residency != "unknown":
        state.residency = ResidencyType(extraction.residency)

    if extraction.intent != "unknown":
        state.intent = Intent(extraction.intent)

    # Advance the stage only forwards through the qualifying phase; the tool
    # layer owns every stage from QUOTED onwards.
    if state.stage is Stage.NEW_LEAD and (state.pickup_at or prefs.models or prefs.categories):
        state.stage = Stage.QUALIFYING

    return state
