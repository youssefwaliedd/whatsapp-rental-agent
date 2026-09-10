"""Provider outages after real searches, no network and no customer data."""
import pytest
from sqlalchemy import select, func

from rental_agent.agent.loop import Agent, PROVIDER_BUSY_REPLY
from rental_agent.agent.settings import AgentSettings
from rental_agent.agent.providers.errors import ProviderUnavailable
from rental_agent.store.models import Reservation
from tests.conftest import dt
from tests.fake_anthropic import FakeClient, calls


def search(**overrides):
    args={'pickup_at':dt(10,17).isoformat(),'return_at':dt(15,17).isoformat()}
    args.update(overrides)
    return calls('search_available_vehicles',**args)


@pytest.mark.parametrize('durable',[False,True])
def test_successful_search_survives_response_outage(booking_ctx,durable):
    ctx=booking_ctx;ctx.session.info['durable_turn']=durable
    client=FakeClient(script=[search(),ProviderUnavailable('timeout_or_deadline (HTTP 504)')])
    agent=Agent(client,AgentSettings(extraction_enabled=False))
    turn=agent.respond(ctx,'open budget give me your best car')
    assert turn.reply.startswith('Here are the options returned'),turn.reply
    assert 'per day' in turn.reply and 'demonstration calendar' in turn.reply
    assert 'send that again' not in turn.reply and 'Nothing has been booked' in turn.reply
    assert '504' in turn.provider_error
    assert turn.tools_succeeded==['search_available_vehicles']
    assert len(client.requests)==2
    assert not turn.invented_figures and not turn.unchecked_availability
    assert ctx.load_state().current_vehicle_options
    assert not ctx.load_state().consecutive_provider_failures
    assert ctx.session.scalar(select(func.count()).select_from(Reservation))==0


def test_failed_later_search_cannot_reuse_earlier_results(booking_ctx):
    client=FakeClient(script=[search(),search(return_at='bad-date'),ProviderUnavailable('503')])
    turn=Agent(client,AgentSettings(extraction_enabled=False)).respond(booking_ctx,'show cars')
    assert turn.reply==PROVIDER_BUSY_REPLY


def test_empty_later_search_cannot_reuse_earlier_results(booking_ctx):
    client=FakeClient(script=[search(),search(max_daily_price=1),ProviderUnavailable('503')])
    turn=Agent(client,AgentSettings(extraction_enabled=False)).respond(booking_ctx,'show cars')
    assert turn.reply==PROVIDER_BUSY_REPLY


def test_previous_turn_results_are_never_reused(booking_ctx):
    from tests.fake_anthropic import says
    client=FakeClient(script=[search(),says('What type of car do you prefer?'),ProviderUnavailable('503')])
    bot=Agent(client,AgentSettings(extraction_enabled=False))
    bot.respond(booking_ctx,'show cars')
    assert bot.respond(booking_ctx,'different dates please').reply==PROVIDER_BUSY_REPLY


def test_search_recovery_never_masks_payment_or_booking_attempt(booking_ctx):
    client=FakeClient(script=[search(),calls('create_demo_reservation',quote_id='missing'),ProviderUnavailable('503')])
    turn=Agent(client,AgentSettings(extraction_enabled=False)).respond(booking_ctx,'show cars')
    assert turn.reply==PROVIDER_BUSY_REPLY
    assert booking_ctx.session.scalar(select(func.count()).select_from(Reservation))==0


def test_browser_delivers_and_persists_recovered_search(session_factory):
    from fastapi.testclient import TestClient
    from rental_agent.webchat.app import create_app
    from tests.conftest import FROZEN_NOW, REFERENCE_DATE
    llm=FakeClient(script=[search(),ProviderUnavailable('504 deadline')])
    bot=Agent(llm,AgentSettings(extraction_enabled=False))
    app=create_app(session_factory=session_factory,agent_factory=lambda:bot,
        now_fn=lambda:FROZEN_NOW,reference_date=REFERENCE_DATE)
    web=TestClient(app)
    result=web.post('/api/message',json={'message':'open budget give me your best car'}).json()
    assert result['reply'].startswith('Here are the options returned'),result
    assert result['tools']==['search_available_vehicles']
    assert result['provider_error']
    history=web.get('/api/state').json()['history']
    assert history[-1]['text']==result['reply']
    assert len(llm.requests)==2
