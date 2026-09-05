"""Resolve customer references before a model can quote an arbitrary vehicle."""
from __future__ import annotations

import re

REFERENCE = re.compile(r"\b(?:this|that|it|these|those|like|fees?|charges?|first|second|third|cheapest)\b|دي|ده|عجبتني|رسوم", re.I)


def named_ids(text, fleet):
    """Match model names, not a broad make or a one-letter catalogue model."""
    found = []
    for vehicle in fleet:
        names = [f"{vehicle.make} {vehicle.model}"]
        if len(vehicle.model) >= 3:
            names.append(vehicle.model)
        if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I) for name in names):
            found.append(vehicle.id)
    return found


def current_options(ctx):
    state = ctx.load_state()
    if state.current_vehicle_options:
        return state.current_vehicle_options
    # Existing conversations predate the explicit current shortlist field.
    previous = [m for m in ctx.messages.for_conversation(ctx.conversation_id) if m.direction == 'outbound']
    if previous:
        return named_ids(previous[-1].content or '', ctx.engine.list_fleet())
    return []


def resolve(ctx, message):
    fleet = ctx.engine.list_fleet()
    named = named_ids(message, fleet)
    if named:
        return named
    options = current_options(ctx)
    makes = {v.make.split()[0].split('-')[0] for v in fleet}
    mentioned_makes = [make for make in makes if re.search(r'\b' + re.escape(make) + r'\b', message, re.I)]
    if mentioned_makes:
        matching = [v.id for v in fleet if v.make.split()[0].split('-')[0] in mentioned_makes]
        narrowed = [vid for vid in options if vid in matching]
        return narrowed or matching
    for index, word in enumerate(('first', 'second', 'third')):
        if re.search(r'\b' + word + r' (?:one|car|option)\b', message, re.I) and len(options) > index:
            return [options[index]]
    if re.search(r'\bcheapest\b', message, re.I) and options:
        minimum = min(ctx.engine.get_vehicle(vid).daily_price for vid in options)
        return [vid for vid in options if ctx.engine.get_vehicle(vid).daily_price == minimum]
    return options


def ambiguous(ctx, message):
    if re.search(r'\b(?:photos?|pictures?|compare|both|each|all)\b|which.*cheapest', message, re.I):
        return False
    return bool(REFERENCE.search(message) and not named_ids(message, ctx.engine.list_fleet())
                and len(resolve(ctx, message)) > 1)


def clarification(ctx, message):
    names = list(dict.fromkeys(ctx.engine.get_vehicle(vid).display_name for vid in resolve(ctx, message)))
    reply = 'Which car do you mean: ' + ' or '.join(names) + '?'
    if re.search(r'fees?|charges?|deposit|رسوم|تأمين', message, re.I):
        reply += (' I can explain the fees for that car once you choose. Any unconfirmed deposit or '
                  'extra-mileage fee needs a colleague to confirm it; choosing a car does not determine those amounts.')
    return reply


def quote_error(ctx, vehicle_id):
    if ctx.session is None or not ctx.conversation_id:
        return None
    inbound = [m for m in ctx.messages.for_conversation(ctx.conversation_id) if m.direction == 'inbound']
    if not inbound:
        return None
    message = inbound[-1].content or ''
    if ambiguous(ctx, message):
        return {'error': 'vehicle_choice_required', 'message': clarification(ctx, message)}
    choices = resolve(ctx, message)
    if choices and vehicle_id not in choices:
        return {'error': 'vehicle_choice_mismatch', 'message': 'Quote only the vehicle the customer named or selected. Ask which car they mean if unclear.',
                'vehicle_options': choices}
    return None


def remember_options(ctx, turn):
    """Track the cars actually presented, rather than every internal lookup."""
    ids = named_ids(turn.reply, ctx.engine.list_fleet())
    photos = [item['vehicle_id'] for item in turn.media if item.get('vehicle_id')]
    if photos:
        ids = list(dict.fromkeys(photos))
    if ids:
        state = ctx.load_state()
        state.current_vehicle_options = ids
        ctx.save_state(state)


def remember_customer_choice(ctx, message):
    explicit = named_ids(message, ctx.engine.list_fleet())
    ordinal = re.search(r'\b(?:first|second|third) (?:one|car|option)\b', message, re.I)
    if explicit or ordinal:
        choices = resolve(ctx, message)
        if len(choices) == 1:
            state = ctx.load_state()
            state.current_vehicle_options = choices
            ctx.save_state(state)
