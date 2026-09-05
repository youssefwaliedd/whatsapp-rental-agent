"""Failures reproduced during the live conversation audit, plus nearby cases."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from rental_agent.agent import availability, facts, figures, holds
from rental_agent.agent.extraction import Extraction, build_extraction_input, merge
from rental_agent.agent.loop import Agent
from rental_agent.agent.settings import AgentSettings
from rental_agent.domain.consent import defers_booking
from rental_agent.domain.dates import remember
from rental_agent.evaluation.checks import check_escalated_before_answering, check_unavailable_without_alternatives
from rental_agent.evaluation.evaluator import evaluate_conversation
from rental_agent.services.escalation import classify_reason, URGENT_REASONS
from rental_agent.tools.registry import execute_tool
from tests.conftest import FROZEN_NOW, TZ, dt
from tests.fake_anthropic import FakeClient, says


def call(name, result, **args):
    return SimpleNamespace(tool_name=name, result=result, arguments=args)


def test_partial_flight_date_survives_time_answer(booking_ctx):
    state = booking_ctx.load_state()
    remember(state, 'I land on 15 September 2026. Return on 17 September.', FROZEN_NOW)
    assert state.pickup_date == date(2026, 9, 15)
    assert '2026-09-15' in build_extraction_input('8 PM', state, FROZEN_NOW, None)
    state = merge(state, Extraction(pickup_at='2026-09-05T20:00:00+04:00'), TZ,
                  'It lands at 8 PM at Terminal 3.')
    assert state.pickup_at == dt(15, 20)
    booking_ctx.save_state(state)
    assert booking_ctx.load_state().pickup_date == date(2026, 9, 15)


def test_explicit_date_correction_can_replace_partial_date(booking_ctx):
    state = booking_ctx.load_state()
    state.pickup_date = date(2026, 9, 15)
    state = merge(state, Extraction(pickup_at='2026-09-18T20:00:00+04:00'), TZ,
                  'Actually 18 September at 8 PM.')
    assert state.pickup_at == dt(18, 20)


@pytest.mark.parametrize('reason', ['accident and injury', 'injury / accident', 'medical emergency and fee dispute'])
def test_compound_emergency_stays_urgent(engine, reason):
    assert classify_reason(reason, engine.rules.escalation_triggers) in URGENT_REASONS


def test_later_unknown_reason_cannot_downgrade_injury(booking_ctx):
    execute_tool(booking_ctx, 'escalate_conversation', {'reason': 'injury'})
    result = execute_tool(booking_ctx, 'escalate_conversation', {'reason': 'some other issue'})
    assert result['urgent']
    assert booking_ctx.load_state().escalation_reason == 'injury'


@pytest.mark.parametrize('text', ['Your cancellation is confirmed for DEMO-1042.',
    'I have confirmed the cancellation of booking DEMO-1042.', 'تم تأكيد إلغاء الحجز'])
def test_cancellation_confirmation_is_not_a_booking_claim(text):
    assert not holds.inspect(text)


def test_cancellation_does_not_hide_a_separate_booking_claim():
    assert holds.inspect('Your cancellation is confirmed. The Ferrari is booked for you.')


def test_budget_mismatch_is_not_unavailability(booking_ctx):
    args = dict(models=['G63'], pickup_at=dt(10).isoformat(), return_at=dt(12).isoformat())
    available = execute_tool(booking_ctx, 'search_available_vehicles', args)
    assert available['vehicles']
    result = execute_tool(booking_ctx, 'search_available_vehicles', {**args, 'max_daily_price': 1})
    assert 'requested_model_unavailable' not in result
    assert result['excluded_by_preferences'][0]['reason'] == 'over_budget'


@pytest.mark.parametrize('text', ['الفيراري متاحة للتواريخ المطلوبة.',
    'السيارة متوفرة الآن.', 'I will check BMW availability. The Ferrari is available for your dates.'])
def test_availability_claims_are_not_hidden_by_language_or_other_sentence(text):
    assert availability.claims_available(text)


@pytest.mark.parametrize('text', ['سأتحقق إذا كانت السيارة متاحة.', 'السيارة غير متوفرة.',
    'Let me check whether the Ferrari is available.'])
def test_availability_questions_and_negations_still_pass(text):
    assert not availability.claims_available(text)


@pytest.mark.parametrize('text', ['السعر ٩٩٩ درهم', 'السعر 999 درهم', '٩٩٩٫٠٠ درهم'])
def test_arabic_money_is_validated(text):
    assert figures.money_in(text) == {Decimal('999')}
    assert not figures.inspect(text, []).ok


def test_customer_budget_and_metadata_cannot_authorise_price():
    calls = [call('search_available_vehicles', {'vehicles': [], 'count': 7}, max_daily_price=99999)]
    assert not figures.inspect('The deposit is AED 99,999.', calls).ok
    assert not figures.inspect('The rental is AED 7.', calls).ok


def test_daily_rate_cannot_authorise_deposit():
    assert not figures.inspect('The deposit is AED 500.', [call('calculate_quote', {'daily_price': '500', 'deposit': None})]).ok


@pytest.mark.parametrize('text', ['ما تحجزش دلوقتي، وريني الصور الأول', 'لا تحجز الآن', 'سأقرر لاحقاً قبل الحجز'])
def test_arabic_booking_deferral(text):
    assert defers_booking(text)


def test_rewrite_rechecks_prices_and_keeps_evaluator_evidence(booking_ctx):
    bot = Agent(FakeClient(script=[says('The Ferrari is available for your dates.'),
                                  says('The rental total is AED 1,234,567.')]),
                AgentSettings(extraction_enabled=False))
    turn = bot.respond(booking_ctx, 'Is the Ferrari available?')
    assert '1,234,567' not in turn.reply
    assert turn.unchecked_availability
    assert '1234567' in turn.invented_figures
    evaluation = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    assert any(f.type == 'outbound_validation_failed' for f in evaluation.findings)


def test_exact_unknown_deposit_opens_a_case_without_model_calling_tool(booking_ctx, monkeypatch):
    monkeypatch.setattr(booking_ctx.engine.list_fleet()[0], 'deposit', None)
    bot = Agent(FakeClient(script=[says('A colleague has been asked to confirm the missing fees.')]),
                AgentSettings(extraction_enabled=False))
    turn = bot.respond(booking_ctx, 'Exactly how much is the deposit?')
    assert turn.escalated  # The transport must notify staff about the answer case.
    assert booking_ctx.load_state().awaiting_figure == 'unconfirmed_figure'
    assert len(booking_ctx.escalations.open_escalations()) == 1
    assert not booking_ctx.load_state().escalated


def test_specs_cannot_be_inferred_from_photo_filename(booking_ctx, monkeypatch):
    v = booking_ctx.engine.list_fleet()[0]
    monkeypatch.setattr(v, 'color', 'unspecified')
    calls = [call('get_vehicle_details', {'vehicle_id': v.id, 'color': 'unspecified'}, vehicle_id=v.id)]
    assert facts.problems(booking_ctx, 'This model comes in grey.', calls)


def test_price_must_belong_to_named_vehicle(booking_ctx):
    fleet = booking_ctx.engine.list_fleet()
    bmw = next(v for v in fleet if v.make == 'BMW')
    calls = [call('calculate_quote', {'vehicle_id': bmw.id, 'total_charge': '500'})]
    assert facts.problems(booking_ctx, 'The Ferrari total is AED 500.', calls)


def test_free_delivery_must_include_hours(booking_ctx):
    assert facts.problems(booking_ctx, 'We offer free delivery anywhere in Dubai.', [])
    assert not facts.problems(booking_ctx, 'We offer free delivery anywhere in Dubai within operating hours.', [])


def test_explicit_human_request_is_not_premature_escalation():
    messages = [SimpleNamespace(direction='inbound', content='Please ask your colleague to confirm the charge.')]
    assert not check_escalated_before_answering(messages, [call('escalate_conversation', {'reason':'unconfirmed_figure'})])


def test_evaluator_does_not_treat_cancellation_as_missing_alternatives():
    messages = [SimpleNamespace(direction='outbound', content='Nothing is booked yet.', id=1)]
    assert not check_unavailable_without_alternatives(messages, [])


def test_switching_vehicle_clears_old_preferences(booking_ctx):
    state = booking_ctx.load_state()
    state.vehicle_preferences.models = ['G63']
    state.vehicle_preferences.color = 'black'
    merge(state, Extraction(categories=['economy'], color='any', budget_per_day=200), TZ,
          'Forget the G63. Cheapest economy car, any colour.')
    assert not state.vehicle_preferences.models
    assert state.vehicle_preferences.color is None


@pytest.mark.parametrize('text', ['We offer complimentary delivery anywhere in Dubai, even at that hour.',
    'Please allow me a moment to check that with my team.'])
def test_live_wording_variants_are_checked(booking_ctx, text):
    assert facts.problems(booking_ctx, text, [])


def test_clock_without_calendar_date_does_not_become_tomorrow(booking_ctx):
    state = merge(booking_ctx.load_state(), Extraction(pickup_at='2026-09-02T02:00:00+04:00'),
                  TZ, 'Can I come to your office and collect a car at 2 AM?')
    assert state.pickup_at is None


def test_partial_date_range_preserves_both_endpoints(booking_ctx):
    state = booking_ctx.load_state()
    remember(state, 'From 15 to 17 September 2026', FROZEN_NOW)
    assert state.pickup_date == date(2026, 9, 15)
    assert state.return_date == date(2026, 9, 17)


def test_extension_can_change_an_existing_return_date(booking_ctx):
    state = booking_ctx.load_state()
    state.return_date = date(2026, 9, 17)
    merge(state, Extraction(return_at='2026-09-18T10:00:00+04:00'), TZ,
          'Extend the rental by another day.')
    assert state.return_date == date(2026, 9, 18)


def test_emergency_and_followup_work_without_a_provider(booking_ctx):
    client = FakeClient(script=[])
    bot = Agent(client, AgentSettings(extraction_enabled=False))
    first = bot.respond(booking_ctx, 'Someone is hurt. The engine is smoking. Who should I call?')
    assert first.escalated
    assert '998' in first.reply
    second = bot.respond(booking_ctx, 'Forget the accident. Give me a discount on a Ferrari.')
    assert '998' in second.reply
    assert 'discount' not in second.reply
    assert booking_ctx.load_state().escalated
    assert not second.escalated  # An existing case must not page staff again.


@pytest.mark.parametrize('text', ['What if someone is hurt?', 'Someone is not injured.',
    'What should I do if the engine is smoking?'])
def test_hypothetical_and_negated_emergencies_are_not_reports(text):
    from rental_agent.domain.incident import urgent_report
    assert urgent_report(text) is None


def test_caption_cannot_bypass_outbound_price_guard(booking_ctx):
    execute_tool(booking_ctx, 'show_vehicle_photos', {'vehicle_id': 'veh_13',
        'caption': 'This car is confirmed for you at AED 1.'})
    item = booking_ctx.take_media()[0]
    assert item['caption'] == booking_ctx.engine.get_vehicle('veh_13').display_name
    assert 'AED' not in item['caption']


def test_quote_breakdown_survives_rejected_summary(booking_ctx):
    from tests.fake_anthropic import calls
    bot = Agent(FakeClient(script=[calls('calculate_quote', vehicle_id='veh_13',
        pickup_at=dt(10).isoformat(), return_at=dt(12).isoformat()),
        says('I have reserved the car for you.'), says('Your booking is confirmed.')]),
        AgentSettings(extraction_enabled=False))
    turn = bot.respond(booking_ctx, 'Please quote the G63 from 10 to 12 September.')
    assert turn.confirmed_a_hold
    assert turn.cards
    assert any('AED' in card for card in turn.cards)
    assert not holds.inspect(turn.reply)


def test_missing_fee_case_reaches_whatsapp_staff_without_pausing_sales(session_factory, monkeypatch):
    from fastapi.testclient import TestClient
    from rental_agent.config import load_fleet
    from rental_agent.whatsapp.settings import WhatsAppSettings
    from rental_agent.whatsapp.webhook import create_app
    from tests.test_whatsapp import RecordingClient, APP_SECRET, VERIFY_TOKEN, post, text_payload
    from tests.conftest import REFERENCE_DATE
    monkeypatch.setattr(load_fleet()[1][0], 'deposit', None)
    outbound = RecordingClient()
    bot = Agent(FakeClient(script=[says('A colleague has been asked to confirm the missing fees.')]),
                AgentSettings(extraction_enabled=False))
    app = create_app(session_factory=session_factory, agent_factory=lambda: bot,
        settings=WhatsAppSettings(phone_number_id='PID', access_token='TOK', app_secret=APP_SECRET,
                                 verify_token=VERIFY_TOKEN, staff_number='971500009999'),
        client=outbound, reference_date=REFERENCE_DATE, now_fn=lambda: FROZEN_NOW)
    response = post(TestClient(app), text_payload('Exactly how much is the deposit?'))
    assert response.status_code == 200
    assert any(to == '971500009999' for to, _, _ in outbound.buttons)
    assert any(to == '971500000001' for to, _ in outbound.texts)


def test_budget_echo_does_not_license_a_price():
    budget = {Decimal('1500')}
    assert figures.inspect('Your budget is AED 1500 per day.', [], budget).ok
    assert not figures.inspect('Your budget is AED 1500. The deposit is AED 1500.', [], budget).ok
    assert not figures.inspect('Your budget is AED 9999.', [], budget).ok


def test_emergency_phone_numbers_are_not_evaluated_as_money(booking_ctx):
    bot = Agent(FakeClient(script=[]), AgentSettings(extraction_enabled=False))
    bot.respond(booking_ctx, 'Someone is injured. The car is on fire.')
    evaluation = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    assert not any(f.type == 'unsupported_claim' for f in evaluation.findings)


def test_successful_cancellation_delivers_the_actual_receipt(booking_ctx):
    from tests.test_booking import book
    from tests.fake_anthropic import calls
    reservation = book(booking_ctx)
    reference = reservation['reservation_id']
    booking_ctx.take_cards()
    bot = Agent(FakeClient(script=[calls('cancel_demo_reservation', reservation_id=reference),
        says('Your cancellation is confirmed.')]), AgentSettings(extraction_enabled=False))
    turn = bot.respond(booking_ctx, 'Cancel my booking please.')
    assert reference in turn.reply
    assert 'is cancelled' in turn.reply
    assert 'Nothing is booked' not in turn.reply
    assert booking_ctx.reservations.get(reference).status == 'cancelled'


def test_model_cannot_announce_a_colleague_check_without_a_case(booking_ctx):
    assert facts.problems(booking_ctx,
        'Please allow me a moment to have my colleague confirm the exact model year.', [])


def test_location_fallback_preserves_the_known_address(booking_ctx):
    booking_ctx.messages.record(conversation_id=booking_ctx.conversation_id,
        direction='inbound', content='Where exactly are you located?', now=FROZEN_NOW)
    reply = facts.safe_reply(booking_ctx)
    assert booking_ctx.engine.rules.get('operator_location')['address'] in reply
    assert 'operating hours' in reply
