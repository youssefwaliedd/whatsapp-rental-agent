"""Render current-turn search evidence when the response model is unavailable."""

SEARCH_TOOLS = {'search_available_vehicles', 'find_alternatives'}


def reply(ctx, outcomes):
    # Never hide a failed tool or a booking/payment attempt behind an earlier
    # successful search. Only this invocation's read-only search results count.
    if not outcomes or any(name not in SEARCH_TOOLS or 'error' in result for name, result in outcomes):
        return None
    name, result = outcomes[-1]
    vehicles = result.get('vehicles' if name == 'search_available_vehicles' else 'alternatives', [])
    if not vehicles:
        return None
    lines = ['Here are the options returned by the car search:']
    for item in vehicles[:3]:
        lines.append(f"• {item['display_name']}: {ctx.engine.operator.currency} {item['daily_price']} per day.")
    lines.append('These are daily rates, not a final rental quote. Nothing has been booked.')
    if ctx.engine.operator.is_demonstration:
        lines.append('Availability is based on the demonstration calendar, not the company’s live bookings.')
    lines.append('Which option interests you? If you want the best fit, do you prefer comfort, performance, or an SUV?')
    return '\n'.join(lines)
