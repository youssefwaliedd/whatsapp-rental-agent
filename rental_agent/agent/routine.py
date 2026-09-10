"""Narrow, unambiguous browsing steps that do not need language generation."""
import re

from ..domain import dates, selection
from ..services import checkout
from ..tools.registry import execute_tool


def reply(ctx, message):
    if not checkout.enabled(ctx): return None
    from .loop import AgentTurn
    state = ctx.load_state()
    text = message.strip().lower().rstrip('.!?')
    if text in {'open budget give me your best car','give me your best car','open budget','unlimited budget'}:
        return AgentTurn(reply='What matters most to you: comfort, performance, or SUV space? That will help narrow down the cars.')
    if text in {'what are their prices','what are the prices','their prices','price list','how much are they'}:
        options = selection.current_options(ctx)
        if not options:
            return AgentTurn(reply='Which cars would you like prices for?')
        lines = ['Daily rates for the cars we discussed:']; succeeded = []
        for vehicle_id in options[:3]:
            result = execute_tool(ctx,'get_vehicle_details',{'vehicle_id':vehicle_id})
            if result.get('error'):
                return AgentTurn(reply=result.get('message','I could not retrieve that car’s details.'), tool_calls=['get_vehicle_details'])
            succeeded.append('get_vehicle_details')
            lines.append(f"• {result['display_name']}: {ctx.engine.operator.currency} {result['daily_price']} per day.")
        lines.append('These are daily rates, not a final rental quote. Fees and availability still need to be checked for your rental.')
        return AgentTurn(reply='\n'.join(lines),tool_calls=succeeded,tools_succeeded=succeeded)
    # Limit date parsing to standalone dates/times, never changes to a booking
    # or dates describing identity documents, payments, or other questions.
    if state.reservation_id: return None
    normalized = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', text)
    words = set(re.findall(r'[a-z]+', normalized))
    allowed = set('from to till until through at both same time pickup return am pm noon midnight january jan february feb march mar april apr may june jun july jul august aug september sept sep october oct november nov december dec'.split())
    if not words <= allowed or not (dates.DATE.search(normalized) or dates.CLOCK.search(normalized) or re.search(r'\b\d{1,2}:\d{2}\b',normalized)):
        return None
    if not state.pickup_date or not state.return_date:
        return AgentTurn(reply='What pickup and return dates would you like?')
    if not state.pickup_at or not state.return_at:
        return AgentTurn(reply='What pickup and return times would you like? Please give both times, or say the same time for both.')
    prefs = state.vehicle_preferences
    chosen = ctx.engine.get_vehicle(state.selected_vehicle_id) if state.selected_vehicle_id else None
    result = execute_tool(ctx,'search_available_vehicles',{
        'pickup_at':state.pickup_at.isoformat(), 'return_at':state.return_at.isoformat(),
        'makes':[chosen.make] if chosen else prefs.makes,
        'models':[chosen.model] if chosen else prefs.models,
        'categories':[c.value for c in prefs.categories],
        'color':prefs.color, 'max_daily_price':str(prefs.budget_per_day) if prefs.budget_per_day is not None else None,
        'min_passenger_capacity':prefs.min_passengers, 'driver_age':state.checkout.get('driver_age'),
    })
    if result.get('error'):
        return AgentTurn(reply=result.get('message','Those rental details could not be checked.'),tool_calls=['search_available_vehicles'])
    from .search_recovery import reply as render
    answer = render(ctx,[('search_available_vehicles',result)])
    if not answer:
        state.current_vehicle_options = [];ctx.save_state(state)
        answer = 'The search returned no cars matching the current dates and preferences. Would you like to change the dates or car preferences?'
    return AgentTurn(reply=answer,tool_calls=['search_available_vehicles'],tools_succeeded=['search_available_vehicles'])
