"""JSON tool schemas — the only capabilities the model has.

Descriptions are deliberately prescriptive about *when* to call each tool, not
just what it does. Recent models reach for tools conservatively, and a trigger
condition in the description is the most reliable lever on call rate.

`strict: true` is not used. Most of these tools have many optional parameters,
and strict mode would force a nullable union on every one of them; the tool
layer already validates arguments and returns error envelopes the agent can
recover from conversationally, which is the behaviour we actually want.

The list is sorted by name so the serialised tool block is byte-stable — tools
render first in the prompt, so any reordering would invalidate the entire cache.
"""

from __future__ import annotations

from typing import Any

_ISO = "ISO 8601 datetime in Asia/Dubai time, e.g. 2026-09-04T19:00:00+04:00"

_CATEGORIES = [
    "economy",
    "sedan",
    "luxury_sedan",
    "suv",
    "luxury_suv",
    "sports",
    "supercar",
    "convertible",
]


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


_WINDOW = {
    "pickup_at": {"type": "string", "description": f"Start of the rental. {_ISO}"},
    "return_at": {"type": "string", "description": f"End of the rental. {_ISO}"},
}


TOOLS: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- reads
    _tool(
        "calculate_quote",
        "Price a specific vehicle for specific dates without saving anything. Call "
        "this when the customer asks what something costs, or to compare prices, and "
        "you do not yet need a quote reference. Returns the full breakdown including "
        "VAT, delivery, deposit and included mileage. Use create_demo_quote instead "
        "once the customer is ready to book.",
        {
            "vehicle_id": {"type": "string", "description": "Vehicle id, e.g. veh_13"},
            **_WINDOW,
            "delivery_location": {
                "type": "string",
                "description": "Where the customer wants the car delivered, in their own words",
            },
            "discount_percent": {
                "type": "number",
                "description": "Only after get_allowed_discount confirms this much is permitted",
            },
            "excess_reduction": {
                "type": "boolean",
                "description": "Add the optional insurance excess reduction",
            },
        },
        ["vehicle_id", "pickup_at", "return_at"],
    ),
    _tool(
        "find_alternatives",
        "Find substitutes for a vehicle that is unavailable or that the customer has "
        "rejected. ALWAYS call this before telling a customer a car is unavailable — "
        "never deliver an unavailable answer without options attached. Results are "
        "ordered by how close each substitute is to what was asked for.",
        {
            "vehicle_id": {"type": "string", "description": "The vehicle they wanted"},
            **_WINDOW,
            "max_daily_price": {"type": "number", "description": "Respect a stated budget"},
            "delivery_location": {"type": "string"},
            "limit": {"type": "integer", "description": "At most 3"},
        },
        ["vehicle_id", "pickup_at", "return_at"],
    ),
    _tool(
        "get_active_reservation",
        "Get the customer's current booking. Call this FIRST whenever the customer "
        "refers to an existing booking without naming it — 'can you make it 8 "
        "instead', 'push it back a day', 'add another day', 'cancel it'. Never ask "
        "the customer which car they mean before checking this.",
        {},
        [],
    ),
    _tool(
        "get_allowed_discount",
        "Get the maximum discount you are permitted to offer on a vehicle for given "
        "dates. Call this BEFORE responding to any request for a better price, "
        "discount or deal. Never name a discount figure you have not obtained here.",
        {"vehicle_id": {"type": "string"}, **_WINDOW},
        ["vehicle_id", "pickup_at", "return_at"],
    ),
    _tool(
        "get_customer",
        "Get what is known about this customer: name, residency, documents already on "
        "file, remembered preferences, and previous demo bookings. Call this at the "
        "start of a conversation with a returning customer, or before asking for "
        "details they may have already given you.",
        {},
        [],
    ),
    _tool(
        "show_vehicle_photos",
        "Send the customer photographs of one specific car. Call this when they "
        "ask to see a vehicle, when they are choosing between options and a look "
        "would decide it, or right after they show real interest in one car — "
        "seeing it is what turns an enquiry into a booking. Do not call it for "
        "every car you mention; a customer who asked for three options wants "
        "three descriptions and photos of the one they lean towards. The photos "
        "are attached to your reply automatically, so write as if the customer "
        "can already see them and never say 'attached' or 'above'.",
        {
            "vehicle_id": {"type": "string"},
            "caption": {
                "type": "string",
                "description": (
                    "One short line under the first photo. Optional. Never put a "
                    "price here that a pricing tool has not returned."
                ),
            },
        },
        ["vehicle_id"],
    ),
    _tool(
        "get_vehicle_details",
        "Get full detail on one vehicle: the daily rate, capacity, mileage "
        "allowance, insurance excess, minimum driver age, photos. Call this when "
        "the customer asks about a specific car's specification, or before "
        "answering a question you would otherwise guess at. **This is also how "
        "you answer 'how much is the X?' before you know their dates** — the "
        "daily rate is a fact about the car and does not depend on when they "
        "want it. Only availability and the total need dates.",
        {"vehicle_id": {"type": "string"}},
        ["vehicle_id"],
    ),
    _tool(
        "search_company_policy",
        "Search the company's own policy documents and get back the relevant "
        "passages. Call this for ANY question about rules, procedure or "
        "eligibility that the state block does not already answer — licences by "
        "nationality, additional drivers, driving to Oman, fines and Salik, "
        "smoking, pets, child seats, breakdowns, accidents, impounded cars, lost "
        "keys, what the deposit is held against. Call it BEFORE saying you are "
        "not sure and before escalating, because the answer is usually there. "
        "Answer from the passages in your own words. They deliberately contain "
        "no prices — every figure still comes from a pricing tool.",
        {
            "question": {
                "type": "string",
                "description": "The customer's question, in their own words",
            },
            "limit": {"type": "integer", "description": "Passages to return, default 3"},
        },
        ["question"],
    ),
    _tool(
        "look_up_vehicles",
        "Check what cars the company actually owns, by name — no dates needed. "
        "Call this for ANY question about what is in the fleet: 'do you have a "
        "Cybertruck?', 'what Teslas do you have?', 'do you have anything "
        "electric?', 'what's your cheapest car?'. NEVER answer a question about "
        "what the fleet contains from your own impression of what a rental "
        "company owns — you will be wrong, and a customer told we do not have a "
        "car we do have goes to a competitor. Owning a car and it being free "
        "are different questions: this answers only the first, so get their "
        "dates and call search_available_vehicles before promising anything.",
        {
            "query": {
                "type": "string",
                "description": (
                    "Words to match against make, model, category and body type — "
                    "'cybertruck', 'tesla', 'convertible ferrari'. Omit to list "
                    "the cheapest cars in the fleet."
                ),
            },
        },
        [],
    ),
    _tool(
        "search_available_vehicles",
        "Search the fleet for cars that are actually free for the requested dates. "
        "Call this as soon as you know pickup and return times — it is the only way "
        "to learn what is available. Returns at most three options, already ranked. "
        "If it returns nothing, call find_alternatives rather than telling the "
        "customer there is nothing.",
        {
            **_WINDOW,
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": _CATEGORIES},
                "description": "Filter to these classes when the customer named one",
            },
            "makes": {"type": "array", "items": {"type": "string"}},
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Model names the customer asked for, e.g. ['G63']",
            },
            "color": {"type": "string"},
            "max_daily_price": {
                "type": "number",
                "description": "A stated budget per day. This is a hard limit.",
            },
            "min_passenger_capacity": {"type": "integer"},
            "driver_age": {"type": "integer", "description": "If the customer has told you"},
            "delivery_location": {"type": "string"},
            "exclude_vehicle_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Cars already shown and rejected",
            },
        },
        ["pickup_at", "return_at"],
    ),
    # --------------------------------------------------------------- writes
    _tool(
        "cancel_demo_reservation",
        "Cancel a demo reservation and release the vehicle. Call this when the "
        "customer clearly asks to cancel, and only after confirming they mean it. "
        "The cancellation fee depends on how close to the delivery time they are and "
        "is calculated from policy — never quote a fee yourself, report the one this "
        "returns. Calling it on an already-cancelled booking is safe and simply "
        "reports the original outcome.",
        {
            "reservation_id": {"type": "string", "description": "e.g. DEMO-1042"},
            "reason": {"type": "string"},
        },
        ["reservation_id"],
    ),
    _tool(
        "create_demo_quote",
        "Create and save a priced quote, returning a quote reference. Call this when "
        "the customer has settled on a car and dates. A reservation can ONLY be made "
        "from a saved quote, so this always comes before create_demo_reservation.",
        {
            "vehicle_id": {"type": "string"},
            **_WINDOW,
            "delivery_location": {"type": "string"},
            "discount_percent": {
                "type": "number",
                "description": "Only a figure get_allowed_discount has approved",
            },
            "excess_reduction": {"type": "boolean"},
        },
        ["vehicle_id", "pickup_at", "return_at"],
    ),
    _tool(
        "create_demo_reservation",
        "Turn a saved quote into a demonstration reservation. Call this only after the "
        "customer has clearly agreed to book. Returns the DEMO- reference to give "
        "them. Calling it twice with the same quote is safe — it returns the same "
        "reservation rather than making a second one.",
        {"quote_id": {"type": "string", "description": "From create_demo_quote, e.g. DQ-501"}},
        ["quote_id"],
    ),
    _tool(
        "create_payment_link",
        "Create a link the customer can pay on, for a CONFIRMED booking. The amount "
        "comes from their stored quote — you cannot set it, change it, round it or "
        "negotiate it, and there is no parameter for one. Send the link with a plain "
        "sentence saying what it is for and how much. Do not call this for a booking "
        "that is still held: a colleague is confirming the car is free, and asking for "
        "money before that is worse than asking late. If the amount for a purpose has "
        "not been confirmed by the operator, this refuses rather than guessing — take "
        "that to a person.",
        {
            "reservation_id": {"type": "string", "description": "e.g. DEMO-1042"},
            "purpose": {
                "type": "string",
                "description": (
                    "What they are paying: 'rental_total' (the default), 'holding' to "
                    "secure the car against the invoice, or 'deposit'."
                ),
            },
        },
        ["reservation_id"],
    ),
    _tool(
        "escalate_conversation",
        "Hand the conversation to a human colleague. Call this IMMEDIATELY, before "
        "anything else, when something HAS HAPPENED: an accident, injury, police "
        "involvement, theft, a breakdown, a medical emergency, a legal threat, a "
        "payment dispute, suspected fraud, abuse, or any explicit request to speak to "
        "a person. Escalating unnecessarily is cheap; failing to escalate a real "
        "incident is not. "
        "But asking ABOUT something is not the same as it happening. 'What happens if "
        "I crash it?', 'what if it breaks down?', 'am I covered for...', 'how do I do "
        "a police report?', 'who do I call?' are ordinary questions from someone "
        "deciding whether to book — answer them from search_company_policy, which has "
        "the excess, the police report requirement, what insurance excludes and what "
        "to do at the scene. Escalating one of those ends the sale, sends your "
        "colleague a case with nothing in it to act on, and leaves the customer stuck "
        "behind it unable to get any further answer from you. "
        "Separately, and ONLY on the second ask: when a customer has already been told "
        "that a figure a tool reported as `unconfirmed` is confirmed before booking, "
        "and they ask again or push for a number, escalate with reason "
        "'unconfirmed_figure'. Do NOT escalate the first time someone asks about a "
        "deposit — nearly every customer does, the honest answer is that it is "
        "confirmed for their car, and escalating there ends the sale over a routine "
        "question. Escalate rather than promising the number later: 'at the final "
        "stage', 'once we book', 'the system calculates it' all describe a process "
        "that does not exist.",
        {
            "reason": {
                "type": "string",
                "description": "Short reason, e.g. 'accident', 'customer asked for a manager'",
            },
            "detail": {"type": "string", "description": "What the customer actually said"},
        },
        ["reason"],
    ),
    _tool(
        "extend_demo_rental",
        "Extend an existing booking to a later return time. Call this when the "
        "customer wants to keep the car longer. The whole rental is re-priced, so a "
        "long extension may reach a cheaper weekly rate — report the figure returned, "
        "never estimate it.",
        {
            "reservation_id": {"type": "string"},
            "new_return_at": {"type": "string", "description": f"New return time. {_ISO}"},
        },
        ["reservation_id", "new_return_at"],
    ),
    _tool(
        "modify_demo_reservation",
        "Change the delivery time, return time, location or vehicle on an existing "
        "booking. Send only the fields that change. Call this for 'make it 8 instead' "
        "or 'can you drop it at the hotel instead', once get_active_reservation has "
        "told you which booking they mean. The booking is re-priced automatically; "
        "report the difference this returns rather than working it out yourself.",
        {
            "reservation_id": {"type": "string"},
            "pickup_at": {"type": "string", "description": f"New delivery time. {_ISO}"},
            "return_at": {"type": "string", "description": f"New return time. {_ISO}"},
            "delivery_location": {"type": "string"},
            "vehicle_id": {"type": "string", "description": "Only to switch to a different car"},
        },
        ["reservation_id"],
    ),
    _tool(
        "record_demo_documents",
        "Record which documents the customer has provided, and find out what is still "
        "outstanding. Call this when they tell you what they have — a passport, a "
        "licence, an Emirates ID — so you can ask only for what is genuinely still "
        "missing. Documents are simulated in this demonstration: never ask the "
        "customer to actually send identity documents or photographs of them.",
        {
            "documents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "e.g. ['passport', 'international_driving_permit']",
            },
            "reservation_id": {"type": "string"},
        },
        ["documents"],
    ),
    _tool(
        "save_customer_preference",
        "Remember something about how this customer likes to rent — preferred colour, "
        "favourite model, usual delivery spot, language. Call this when they express "
        "a lasting preference. It cannot store prices, discounts, deposits or "
        "policies, and nothing saved here ever changes what they are charged.",
        {
            "key": {"type": "string", "description": "e.g. preferred_color, favourite_model"},
            "value": {"type": "string"},
        },
        ["key", "value"],
    ),
    _tool(
        "schedule_demo_delivery",
        "Record the confirmed delivery time and place on an existing reservation. "
        "Call this once the customer has agreed a specific delivery slot, so the "
        "booking carries it. Reports whether the slot falls inside normal operating "
        "hours — an out-of-hours delivery carries a fee that the quote already "
        "accounts for.",
        {
            "reservation_id": {"type": "string"},
            "delivery_at": {"type": "string", "description": _ISO},
            "delivery_location": {"type": "string"},
        },
        ["reservation_id"],
    ),
    _tool(
        "simulate_payment",
        "Simulate the deposit pre-authorisation on a demo reservation. Call this once "
        "documents are complete and the customer is ready to confirm. No card is "
        "charged and no money moves — say so plainly when you report it back, and "
        "never ask the customer for real card details.",
        {
            "reservation_id": {"type": "string"},
            "method": {
                "type": "string",
                "enum": ["credit_card", "debit_card", "bank_transfer"],
            },
        },
        ["reservation_id"],
    ),
]

TOOLS.sort(key=lambda t: t["name"])

TOOL_NAMES = [t["name"] for t in TOOLS]
