"""Checks for operational claims that do not contain a price."""
from __future__ import annotations

import re
from types import SimpleNamespace

STAFF = re.compile(
    r"\b(?:i(?:'m| am) checking with|i(?:'ve| have) (?:asked|contacted).{0,30}(?:colleague|team|manager|owner)|"
    r"i(?:'ve| have) (?:escalated|passed|sent).{0,60}(?:colleague|team|manager|owner)|"
    r"(?:our|the) team is (?:currently )?(?:confirming|checking)|"
    r"(?:we|i) (?:are|am) (?:checking|confirming) with)\b"
    r"|(?:please allow me|let me).{0,50}(?:check|confirm).{0,50}(?:colleague|team)"
    r"|(?:please allow me|let me).{0,50}(?:colleague|team).{0,35}(?:check|confirm)"
    r"|تواصلت مع|طلبت من زميل|أرسلت.*(?:الفريق|زميل)", re.I)
FEES = re.compile(r"\b(?:deposit|no.deposit|extra.?kilomet|extra.?km|per.?km|out.of.hours|delivery (?:fee|charge))\b|تأمين|وديعة|رسوم", re.I)
PROMISE = re.compile(
    r"(?:once|when|if).{0,140}(?:exact|confirmed) (?:figures|fees|deposit|pricing|information)"
    r"|(?:can|will).{0,50}(?:exact|confirmed) (?:figures|fees).{0,60}(?:immediately|dates)"
    r"|(?:calculated|calculate).{0,60}(?:vehicle and dates|specific vehicle|booking stage)"
    r"|confirm the exact fee.{0,70}(?:booking details|dates)", re.I | re.S)
NEXT_STEP_PROMISE = re.compile(
    r"(?:confirmed|exact) (?:figure|amount|fee|deposit).{0,160}(?:next step|next stage|move (?:forward|to)|go ahead)"
    r"|(?:next step|next stage).{0,160}(?:confirmed|exact) (?:figure|amount|fee|deposit)", re.I | re.S)
GENERAL_FEES = re.compile(r"\b(?:what(?: are| is)?|explain|tell me(?: about)?)\s+(?:(?:all|the|rental|other)\s+)*(?:fees|charges)\b|ما هي الرسوم|ايه الرسوم", re.I)


def latest_customer(ctx):
    if ctx.session is None or not ctx.conversation_id:
        return ""
    return next((m.content or "" for m in reversed(ctx.messages.for_conversation(ctx.conversation_id))
                 if m.direction == "inbound"), "")


def customer_budgets(ctx):
    from .figures import budget_values
    if ctx.session is None or not ctx.conversation_id:
        return set()
    return set().union(*(budget_values(m.content) for m in ctx.messages.for_conversation(ctx.conversation_id)
                         if m.direction == "inbound"))


def needs_answer(ctx, reply, calls):
    question = latest_customer(ctx)
    fleet = list(ctx.engine.list_fleet())
    state = ctx.load_state() if ctx.session is not None else None
    if state and state.selected_vehicle_id:
        fleet = [v for v in fleet if v.id == state.selected_vehicle_id]
    pending = ctx.engine.rules.get("_pending_confirmation", {})
    missing = (
        bool(GENERAL_FEES.search(question)) and any(v.deposit is None or v.extra_km_price is None for v in fleet)
        or bool(re.search(r"deposit|تأمين|وديعة", question, re.I)) and any(v.deposit is None for v in fleet)
        or bool(re.search(r"extra.?k|per.?k|kilomet|كيلومتر", question, re.I)) and any(v.extra_km_price is None for v in fleet)
        or bool(re.search(r"no.deposit", question, re.I)) and "no_deposit_option_fee" in pending
        or bool(re.search(r"delivery|deliver", question, re.I))
        and bool(re.search(r"Abu Dhabi|Sharjah|Ajman|Fujairah|Ras Al Khaimah|outside Dubai", question, re.I))
        and any("delivery" in k or "hours" in k for k in pending)
    )
    explicit = re.search(r"\b(?:exact|how much|confirm|zero|actually|colleague)\b|بالضبط|كام|كم", question, re.I)
    return bool(missing and (GENERAL_FEES.search(question) or FEES.search(question) or re.search(r"delivery|deliver", question, re.I))
                and (GENERAL_FEES.search(question) or explicit or STAFF.search(reply) or PROMISE.search(reply) or NEXT_STEP_PROMISE.search(reply)))


