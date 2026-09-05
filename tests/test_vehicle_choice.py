"""The manual browser report: liking a photo is neither a car ID nor a booking."""
from types import SimpleNamespace

import pytest

from rental_agent.agent import facts
from rental_agent.agent.extraction import Extraction, merge
from rental_agent.agent.loop import Agent, AgentTurn
from rental_agent.agent.settings import AgentSettings
from rental_agent.domain import selection
from rental_agent.domain.consent import defers_booking
from rental_agent.tools.registry import execute_tool
from tests.conftest import FROZEN_NOW, TZ, dt
from tests.fake_anthropic import FakeClient, says


def record(ctx, direction, text):
    ctx.messages.record(conversation_id=ctx.conversation_id, direction=direction,
                        content=text, now=FROZEN_NOW)


def options(ctx):
    record(ctx, 'outbound', 'Here are photos of the Mercedes-Benz C300 and BMW 530i. Which appeals to you?')


@pytest.mark.parametrize('message', ['okay i like this. what are the fees', 'I like it', 'Book that one'])
def test_ambiguous_choice_asks_without_model_quotes_or_bookings(booking_ctx, message):
    options(booking_ctx)
    client = FakeClient(script=[])
    turn = Agent(client, AgentSettings(extraction_enabled=False)).respond(booking_ctx, message)
    assert 'C300' in turn.reply and '530i' in turn.reply
    assert not turn.cards and not turn.tool_calls and not client.requests
    assert not booking_ctx.load_state().quote_id
    assert not booking_ctx.load_state().reservation_id


@pytest.mark.parametrize('tool', ['calculate_quote', 'create_demo_quote'])
def test_quote_handlers_reject_ambiguous_choice_before_writing(booking_ctx, tool):
    options(booking_ctx)
    record(booking_ctx, 'inbound', 'okay i like this. what are the fees')
    result = execute_tool(booking_ctx, tool, {'vehicle_id': 'veh_15',
        'pickup_at': dt(6,17).isoformat(), 'return_at': dt(10,17).isoformat()})
    assert result['error'] == 'vehicle_choice_required'
    assert not booking_ctx.take_cards()
    assert not booking_ctx.load_state().quote_id


@pytest.mark.parametrize('tool', ['calculate_quote', 'create_demo_quote'])
def test_explicit_choice_cannot_be_replaced_by_another_vehicle(booking_ctx, tool):
    options(booking_ctx)
    record(booking_ctx, 'inbound', 'The C300 please. What are the fees?')
    args = {'pickup_at': dt(6,17).isoformat(), 'return_at': dt(10,17).isoformat()}
    wrong = execute_tool(booking_ctx, tool, {**args, 'vehicle_id': 'veh_15'})
    assert wrong['error'] == 'vehicle_choice_mismatch'
    right = execute_tool(booking_ctx, tool, {**args, 'vehicle_id': 'veh_07'})
    assert right['vehicle_id'] == 'veh_07' and 'error' not in right
    assert all('C300' in card for card in booking_ctx.take_cards())


def test_photo_shortlist_replaces_the_earlier_search_list(booking_ctx):
    state = booking_ctx.load_state()
    state.current_vehicle_options = ['veh_07','veh_08','veh_15']
    booking_ctx.save_state(state)
    selection.remember_options(booking_ctx, AgentTurn(reply='Here are both cars.',
        media=[{'vehicle_id':'veh_07'}, {'vehicle_id':'veh_08'}]))
    assert selection.current_options(booking_ctx) == ['veh_07','veh_08']
    assert selection.resolve(booking_ctx, 'the second one') == ['veh_08']
    assert not selection.ambiguous(booking_ctx, 'show me photos of both')
    assert not selection.ambiguous(booking_ctx, 'which ones the cheapest?')


def test_general_fees_question_opens_a_real_answer_request(booking_ctx, monkeypatch):
    monkeypatch.setattr(booking_ctx.engine.get_vehicle('veh_07'), 'deposit', None)
    state = booking_ctx.load_state()
    state.selected_vehicle_id = 'veh_07'
    booking_ctx.save_state(state)
    reply = ('I will be able to give you the confirmed figure for the car you choose '
             'as soon as we move to the next step.')
    turn = Agent(FakeClient(script=[says(reply)]), AgentSettings(extraction_enabled=False)).respond(
        booking_ctx, 'I like the C300. What are the fees?')
    assert 'recorded a request' in turn.reply
    assert 'next step' not in turn.reply
    assert turn.escalated and not booking_ctx.load_state().escalated
    assert len(booking_ctx.escalations.open_escalations()) == 1


def test_next_step_deposit_promise_is_detected(booking_ctx, monkeypatch):
    monkeypatch.setattr(booking_ctx.engine.list_fleet()[0], 'deposit', None)
    assert facts.problems(booking_ctx,
        'I can give you the confirmed figure as soon as we move to the next step.', [])


def test_nevermind_clears_the_old_vehicle_and_quote(booking_ctx):
    state = booking_ctx.load_state()
    state.vehicle_preferences.models = ['Urus']
    state.selected_vehicle_id = 'veh_15'
    state.quote_id = 'DQ-501'
    state.current_vehicle_options = ['veh_15']
    merge(state, Extraction(makes=['Mercedes']), TZ, 'nevermind i want a mercedes')
    assert not state.vehicle_preferences.models
    assert state.vehicle_preferences.makes == ['Mercedes']
    assert not state.selected_vehicle_id and not state.quote_id
    assert not state.current_vehicle_options


def test_liking_a_car_does_not_authorise_a_reservation(booking_ctx):
    from tests.test_booking import quote_for
    quote = quote_for(booking_ctx)
    record(booking_ctx, 'inbound', 'I like it. What are the fees?')
    result = execute_tool(booking_ctx, 'create_demo_reservation', {'quote_id': quote['quote_id']})
    assert result['error'] == 'not_asked_for_yet'
    assert not booking_ctx.load_state().reservation_id
    assert not defers_booking('I like it. Book it now please.')


def test_clear_choice_is_remembered_for_the_following_turn(booking_ctx):
    options(booking_ctx)
    record(booking_ctx, 'inbound', 'The C300 please.')
    selection.remember_customer_choice(booking_ctx, 'The C300 please.')
    record(booking_ctx, 'outbound', 'A colleague needs to confirm the missing fees.')
    record(booking_ctx, 'inbound', 'Yes, go ahead.')
    assert selection.current_options(booking_ctx) == ['veh_07']
    assert selection.quote_error(booking_ctx, 'veh_08')['error'] == 'vehicle_choice_mismatch'
    assert selection.quote_error(booking_ctx, 'veh_07') is None


def test_booking_cannot_use_a_quote_for_a_different_car(booking_ctx):
    from tests.test_booking import quote_for
    quote = quote_for(booking_ctx, vehicle_id='veh_13')
    record(booking_ctx, 'inbound', 'Book the C300 please.')
    result = execute_tool(booking_ctx, 'create_demo_reservation', {'quote_id': quote['quote_id']})
    assert result['error'] == 'vehicle_choice_mismatch'
    assert not booking_ctx.load_state().reservation_id
