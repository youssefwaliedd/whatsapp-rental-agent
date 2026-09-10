"""Server-enforced rental eligibility and acceptance, independent of AI wording."""
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from ..domain.models import Quote
from ..store.models import CustomerDocument, DocumentCheck, DocumentFacts

ACCEPT = 'I accept the rental terms and this quote'
LABELS = {'emirates_id':'Emirates ID', 'uae_driving_licence':'UAE driving licence',
          'passport':'passport', 'visit_visa_or_entry_stamp':'visit visa or entry stamp',
          'home_country_licence':'home-country driving licence',
          'international_driving_permit':'international driving permit',
          'gcc_driving_licence':'GCC driving licence'}


def enabled(ctx):
    # The explicit legacy setting exists only for old isolated test fixtures.
    return ctx.engine.rules.get('booking', {}).get('enforce_checkout_flow', True)


def last_message(ctx, direction='inbound'):
    if not ctx.session or not ctx.conversation_id:
        return ''
    return next((m.content for m in reversed(ctx.messages.for_conversation(ctx.conversation_id))
                 if m.direction == direction), '')


def save(ctx, state, flow):
    state.checkout = flow
    ctx.save_state(state)


def observe(ctx, message):
    if not enabled(ctx): return
    state = ctx.load_state(); flow = dict(state.checkout)
    if not flow and not state.reservation_id:
        flow['mode'] = 'new'
    if re.search(r'\bnew (?:booking|rental|reservation)\b|حجز جديد|ايجار جديد|إيجار جديد', message, re.I):
        if flow.get('mode') != 'new' or state.reservation_id:
            old = ctx.reservations.get(state.reservation_id) if state.reservation_id else None
            if old:
                if state.pickup_date == old.pickup_at.date(): state.pickup_date = None
                if state.return_date == old.return_at.date(): state.return_date = None
                state.selected_vehicle_id = None
                state.current_vehicle_options = []
            state.pickup_at = state.return_at = None
            state.quote_id = state.reservation_id = None
            state.delivery_location = None
            from ..domain.enums import Stage
            state.stage = Stage.QUALIFYING
            flow = {'mode':'new'}
    if re.search(r"\b(?:proceed|reserve|book it|book this|ready to book|want to book|would like to book|let'?s book|want to pay|payment link|payement link|go ahead)\b|احجز|أدفع|ادفع|رابط الدفع", message, re.I):
        if not re.search(r"\b(?:don't|do not|not yet|cancel)\b|لا تحجز|مت?حجز", message, re.I):
            flow['requested'] = True
    from ..domain.consent import defers_booking
    if defers_booking(message):
        flow['requested'] = False
        flow.pop('accepted', None)
    # Record only statements in customer text, never a model's inferred age.
    age = re.search(r"\b(?:i am|i'm|im|age(?: is)?|aged)\s*(\d{1,3})\b|\b(\d{1,3})\s*(?:years? old|yo)\b|عمري\s*(\d{1,3})", message, re.I)
    age_question = re.search(r'\b(?:your age|driver.s age|how old|what age)\b|كم عمرك|عمر السائق', last_message(ctx,'outbound'), re.I)
    if not age and (flow.get('asked') == 'driver_age' or age_question):
        age = re.fullmatch(r'\s*(\d{1,3})\s*', message)
    if age:
        value = int(next(g for g in age.groups() if g))
        if 16 <= value <= 100: flow['driver_age'] = value
    if re.search(r'\b(?:tourist|visitor|visiting)\b|سائح|زيارة', message, re.I): flow['residency'] = 'tourist'
    elif re.search(r'\bgcc resident\b|مقيم خليجي', message, re.I): flow['residency'] = 'gcc_resident'
    elif re.search(r'\b(?:uae resident|resident in (?:dubai|uae)|live in (?:dubai|the uae)|emirates id)\b|مقيم.*(?:الإمارات|الامارات|دبي)', message, re.I): flow['residency'] = 'uae_resident'
    normalized = message.lower().replace('noon','12pm').replace('midnight','12am')
    clocks = []
    for match in re.finditer(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b', normalized):
        h = int(match[1] or match[4]); m = int(match[2] or match[5] or 0)
        if match[3]:
            if not 1 <= h <= 12: continue
            h = h % 12 + (12 if match[3] == 'pm' else 0)
        if h < 24 and m < 60: clocks.append([h,m])
    if len(clocks) >= 2: flow['pickup_clock'], flow['return_clock'] = clocks[0], clocks[-1]
    elif clocks:
        if re.search(r'\b(?:both|same time)\b|الاتنين|نفس الوقت', message, re.I):
            flow['pickup_clock'] = flow['return_clock'] = clocks[0]
        elif re.search(r'\b(?:return|drop off)\b|إرجاع|ارجاع', message, re.I): flow['return_clock'] = clocks[0]
        else: flow['pickup_clock'] = clocks[0]
    from ..domain.dates import remember
    remember(state, message, ctx.now())
    for side in ['pickup','return']:
        day = getattr(state, side+'_date'); clock = flow.get(side+'_clock')
        if day and clock:
            setattr(state, side+'_at', datetime.combine(day, datetime.min.time(), ctx.engine.tz).replace(hour=clock[0],minute=clock[1]))
        elif day and getattr(state, side+'_at') and getattr(state, side+'_at').date() != day:
            # An older quote's timestamp cannot supply an unconfirmed time on
            # a different enquiry date. The reservation itself stays untouched.
            setattr(state, side+'_at', None)
    duration = re.search(r'\b(?:change|make)\s+(?:it|that|this)\s+(?:to\s+)?(\d+)\s*days?\b|خليها\s*(\d+)\s*(?:أيام|ايام)', message, re.I)
    if duration and state.pickup_at and not state.reservation_id:
        days = int(next(g for g in duration.groups() if g))
        if 1 <= days <= 90:
            state.return_at = state.pickup_at + timedelta(days=days)
            state.return_date = state.return_at.date()
            flow['return_clock'] = [state.return_at.hour,state.return_at.minute]
    if re.search(r'\b(?:pick it up|pick up from|pickup option|collect from|branch pickup)\b|استلم.*(?:فرع|مكتب)', message, re.I):
        flow['fulfilment'] = 'collection'
        state.delivery_location = None
    delivery = re.search(r'\bdeliver(?:y)?\s+(?:it\s+)?to\s+(.+)', message, re.I)
    if delivery:
        flow['fulfilment'] = 'delivery'; flow['address'] = delivery[1].strip()[:500]
        state.delivery_location = flow['address']
    elif flow.get('asked') == 'delivery_address' and len(message.strip()) >= 8 and '?' not in message and not informational(message):
        flow['fulfilment'] = 'delivery'; flow['address'] = message.strip()[:500]
        state.delivery_location = flow['address']
    if re.fullmatch(r'(?:delivery|deliver it|توصيل)[.! ]*', message, re.I): flow['fulfilment'] = 'delivery'
    if flow.get('driver_age'):
        state.driver_age = flow['driver_age']
        ctx.customers.get(ctx.customer_id).driver_age = flow['driver_age']
    if flow.get('residency'):
        from ..domain.enums import ResidencyType
        state.residency = ResidencyType(flow['residency'])
        ctx.customers.get(ctx.customer_id).residency = flow['residency']
    if message.strip().rstrip('.!').casefold() in {ACCEPT.casefold(), 'أوافق على شروط الإيجار وعرض السعر هذا'}:
        if flow.get('offered_text') and last_message(ctx,'outbound').endswith(flow['offered_text']):
            flow['accepted'] = flow.get('offered')
    save(ctx,state,flow)


def mutation_error(ctx, reservation_id):
    if not enabled(ctx): return None
    message = last_message(ctx)
    if not reservation_id or str(reservation_id).lower() not in message.lower() or not re.search(r'\b(?:change|modify|extend|cancel|move|update)\b|عدل|عدّل|مدد|إلغاء|الغاء',message,re.I):
        return {'error':'booking_reference_required', 'message':
            'I will not change an existing booking while we discuss a new quote. To change an existing booking, state its reference and the change you want.'}
    from ..domain.consent import defers_booking
    if defers_booking(message): return {'error':'change_not_authorized','message':'The requested change has not been authorized.'}


def time_error(ctx, pickup, ret):
    if not enabled(ctx): return None
    state = ctx.load_state(); flow = state.checkout
    if (state.pickup_date and pickup.date()!=state.pickup_date) or (state.return_date and ret.date()!=state.return_date):
        return {'error':'rental_dates_changed','message':date_status(ctx) + ' The attempted quote uses different dates. It has not been applied.'}
    if flow.get('pickup_clock') != [pickup.hour,pickup.minute] or flow.get('return_clock') != [ret.hour,ret.minute]:
        return {'error':'rental_times_required','message':'What pickup and return times would you like? Please give both times, or say the same time for both.'}


def cipher(ctx):
    value = ctx.session.info.get('document_cipher')
    if value: return value
    key = os.getenv('DOCUMENT_ENCRYPTION_KEY')
    return Fernet(key.encode()) if key else None


def document_status(ctx, quote, flow):
    rules = ctx.engine.rules['required_documents']
    required = list(rules.get(flow.get('residency'), []))
    state = ctx.load_state()
    vehicle_id = quote.vehicle_id if quote else state.selected_vehicle_id
    category = ctx.engine.get_vehicle(vehicle_id).category.value if vehicle_id else None
    rental_end = quote.return_at.date() if quote else state.return_date or ctx.now().date()
    rental_start = quote.pickup_at.date() if quote else state.pickup_date or ctx.now().date()
    required += rules.get('additional_for_categories',{}).get(category,[])
    found = set(); digests = set(); fingerprints = []; birth_ages = []; problems = []
    crypto = cipher(ctx)
    rows = ctx.session.execute(select(CustomerDocument,DocumentCheck,DocumentFacts).join(DocumentCheck).join(DocumentFacts,
        DocumentFacts.document_id==CustomerDocument.document_id).where(CustomerDocument.customer_id==ctx.customer_id,
        CustomerDocument.status=='checks_passed',CustomerDocument.storage_key.is_not(None)))
    for doc,check,facts in rows:
        if not crypto: continue
        try: data = json.loads(crypto.decrypt(facts.encrypted_payload.encode()))
        except (InvalidToken,ValueError): continue
        expiry = data.get('expiry_date')
        if not expiry or date.fromisoformat(expiry) < rental_end: continue
        kind = doc.document_type; country = (data.get('issuing_country') or '').upper()
        mapped = {'passport':'passport','emirates_id':'emirates_id','visa':'visit_visa_or_entry_stamp',
                  'international_driving_permit':'international_driving_permit'}.get(kind)
        if kind == 'driving_license' and country:
            mapped = 'uae_driving_licence' if country == 'AE' else 'home_country_licence'
            if country in {'BH','KW','OM','QA','SA','AE'}: found.add('gcc_driving_licence')
        if mapped: found.add(mapped)
        if check.name_digest: digests.add(check.name_digest)
        fingerprints.append(doc.document_id+':'+(doc.sha256 or '')+':'+check.checked_at.isoformat())
        if data.get('date_of_birth'):
            born = date.fromisoformat(data['date_of_birth']); now = ctx.now().date()
            birth_ages.append(now.year-born.year-((now.month,now.day)<(born.month,born.day)))
        years = ctx.engine.rules['driver_requirements'].get('minimum_licence_held_years_supercar' if category=='supercar' else 'minimum_licence_held_years',0)
        if kind=='driving_license' and years:
            issued = data.get('issue_date')
            if not issued or (rental_start-date.fromisoformat(issued)).days < int(years)*365.25:
                problems.append('licence_history')
    missing = [r for r in required if r not in found]
    if not birth_ages: problems.append('document_date_of_birth')
    elif any(age != flow.get('driver_age') for age in birth_ages): problems.append('driver_age_mismatch')
    if len(digests) != 1: problems.append('matching_document_names')
    return missing, problems, sorted(fingerprints)


def evaluate(ctx, quote, *, check_expiry=True):
    state = ctx.load_state(); flow = state.checkout
    def block(code, message): return {'ready':False,'step':code,'message':message}
    if check_expiry and quote.expires_at <= ctx.now(): return block('refresh_quote','The quote has expired. A fresh quote and acceptance are required.')
    if time_error(ctx, quote.pickup_at, quote.return_at): return block('rental_times',time_error(ctx,quote.pickup_at,quote.return_at)['message'])
    if not flow.get('driver_age'): return block('driver_age','How old is the driver? I need to check the age requirement before booking or payment.')
    minimum = ctx.engine.minimum_age_for(ctx.engine.get_vehicle(quote.vehicle_id).category)
    if flow['driver_age'] < minimum: return block('underage',f'This vehicle requires a driver aged at least {minimum}. I cannot proceed with this booking.')
    if not flow.get('residency'): return block('residency','Is the driver a UAE resident, a tourist, or a GCC resident? This determines the required documents.')
    missing,problems,documents = document_status(ctx,quote,flow)
    if missing:
        labels = ', '.join(LABELS.get(m,m.replace('_',' ')) for m in missing if m!='credit_card_in_driver_name')
        if labels: return block('documents','Please attach the following documents here: '+labels+'. They must be readable, show the same driver, and remain valid through the rental. For this demonstration, use fictional sample documents only.')
        return block('cardholder_verification','Cardholder verification for this vehicle has not been configured. Booking and payment remain blocked. Do not upload a bank card.')
    if problems: return block('document_eligibility','The documents have not established the required driver details: '+', '.join(p.replace('_',' ') for p in problems)+'. Please provide a current, matching sample document showing these details.')
    if not flow.get('fulfilment'): return block('fulfilment','Would you like delivery, or collection from our location? We must confirm this before payment.')
    if flow['fulfilment']=='collection':
        if ctx.engine.rules.get('operator_location',{}).get('collection_available') is not True:
            return block('collection_policy','Collection from the office has not been confirmed by the company. I cannot promise it or request payment for this arrangement. You can choose delivery instead.')
    elif not flow.get('address'): return block('delivery_address','What is the exact delivery address, including the building or hotel and area?')
    elif quote.delivery_location != flow['address']: return block('refresh_quote','The delivery address changed. A fresh quote is required before payment.')
    unknown = []
    if quote.deposit is None: unknown.append('security deposit')
    if quote.extra_km_price is None: unknown.append('extra-mileage rate')
    if quote.insurance_excess_is_minimum: unknown.append('final insurance excess')
    if unknown: return block('company_fees','The company still needs to confirm: '+', '.join(unknown)+'. This quote is provisional. Booking and payment remain blocked until those amounts are configured.')
    evidence = {'quote':quote.model_dump(mode='json'), 'policy':ctx.engine.rules._data,
                'age':flow['driver_age'],'residency':flow['residency'], 'documents':documents,
                'fulfilment':flow['fulfilment'],'address':flow.get('address')}
    fingerprint = hashlib.sha256(json.dumps(evidence,sort_keys=True,default=str).encode()).hexdigest()
    if flow.get('accepted') != fingerprint:
        return {'ready':False,'step':'acceptance','fingerprint':fingerprint,'message':terms(ctx,quote,flow)}
    return {'ready':True,'step':'ready','fingerprint':fingerprint,'message':'The configured checks and rental terms are complete. You can now request payment.'}


def terms(ctx, q, flow):
    policy = ctx.engine.rules
    cancellation = policy.get('cancellation',{})
    fuel = policy.get('fuel_policy',{})
    restrictions = policy.get('insurance',{}).get('not_covered',[])
    # Policy values are configured facts, never supplied by the model/customer.
    details = []
    if cancellation:
        details.append(f'Cancellation is free at least {cancellation.get("free_cancellation_hours_before_pickup")} hours before pickup. '
                       f'Later cancellation: {cancellation.get("late_cancellation_fee_percent")}% of the rental; '
                       f'no-show: {cancellation.get("no_show_fee_percent")}%.')
    if fuel.get('type')=='same_level_returned': details.append('Return the car with the same fuel level.')
    if fuel.get('grade'): details.append('Required fuel: '+str(fuel['grade'])+'.')
    if fuel.get('refuelling_service_fee') is not None:
        details.append(f'Refuelling service fee: {q.currency} {fuel["refuelling_service_fee"]}.')
    if restrictions: details.append('Insurance exclusions: '+', '.join(str(item).replace('_',' ') for item in restrictions)+'.')
    address = (flow.get('address') or policy.get('operator_location',{}).get('address') or 'company location').rstrip(' .')
    return (f'Rental terms for {q.quote_id}: {q.vehicle_display_name}\n'
            f'{q.pickup_at:%d %b %Y %H:%M} to {q.return_at:%d %b %Y %H:%M} (Dubai time)\n'
            f'Rental total including VAT: {q.currency} {q.total_charge}. Refundable deposit: {q.currency} {q.deposit}.\n'
            f'Included mileage: {q.included_km_total} km. Additional mileage: {q.currency} {q.extra_km_price}/km. '
            f'Insurance excess: {q.currency} {q.insurance_excess}.\n'
            f'Arrangement: {flow["fulfilment"]}, {address}.\n'
            +'\n'.join(details)+f'\nThis is a demonstration. No real vehicle is reserved.\nTo accept this quote and these terms, reply: {ACCEPT}')


def gate(ctx, quote):
    if not enabled(ctx): return None
    stored = ctx.quotes.get(quote.quote_id)
    if not stored or stored.customer_id != ctx.customer_id or stored.conversation_id != ctx.conversation_id or ctx.load_state().quote_id != quote.quote_id:
        return {'error':'quote_scope_mismatch','message':'Use the current quote for this enquiry. Earlier bookings and other customers\' quotes cannot authorize this rental.'}
    status = evaluate(ctx,quote)
    if status['ready']: return None
    return {'error':'checkout_incomplete', **status}


def reservation_gate(ctx, reservation):
    if not enabled(ctx): return None
    if reservation.customer_id != ctx.customer_id or reservation.conversation_id != ctx.conversation_id:
        return {'error':'reservation_scope_mismatch','message':'This booking does not belong to the current enquiry.'}
    stored = ctx.quotes.get(reservation.quote_id) if reservation.quote_id else None
    if not stored: return {'error':'checkout_incomplete','message':'A fresh accepted quote and eligibility checks are required.'}
    quote = Quote.model_validate(stored.payload)
    if (quote.vehicle_id,quote.pickup_at,quote.return_at,quote.total_charge)!=(reservation.vehicle_id,reservation.pickup_at,reservation.return_at,reservation.total_charge):
        return {'error':'reservation_quote_mismatch','message':'The reservation changed. A fresh quote and acceptance are required before payment.'}
    return gate(ctx,quote)


def tool_gate(ctx, name, args):
    if not ctx.session or not enabled(ctx): return None
    if name in {'search_available_vehicles','find_alternatives','calculate_quote','create_demo_quote'} and args.get('pickup_at') and args.get('return_at'):
        from ..tools.rental_tools import _parse_dt
        try:
            return time_error(ctx,_parse_dt(args['pickup_at'],'pickup_at',ctx.engine.tz),_parse_dt(args['return_at'],'return_at',ctx.engine.tz))
        except ValueError: return {'error':'invalid_dates','message':'Please confirm valid pickup and return dates and times.'}
    if name in {'modify_demo_reservation','extend_demo_rental','cancel_demo_reservation'}:
        return mutation_error(ctx,args.get('reservation_id',''))
    if name == 'create_demo_reservation':
        stored = ctx.quotes.get(args.get('quote_id',''))
        if stored: return gate(ctx,Quote.model_validate(stored.payload))
    if name in {'create_payment_link','schedule_demo_delivery','simulate_payment'}:
        reservation = ctx.reservations.get(args.get('reservation_id',''))
        if reservation:
            if name == 'create_payment_link' and reservation.customer_id == ctx.customer_id and reservation.payment_status in {'paid','payment_review','refund_pending','refunded','partially_refunded','holding_paid','deposit_paid'}:
                return {'error':'payment_already_received','message':'A payment is already recorded for this booking. I will not request another payment.'}
            return reservation_gate(ctx,reservation)


def next_reply(ctx, quote):
    result = evaluate(ctx,quote)
    state = ctx.load_state(); flow = dict(state.checkout); flow['asked'] = result['step']
    if result['step']=='acceptance': flow['offered'], flow['offered_text'] = result['fingerprint'], result['message']
    save(ctx,state,flow)
    return result['message']


def current_quote(ctx):
    state = ctx.load_state()
    stored = ctx.quotes.get(state.quote_id) if state.quote_id else None
    return Quote.model_validate(stored.payload) if stored else None


def informational(message):
    return bool(re.search(r'\b(?:what|wha|which|why|how|did you|have you|do you)\b|ايه|إيه|ما هي|ماهي', message, re.I))


def document_checklist(ctx):
    """Report document requirements without advancing or refreshing checkout."""
    from .documents import for_customer
    from .document_checks import type_label
    rows = [row for row in for_customer(ctx) if row.storage_key and row.status != 'deleted']
    received = sorted({type_label(row.document_type) for row in rows if row.status == 'checks_passed'})
    prefix = 'Received with initial checks passed: ' + ', '.join(received) + '. ' if received else ''
    state = ctx.load_state()
    if not state.checkout.get('residency'):
        return prefix + 'Is the driver a UAE resident, a tourist, or a GCC resident? This determines which documents are required.'
    missing, problems, _ = document_status(ctx, None, state.checkout)
    labels = [LABELS.get(kind, kind.replace('_',' ')) for kind in missing if kind != 'credit_card_in_driver_name']
    if labels:
        result = 'Still needed with readable details and validity through the rental: ' + ', '.join(labels) + '. Please attach them here.'
    else:
        result = 'The required identity and driving documents are on file; no additional copy is needed at this stage.'
    if 'credit_card_in_driver_name' in missing:
        result += ' Cardholder verification is not configured. Do not upload a bank card.'
    if problems:
        result += ' Still to establish: ' + ', '.join(p.replace('_',' ') for p in problems) + '.'
    if not state.return_date:
        result += ' The rental return date is needed to check validity through the full rental.'
    return prefix + result + ' This does not confirm rental eligibility or a booking.'


def date_status(ctx):
    state = ctx.load_state()
    if state.pickup_date and state.return_date:
        text = f'The dates saved for this enquiry are {state.pickup_date:%d %B %Y} to {state.return_date:%d %B %Y}.'
    else:
        text = 'The pickup and return dates for this enquiry are not both recorded. Please provide both dates.'
    reservation = ctx.reservations.get(state.reservation_id) if state.reservation_id else None
    if reservation and reservation.customer_id == ctx.customer_id:
        text += f' Existing booking {reservation.reservation_id} is for {reservation.pickup_at:%d %B %Y} to {reservation.return_at:%d %B %Y}.'
        if (state.pickup_date, state.return_date) != (reservation.pickup_at.date(), reservation.return_at.date()):
            text += ' It has not been changed. Say "new booking" for a separate rental, or give the booking reference and the change you want.'
    if state.pickup_date and state.return_date and not all(state.checkout.get(side+'_clock') for side in ['pickup','return']):
        text += ' Pickup and return times still need your confirmation for this enquiry.'
    return text


def information_reply(ctx, message):
    """Answer stored-fact questions before invoking quote tools or the model."""
    if not enabled(ctx): return None
    document_words = re.search(r'\b(?:documents?|docum?ents?|licen[cs]e|licence|passport|emirates id|id card)\b|مستند|رخصة|جواز|هوية', message, re.I)
    if document_words and (informational(message) or re.search(r'\b(?:expir\w*|date of birth|issued)\b', message, re.I)):
        from .documents import for_customer
        from .document_checks import notice
        rows = [row for row in for_customer(ctx) if row.status != 'deleted']
        if re.search(r'\bname\b|اسم', message, re.I):
            if not rows: return 'I have no saved document from you to check a name against.'
            return ('The automated reader extracts the name to compare your documents. The saved check keeps a protected comparison fingerprint, '
                    'not the readable name, so I cannot quote it from that record. Your original document is stored encrypted and may contain your name. '
                    'Passing these checks does not verify identity authenticity or confirm a booking.')
        if re.search(r'\b(?:just|last|latest)\b.*\b(?:receive|received|send|sent|upload)|\b(?:receive|received)\b', message, re.I):
            current = [row for row in rows if row.conversation_id == ctx.conversation_id]
            if not current: return 'I have no saved attachment in this chat yet. Please attach the document here.'
            return notice(current[-1])
        return document_checklist(ctx)
    if informational(message) and re.search(r'\bdates?\b|تواريخ|التاريخ', message, re.I):
        return date_status(ctx)
    # A date-only answer acknowledges the customer's dates, including in legacy
    # chats. It never modifies the existing reservation or revives a paid quote.
    from ..domain.dates import DATE
    normalized = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', message, flags=re.I)
    if DATE.search(normalized) and not re.search(r'\b(?:book|booking|reserve|pay|cancel|change|modify|extend|available|price|quote)\b', message, re.I):
        if ctx.load_state().reservation_id:
            return date_status(ctx)
    return None


def collection_claim_error(ctx, reply):
    """A conversational claim cannot approve an unknown collection policy."""
    if not enabled(ctx) or ctx.engine.rules.get('operator_location',{}).get('collection_available') is True:
        return None
    claim = re.search(
        r'\b(?:collection|(?:office|branch) pick\s?up)\s+(?:is|will be)\s+(?:possible|available|confirmed|arranged|ready)\b'
        r'|\b(?:you can|you may|welcome to)\s+(?:come\s+)?(?:collect|pick\s?up)\b'
        r'|\b(?:confirmed|approved|arranged)\s+(?:your\s+|the\s+)?(?:collection|(?:office|branch) pick\s?up)\b',reply,re.I)
    if not claim:
        claim = re.search(r'\b(?:registered|prepared|ready)\s+for\s+(?:your\s+)?collection\b|\b(?:your collection|collect(?:ion)? the car)\s+(?:at|from)\s+(?:our|the)\b',reply,re.I)
    if claim:
        return 'Collection from the office has not been confirmed by the company. I cannot confirm that arrangement. The collection policy must be configured before I can proceed with it.'


def reply_for_turn(ctx, message):
    """Take over committed checkout steps; ordinary browsing stays conversational."""
    if not enabled(ctx): return None
    answer = information_reply(ctx, message)
    if answer: return answer
    state = ctx.load_state(); flow = state.checkout
    if not flow.get('requested'):
        return qualification_reply(ctx, message)
    reservation = ctx.reservations.get(state.reservation_id) if state.reservation_id else None
    if reservation:
        # After a reservation exists, status questions must never silently open
        # another quote or erase the acceptance attached to that reservation.
        return None
    if re.search(r'\b(?:photo|picture|show|cheaper|what are|why|cancel|modify|extend)\b',message,re.I): return None
    if not state.selected_vehicle_id and len(state.current_vehicle_options)==1:
        state.selected_vehicle_id = state.current_vehicle_options[0]
        ctx.save_state(state)
    if not state.selected_vehicle_id: return 'Which car would you like to proceed with?'
    if not state.pickup_at or not state.return_at:
        return 'Please confirm the pickup and return dates and both times. For example: 10 September at 5pm to 13 September at 5pm.'
    error = time_error(ctx,state.pickup_at,state.return_at)
    if error: return error['message']
    from . import booking
    q = current_quote(ctx)
    desired_location = flow.get('address') if flow.get('fulfilment')=='delivery' else None
    if not q or q.vehicle_id!=state.selected_vehicle_id or q.pickup_at!=state.pickup_at or q.return_at!=state.return_at or q.delivery_location!=desired_location or q.expires_at<=ctx.now():
        result = booking.create_demo_quote(ctx,vehicle_id=state.selected_vehicle_id,pickup_at=state.pickup_at,
            return_at=state.return_at,delivery_location=desired_location)
        if result.get('error'): return result['message']
        q = current_quote(ctx)
    status = evaluate(ctx,q)
    if not status['ready']: return next_reply(ctx,q)
    # Once the exact terms have been accepted, another model request adds no
    # information. Complete this step through the gated, idempotent service.
    from ..tools.registry import execute_tool
    result = execute_tool(ctx,'create_demo_reservation',{'quote_id':q.quote_id})
    if result.get('error'):
        return result.get('message','The reservation could not be created. Please try again.')
    reference = result['reservation_id']
    if result.get('status') == 'held':
        return f'Demonstration request {reference} is awaiting availability confirmation. Payment is blocked until it is confirmed. No real vehicle is reserved.'
    return f'Demonstration reservation {reference} has been created using the accepted quote. Would you like the payment link? No real vehicle is reserved.'


def qualification_reply(ctx, message):
    """A factual answer to our age/residency question needs no model call.

    Recording driver details is not consent to book. This path may explain a
    prerequisite, but cannot create a quote, reservation, or payment link.
    """
    state = ctx.load_state(); flow = dict(state.checkout)
    text = message.strip().rstrip('.!').lower()
    age_answer = bool(re.fullmatch(r"(?:(?:i am|i'm|im|my age is)\s+)?\d{1,3}(?:\s+years? old)?",text)) and flow.get('driver_age') is not None
    residency_answer = text in {'uae resident','i am a uae resident','tourist','i am a tourist','visitor','gcc resident','i am a gcc resident'}
    followup = flow.get('requested') is not False and text in {'continue','ok','okay','yes','delivery','deliver it','pickup option','branch pickup','pick it up'}
    address_answer = flow.get('address') and message.strip() == flow['address'] and not informational(message)
    if not age_answer and not residency_answer and not ((followup or address_answer) and current_quote(ctx) and not state.reservation_id): return None
    if not flow.get('driver_age'):
        flow['asked'] = 'driver_age'; save(ctx,state,flow)
        return 'How old is the driver? I need this to check the rental requirements.'
    if not flow.get('residency'):
        flow['asked'] = 'residency'; save(ctx,state,flow)
        return 'Is the driver a UAE resident, a tourist, or a GCC resident? This determines the required documents.'
    q = current_quote(ctx)
    if q and q.vehicle_id == state.selected_vehicle_id:
        # Expiry matters when accepting/paying for a quote, not when answering
        # the age question. Never offer expired terms through this path.
        result = evaluate(ctx,q,check_expiry=False)
        if result['step'] not in {'acceptance','ready'}:
            flow['asked'] = result['step']; save(ctx,state,flow)
            return result['message']
        return 'Your driver details and configured checks are complete. Would you like to proceed with a fresh quote and rental terms? Nothing has been booked.'
    return document_checklist(ctx)