def problems(ctx, reply, calls):
    issues = []
    if STAFF.search(reply) and not any(c.tool_name == "escalate_conversation"
                                      and (c.result or {}).get("escalated") for c in calls):
        issues.append("Do not claim a colleague was contacted: no escalation was recorded. Call escalate_conversation if help is needed.")
    if (PROMISE.search(reply) or NEXT_STEP_PROMISE.search(reply)) and any(v.deposit is None for v in ctx.engine.list_fleet()):
        issues.append("Missing fees cannot be calculated from dates or at a later booking stage. A colleague must supply them.")
    # Evaluate descriptions against the actual selected or presented fleet rows.
    ids = set()
    for call in calls:
        if call.tool_name in ("get_vehicle_details", "show_vehicle_photos", "calculate_quote", "create_demo_quote"):
            vid = (call.arguments or {}).get("vehicle_id") or (call.result or {}).get("vehicle_id")
            if vid:
                ids.add(vid)
    vehicles = [v for v in ctx.engine.list_fleet() if v.id in ids]
    records = []
    def visit(value):
        if isinstance(value, dict):
            if value.get("vehicle_id"):
                records.append(value)
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for call in calls:
        if not (call.result or {}).get("error"):
            visit(call.result or {})
    from .figures import money_in, without_budget_echoes
    reply = without_budget_echoes(reply, customer_budgets(ctx))
    from ..evaluation.checks import supported_numbers
    colours = r"black|white|grey|gray|red|blue|green|silver|yellow|orange|أسود|أبيض|رمادي|أحمر|أزرق"
    for sentence in re.split(r"[.!?؟\n]+", reply):
        if vehicles and all(v.color == "unspecified" for v in vehicles):
            claim = re.search(rf"(?:comes in|is|in)\s+(?:a\s+)?(?:{colours})\b|(?:لونها|لونه|باللون)\s*(?:{colours})", sentence, re.I)
            if claim and not re.search(r"(?:check|confirm|unknown|unspecified|not|unconfirmed|تأكيد|غير)", sentence, re.I):
                issues.append("The vehicle colour is unspecified. Photo filenames are not verified specifications. Say the colour needs confirmation.")
        if vehicles and all(v.year is None for v in vehicles) and re.search(r"\b(?:19|20)\d{2}\s+model|model (?:year )?(?:is )?(?:19|20)\d{2}\b", sentence, re.I):
            issues.append("The model year is unknown. Do not infer it from photographs or filenames.")
    for sentence in re.split(r"[!?؟\n]|\.(?!\d)", reply):
        amounts = money_in(sentence)
        if not amounts:
            continue
        fleet = ctx.engine.list_fleet()
        named = [v for v in fleet if re.search(r"\b" + re.escape(v.model) + r"\b", sentence, re.I)]
        if not named:
            named = [v for v in fleet if re.search(r"\b" + re.escape(v.make) + r"\b", sentence, re.I)]
        if named:
            named_ids = {v.id for v in named}
            evidence = [SimpleNamespace(tool_name="vehicle_evidence", result=r) for r in records
                        if r["vehicle_id"] in named_ids]
            if not amounts <= supported_numbers(evidence, monetary_only=True):
                issues.append("The amount is not supported for the named vehicle. Quote that vehicle before stating its price; another car's price cannot support it.")
    if re.search(r"(?:free|complimentary) delivery (?:anywhere|within|in) (?:in )?Dubai", reply, re.I) and not re.search(
        r"hours|09:00|9\s*(?:am|AM)|charge|fee|ساعات", reply, re.I
    ):
        issues.append("Qualify free Dubai delivery: it applies within the published operating hours; out-of-hours charges apply.")
    return list(dict.fromkeys(issues))


def safe_reply(ctx):
    if ctx.load_state().awaiting_figure:
        return "I have recorded a request for a colleague to confirm the missing fees. I cannot quote those amounts until they reply. We can continue with your other rental details."
    where = ctx.engine.rules.get("operator_location", {})
    if where.get("address") and re.search(r"where.*(?:located|office)|office.*collect|أين.*المكتب", latest_customer(ctx), re.I):
        return f"Our office is at {where['address']}. Office collection needs confirmation. Free Dubai delivery applies within operating hours; out-of-hours charges apply."
    return "I could not verify that detail. A colleague needs to confirm it before I can promise it."
