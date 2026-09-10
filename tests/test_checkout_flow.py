"""Strict checkout integration, with fictional documents and no network."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from rental_agent.services import checkout, booking, payments
from rental_agent.services.document_checks import DocumentChecker
from rental_agent.store.models import Reservation, CustomerDocument, DocumentFacts
from rental_agent.tools.registry import execute_tool
from rental_agent.whatsapp.storage import EncryptedStore, DiskBackend
from tests.conftest import dt, FROZEN_NOW, REFERENCE_DATE
from tests.test_automated_documents import Reader, receipt


def say(ctx,text):
    ctx.messages.record(conversation_id=ctx.conversation_id,direction='inbound',content=text,now=ctx.now())
    checkout.observe(ctx,text)


@pytest.fixture
def strict(booking_ctx, monkeypatch, tmp_path):
    ctx = booking_ctx
    monkeypatch.setitem(ctx.engine.rules._data['booking'],'enforce_checkout_flow',True)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('DOCUMENT_ENCRYPTION_KEY',key)
    store = EncryptedStore(DiskBackend(tmp_path/'docs'),key)
    say(ctx,'I want a new booking, September 10th at 5pm until September 13th at 5pm')
    state=ctx.load_state();state.selected_vehicle_id='veh_07';ctx.save_state(state)
    result=booking.create_demo_quote(ctx,vehicle_id='veh_07',pickup_at=dt(10,17),return_at=dt(13,17),delivery_location='Dubai Marina')
    assert not result.get('error'),result
    return ctx,store


def details(ctx):
    say(ctx,'I am 31 years old, a UAE resident. Delivery to Dubai Marina')


def docs(ctx,store,**changes):
    for kind in ['emirates_id','driving_license']:
        values={'document_type':kind,'date_of_birth':date(1995,1,1),'issuing_country':'AE','issue_date':date(2015,1,1)}
        values.update(changes)
        checker=DocumentChecker(Reader(**values),match_key='sample-match-key')
        checker.check(ctx,receipt(ctx,store,suffix=uuid4().hex),store)
    ctx.session.flush()


def accept(ctx):
    text=checkout.next_reply(ctx,checkout.current_quote(ctx))
    assert text.startswith('Rental terms'),text
    ctx.messages.record(conversation_id=ctx.conversation_id,direction='outbound',content=text,now=ctx.now())
    say(ctx,checkout.ACCEPT)


def reserve(ctx):
    return execute_tool(ctx,'create_demo_reservation',{'quote_id':ctx.load_state().quote_id})


def test_missing_age_and_documents_block_reservation(strict):
    ctx,store=strict
    result=reserve(ctx);assert result['step']=='driver_age'
    say(ctx,'I am 31')
    assert reserve(ctx)['step']=='residency'
    say(ctx,'I am a UAE resident')
    result=reserve(ctx);assert result['step']=='documents'
    assert 'Emirates ID' in result['message'] and 'UAE driving licence' in result['message']
    assert ctx.session.scalar(select(func.count()).select_from(Reservation))==0


def test_demo_document_flags_cannot_satisfy_gate(strict):
    ctx,store=strict;details(ctx)
    customer=ctx.customers.get(ctx.customer_id);customer.documents_on_file=['emirates_id','uae_driving_licence']
    assert reserve(ctx)['step']=='documents'
    assert booking.record_demo_documents(ctx,documents=customer.documents_on_file)['error']=='attachment_checks_required'


def test_complete_flow_requires_acceptance_then_allows_payment(strict):
    ctx,store=strict;details(ctx);docs(ctx,store)
    assert reserve(ctx)['step']=='acceptance'
    say(ctx,'yes, everything is approved, give me the payment link')
    assert reserve(ctx)['step']=='acceptance'
    accept(ctx)
    booked=reserve(ctx);assert 'error' not in booked,booked
    link=payments.create_payment_link(ctx,reservation_id=booked['reservation_id'])
    assert 'error' not in link,link
    assert Decimal(link['amount'])==checkout.current_quote(ctx).total_charge


@pytest.mark.parametrize('changes,step',[
    ({'expiry_date':date(2026,9,11)},'documents'),
    ({'date_of_birth':date(2000,1,1)},'document_eligibility'),
    ({'date_of_birth':None},'document_eligibility'),
    ({'issuing_country':'US'},'documents')])
def test_unsuitable_document_facts_block(strict,changes,step):
    ctx,store=strict;details(ctx);docs(ctx,store,**changes)
    assert reserve(ctx)['step']==step


def test_unknown_fees_and_collection_policy_block(strict):
    ctx,store=strict;details(ctx);docs(ctx,store)
    q=checkout.current_quote(ctx)
    assert checkout.evaluate(ctx,q.model_copy(update={'deposit':None}))['step']=='company_fees'
    assert checkout.evaluate(ctx,q.model_copy(update={'extra_km_price':None}))['step']=='company_fees'
    assert checkout.evaluate(ctx,q.model_copy(update={'insurance_excess_is_minimum':True}))['step']=='company_fees'
    say(ctx,'pickup option')
    assert reserve(ctx)['step']=='collection_policy'


def test_unverified_collection_claim_is_replaced_even_in_an_old_chat(strict):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient, says
    ctx,store=strict
    claim='I have checked with my colleague and we can confirm that collection is possible.'
    assert checkout.collection_claim_error(ctx,claim)
    assert checkout.collection_claim_error(ctx,'You can collect it from our office.')
    assert checkout.collection_claim_error(ctx,'Collection is not available.') is None
    result=Agent(FakeClient(script=[says(claim)]),AgentSettings(extraction_enabled=False)).respond(ctx,'so what do you need')
    assert 'has not been confirmed' in result.reply


def test_terms_cannot_be_accepted_before_they_were_presented(strict):
    ctx,store=strict;details(ctx);docs(ctx,store)
    say(ctx,checkout.ACCEPT)
    assert reserve(ctx)['step']=='acceptance'


def test_deferring_booking_stops_checkout_and_revokes_acceptance(strict):
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    say(ctx,"Do not book yet, first show me the pictures")
    assert checkout.reply_for_turn(ctx,'continue') is None
    assert not ctx.load_state().checkout.get('accepted')
    assert reserve(ctx)['error']=='checkout_incomplete'
    assert booking.create_demo_reservation(ctx,quote_id=ctx.load_state().quote_id)['error']=='not_asked_for_yet'


def test_policy_change_invalidates_acceptance_and_cached_booking(strict,monkeypatch):
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    assert 'error' not in reserve(ctx)
    monkeypatch.setitem(ctx.engine.rules._data['cancellation'],'late_cancellation_fee_percent',99)
    assert reserve(ctx)['step']=='acceptance'


def test_old_incomplete_booking_cannot_get_payment_link(strict):
    ctx,store=strict
    q=checkout.current_quote(ctx)
    row=Reservation(reservation_id='LEGACY-1',customer_id=ctx.customer_id,conversation_id=ctx.conversation_id,
        vehicle_id=q.vehicle_id,quote_id=q.quote_id,pickup_at=q.pickup_at,return_at=q.return_at,
        total_charge=q.total_charge,deposit=q.deposit,currency=q.currency,status='confirmed',
        created_at=ctx.now(),updated_at=ctx.now(),history=[])
    ctx.session.add(row);ctx.session.flush()
    assert payments.create_payment_link(ctx,reservation_id=row.reservation_id)['step']=='driver_age'


def test_new_enquiry_does_not_modify_old_booking(strict):
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    old=reserve(ctx);reference=old['reservation_id']
    before=ctx.reservations.get(reference).return_at
    say(ctx,'I want a new booking')
    assert ctx.load_state().reservation_id is None
    assert ctx.load_state().pickup_at is None
    assert not booking.get_active_reservation(ctx)['has_active_reservation']
    say(ctx,'change it to 5 days')
    blocked=booking.modify_demo_reservation(ctx,reservation_id=reference,return_at=dt(15,17))
    assert blocked['error']=='booking_reference_required'
    assert ctx.reservations.get(reference).return_at==before
    assert checkout.mutation_error(ctx,reference)


def test_new_enquiry_upload_does_not_attach_to_old_reservation(strict):
    from types import SimpleNamespace
    from rental_agent.services import documents
    from tests.test_document_collection import PDF
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx);reserve(ctx)
    say(ctx,'I want a new booking')
    message=SimpleNamespace(message_id='new-file',media_kind='document',raw={'document':{'id':'sample'}})
    row=documents.receive(ctx,message,store,lambda _:(PDF,'application/pdf'))
    assert row.reservation_id is None


def test_malformed_model_dates_return_an_error_instead_of_crashing(strict):
    ctx,store=strict
    result=execute_tool(ctx,'create_demo_quote',{'vehicle_id':'veh_07','pickup_at':'not a date','return_at':'tomorrow'})
    assert result['error']=='invalid_request'


def test_new_dates_never_inherit_time(strict):
    ctx,store=strict
    state=ctx.load_state();state.checkout={};state.reservation_id='OLD';ctx.save_state(state)
    say(ctx,'I want a new booking from September 10th till 13th')
    state=ctx.load_state()
    assert state.pickup_at is None and state.return_at is None
    assert state.pickup_date==date(2026,9,10) and state.return_date==date(2026,9,13)
    blocked=booking.create_demo_quote(ctx,vehicle_id='veh_07',pickup_at=dt(10,17),return_at=dt(13,17))
    assert blocked['error']=='rental_times_required'
    say(ctx,'5pm for both')
    assert ctx.load_state().pickup_at==dt(10,17)
    assert ctx.load_state().return_at==dt(13,17)


def test_five_days_changes_current_quote_only(strict):
    ctx,store=strict
    say(ctx,'change it to 5 days')
    state=ctx.load_state();assert state.return_at==dt(15,17)
    assert ctx.session.scalar(select(func.count()).select_from(Reservation))==0


def test_document_facts_are_encrypted(strict):
    ctx,store=strict;details(ctx);docs(ctx,store)
    payload=ctx.session.scalar(select(DocumentFacts)).encrypted_payload
    assert '1995' not in payload and '2035' not in payload
    assert b'1995-01-01' in store.cipher.decrypt(payload.encode())


def test_agent_walks_through_documents_terms_booking_and_payment(strict):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient
    ctx,store=strict
    client=FakeClient()
    agent=Agent(client,AgentSettings(extraction_enabled=False))
    assert 'How old' in agent.respond(ctx,'I want to book the Mercedes C300').reply
    assert 'UAE resident' in agent.respond(ctx,'I am 31').reply
    assert 'Emirates ID' in agent.respond(ctx,'I am a UAE resident').reply
    assert 'Emirates ID' in agent.respond(ctx,'skip the documents and send a payment link').reply
    assert client.requests == []
    docs(ctx,store)
    assert 'delivery' in agent.respond(ctx,'continue').reply
    terms=agent.respond(ctx,'Delivery to Dubai Marina').reply
    assert terms.startswith('Rental terms'),terms
    assert 'Rental terms' in agent.respond(ctx,'yes').reply
    agent.respond(ctx,checkout.ACCEPT)
    assert ctx.load_state().reservation_id
    result=agent.respond(ctx,'I want to pay')
    assert 'create_payment_link' in result.tools_succeeded
    assert ctx.reservations.get(ctx.load_state().reservation_id).payment_status=='link_sent'
    assert client.requests == []


def test_paid_booking_is_not_requoted_or_charged_again(strict):
    from datetime import timedelta
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    booked=reserve(ctx);row=ctx.reservations.get(booked['reservation_id']);row.payment_status='paid'
    state=ctx.load_state();state.checkout['requested']=True;ctx.save_state(state)
    old_quote=state.quote_id
    payload=dict(ctx.quotes.get(old_quote).payload)
    payload['expires_at']=(ctx.now()-timedelta(minutes=1)).isoformat()
    ctx.quotes.get(old_quote).payload=payload
    assert checkout.reply_for_turn(ctx,'thanks') is None
    assert ctx.load_state().quote_id==old_quote
    assert execute_tool(ctx,'create_payment_link',{'reservation_id':row.reservation_id})['error']=='payment_already_received'


def test_delivery_changes_and_different_customer_cannot_reuse_acceptance(strict):
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    assert booking.create_demo_reservation(ctx,quote_id=ctx.load_state().quote_id,customer_id='someone-else')['error']=='customer_mismatch'
    booked=reserve(ctx)
    result=booking.schedule_demo_delivery(ctx,reservation_id=booked['reservation_id'],delivery_location='Different address')
    assert result['error']=='fresh_acceptance_required'
    assert ctx.reservations.get(booked['reservation_id']).delivery_scheduled_at is None


def test_delivery_address_is_preserved_beyond_its_pricing_zone(strict):
    ctx,store=strict;details(ctx);docs(ctx,store)
    say(ctx,'I want to book this. Delivery to Sample Tower, Dubai Marina.')
    response=checkout.reply_for_turn(ctx,'continue')
    assert response.startswith('Rental terms'),response
    q=checkout.current_quote(ctx)
    assert q.delivery_location=='Sample Tower, Dubai Marina.'
    accept(ctx)
    booked=reserve(ctx)
    assert ctx.reservations.get(booked['reservation_id']).delivery_location==q.delivery_location


def test_hold_confirmation_preserves_accepted_quote_and_rechecks_documents(strict,monkeypatch):
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    monkeypatch.setitem(ctx.engine.rules._data['booking'],'holds_require_confirmation',True)
    accept(ctx)
    booked=reserve(ctx);row=ctx.reservations.get(booked['reservation_id'])
    assert row.status=='held'
    quote_id=row.quote_id
    document=ctx.session.scalar(select(CustomerDocument))
    document.status='needs_replacement';ctx.session.flush()
    assert booking.confirm_hold(ctx,row)['outcome']=='conflict'
    assert row.status=='held'
    document.status='checks_passed';ctx.session.flush()
    assert booking.confirm_hold(ctx,row)['outcome']=='confirmed'
    assert row.quote_id==quote_id


def test_browser_upload_records_checks_and_survives_reload(session_factory,tmp_path,monkeypatch):
    from rental_agent.webchat.app import create_app
    from tests.test_document_collection import PDF
    from tests.test_webchat import StubAgent
    store=EncryptedStore(DiskBackend(tmp_path/'uploads'),Fernet.generate_key().decode())
    reader=Reader(document_type='emirates_id',date_of_birth=date(1995,1,1),issuing_country='AE')
    app=create_app(session_factory=session_factory,agent_factory=StubAgent,now_fn=lambda:FROZEN_NOW,
        reference_date=REFERENCE_DATE,document_store=store,document_checker=DocumentChecker(reader,match_key='test'))
    web=TestClient(app)
    headers={'content-type':'application/pdf','x-sample-document':'1','x-upload-id':uuid4().hex}
    response=web.post('/api/documents',content=PDF,headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['state']['documents'][0]['status']=='checks_passed'
    assert web.post('/api/documents',content=PDF,headers=headers).status_code==200
    assert reader.calls==1
    assert len(web.get('/api/state').json()['history'])==2
    assert web.post('/api/documents',content=PDF).status_code==403
    headers['x-upload-id']=uuid4().hex
    assert web.post('/api/documents',content=b'not a PDF',headers=headers).status_code==400


def test_browser_encryption_key_survives_process_environment_reset(tmp_path,monkeypatch):
    from rental_agent.whatsapp.storage import build_browser_store, existing_browser_cipher
    monkeypatch.delenv('DOCUMENT_ENCRYPTION_KEY',raising=False)
    monkeypatch.setenv('WHATSAPP_DOCUMENT_DIR',str(tmp_path/'persistent-docs'))
    monkeypatch.setenv('DOCUMENT_STORAGE','local')
    store=build_browser_store()
    encrypted=store.cipher.encrypt(b'fictional date of birth')
    monkeypatch.delenv('DOCUMENT_ENCRYPTION_KEY')
    assert existing_browser_cipher().decrypt(encrypted)==b'fictional date of birth'


def test_document_questions_escape_expired_legacy_booking_date_loop(strict):
    from datetime import timedelta
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient
    ctx,store=strict;details(ctx);docs(ctx,store);accept(ctx)
    booked=reserve(ctx);row=ctx.reservations.get(booked['reservation_id'])
    row.payment_status='paid';row.return_at=dt(15,17)
    state=ctx.load_state();quote_id=state.quote_id
    state.checkout={'residency':'uae_resident','asked':'refresh_quote'}
    ctx.save_state(state)
    payload=dict(ctx.quotes.get(quote_id).payload)
    payload['expires_at']=(ctx.now()-timedelta(days=1)).isoformat()
    payload['return_at']=dt(15,17).isoformat()
    ctx.quotes.get(quote_id).payload=payload
    # Give the latest attachment an unambiguous receipt time.
    licence=ctx.session.scalar(select(CustomerDocument).where(CustomerDocument.document_type=='driving_license'))
    licence.created_at=ctx.now()+timedelta(seconds=1)
    ctx.session.flush()
    client=FakeClient();agent=Agent(client,AgentSettings(extraction_enabled=True))
    response=agent.respond(ctx,'what document did you just receive').reply
    assert 'driving licence' in response and licence.document_id in response
    assert 'dates' not in response and 'expired' not in response
    response=agent.respond(ctx,'what was the name on the driving license').reply
    assert 'fingerprint' in response and 'original document is stored encrypted' in response
    assert "doesn't display or store" not in response
    response=agent.respond(ctx,'wha documents do you need. i sent teh emirates id and driving license').reply
    assert 'no additional copy' in response
    assert 'passport' not in response and 'quote has expired' not in response
    response=agent.respond(ctx,'september 10th till 13th').reply
    assert '10 September 2026 to 13 September 2026' in response
    assert '15 September 2026' in response and booked['reservation_id'] in response
    assert 'new booking' in response
    assert '10 September 2026 to 13 September 2026' in agent.respond(ctx,'what dates are in your inquiry').reply
    assert ctx.load_state().quote_id==quote_id
    assert row.return_at==dt(15,17) and row.payment_status=='paid'
    assert ctx.session.scalar(select(func.count()).select_from(Reservation))==1
    assert client.requests==[]


def test_document_checklist_uses_current_enquiry_end_not_old_quote(strict):
    ctx,store=strict;details(ctx);docs(ctx,store,expiry_date=date(2026,9,14))
    quote_id=ctx.load_state().quote_id
    payload=dict(ctx.quotes.get(quote_id).payload);payload['return_at']=dt(15,17).isoformat()
    ctx.quotes.get(quote_id).payload=payload
    assert 'no additional copy' in checkout.document_checklist(ctx)
    say(ctx,'September 10th till 16th')
    response=checkout.document_checklist(ctx)
    assert 'Still needed' in response and 'UAE driving licence' in response and 'Emirates ID' in response


def test_document_expiry_and_status_questions_do_not_change_rental_dates(strict):
    ctx,store=strict
    say(ctx,'My driving licence expires September 20th 2035')
    assert ctx.load_state().pickup_date==date(2026,9,10)
    say(ctx,'what dates do you have, September 12th or September 14th?')
    assert ctx.load_state().pickup_date==date(2026,9,10)
    state=ctx.load_state();state.checkout['asked']='delivery_address';ctx.save_state(state)
    before=state.delivery_location
    say(ctx,'what documents do you need')
    assert ctx.load_state().delivery_location==before


def test_changed_legacy_dates_do_not_inherit_clock(strict):
    ctx,store=strict
    state=ctx.load_state();state.checkout={};state.reservation_id='LEGACY';ctx.save_state(state)
    say(ctx,'September 11th till 14th')
    assert ctx.load_state().pickup_at is None and ctx.load_state().return_at is None
    say(ctx,'5pm both')
    assert ctx.load_state().pickup_at==dt(11,17) and ctx.load_state().return_at==dt(14,17)


def test_checklist_does_not_assume_residency_or_accept_failed_documents(strict):
    ctx,store=strict
    docs(ctx,store)
    assert 'UAE resident, a tourist, or a GCC resident' in checkout.document_checklist(ctx)
    say(ctx,'I am a UAE resident')
    row=ctx.session.scalar(select(CustomerDocument).where(CustomerDocument.document_type=='driving_license'))
    row.status='needs_replacement';ctx.session.flush()
    response=checkout.document_checklist(ctx)
    assert 'Still needed' in response and 'UAE driving licence' in response
    assert 'no additional copy' not in response


@pytest.mark.parametrize('channel',['browser','whatsapp'])
def test_upload_receipt_uses_checklist_even_with_expired_quote(strict,session_factory,monkeypatch,channel):
    from datetime import timedelta
    from rental_agent.webchat.app import create_app as browser_app
    from rental_agent.whatsapp.webhook import create_app as whatsapp_app
    from tests.test_document_collection import PDF, SETTINGS, attachment
    from tests.test_whatsapp import RecordingClient, StubAgent, post
    from rental_agent.agent.loop import AgentTurn
    ctx,store=strict;details(ctx);docs(ctx,store)
    quote_id=ctx.load_state().quote_id
    payload=dict(ctx.quotes.get(quote_id).payload)
    payload['expires_at']=(ctx.now()-timedelta(days=1)).isoformat()
    ctx.quotes.get(quote_id).payload=payload
    ctx.session.commit()
    monkeypatch.setattr(checkout,'enabled',lambda _:True)
    checker=DocumentChecker(Reader(document_type='emirates_id',date_of_birth=date(1995,1,1),issuing_country='AE'),match_key='sample-match-key')
    agent=StubAgent(AgentTurn(reply='Must not be called for an upload'))
    common=dict(session_factory=session_factory,agent_factory=lambda:agent,now_fn=lambda:FROZEN_NOW,
        reference_date=REFERENCE_DATE,document_store=store,document_checker=checker)
    if channel=='browser':
        web=TestClient(browser_app(**common))
        response=web.post('/api/documents',params={'handle':'+971500000001'},content=PDF,
            headers={'content-type':'application/pdf','x-sample-document':'1','x-upload-id':uuid4().hex})
        assert response.status_code==200,response.text
        reply=response.json()['reply']
    else:
        client=RecordingClient()
        web=TestClient(whatsapp_app(**common,settings=SETTINGS,client=client,media_downloader=lambda _:(PDF,'application/pdf')))
        assert post(web,attachment(sender='+971500000001')).status_code==200
        reply=client.texts[-1][1]
    assert 'Received: Emirates ID' in reply
    assert 'no additional copy' in reply
    assert 'quote has expired' not in reply and 'Those dates' not in reply
    ctx.session.expire_all()
    assert ctx.load_state().quote_id==quote_id
    assert ctx.session.scalar(select(func.count()).select_from(Reservation))==0


def test_bare_age_after_model_question_needs_no_model_or_booking_consent(strict):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient
    ctx,store=strict
    state=ctx.load_state();state.checkout.pop('asked',None);state.checkout['residency']='uae_resident';ctx.save_state(state)
    ctx.messages.record(conversation_id=ctx.conversation_id,direction='outbound',
        content='For our insurance and registration requirements, may I please ask your age?',now=ctx.now())
    client=FakeClient();bot=Agent(client,AgentSettings(extraction_enabled=True))
    response=bot.respond(ctx,'25').reply
    assert ctx.load_state().checkout['driver_age']==25
    assert 'Emirates ID' in response and 'UAE driving licence' in response
    assert not client.requests
    assert not ctx.load_state().checkout.get('requested')
    assert ctx.session.scalar(select(func.count()).select_from(Reservation))==0


def test_residency_then_age_with_documents_advances_without_ai(strict):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient
    ctx,store=strict;docs(ctx,store)
    client=FakeClient();bot=Agent(client,AgentSettings(extraction_enabled=True))
    assert 'How old' in bot.respond(ctx,'uae resident').reply
    assert 'delivery' in bot.respond(ctx,'31').reply
    assert not client.requests
    assert not ctx.load_state().reservation_id


def test_bare_number_without_age_question_is_not_age(strict):
    ctx,store=strict
    say(ctx,'25')
    assert not ctx.load_state().checkout.get('driver_age')
    assert checkout.qualification_reply(ctx,'25') is None


def test_routine_dates_prices_and_preferences_need_no_model(strict):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient
    ctx,store=strict
    state=ctx.load_state();state.quote_id=None;state.pickup_at=state.return_at=None
    state.pickup_date=state.return_date=None;state.checkout={'mode':'new'};ctx.save_state(state)
    client=FakeClient();bot=Agent(client,AgentSettings(extraction_enabled=True))
    assert 'times' in bot.respond(ctx,'September 10th till September 15th').reply
    reply=bot.respond(ctx,'5 pm both')
    assert 'options returned' in reply.reply
    assert reply.tools_succeeded==['search_available_vehicles']
    assert 'Daily rates' in bot.respond(ctx,'what are their prices').reply
    assert 'comfort, performance' in bot.respond(ctx,'open budget give me your best car').reply
    assert not client.requests and not ctx.load_state().reservation_id


def test_routine_checkout_followups_need_no_model_or_implied_consent(strict):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from tests.fake_anthropic import FakeClient
    ctx,store=strict;details(ctx);docs(ctx,store)
    client=FakeClient();bot=Agent(client,AgentSettings(extraction_enabled=True))
    assert 'has not been confirmed' in bot.respond(ctx,'pickup option').reply
    assert 'Nothing has been booked' in bot.respond(ctx,'delivery').reply
    assert not ctx.load_state().reservation_id and not client.requests
