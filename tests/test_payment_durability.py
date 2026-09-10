from decimal import Decimal
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from rental_agent.context import ToolContext
from rental_agent.payments.stripe import StripeProvider
from rental_agent.payments.webhook import build_router
from rental_agent.whatsapp.jobs import enqueue
from rental_agent.store.models import WorkItem
from tests.test_payment_callback import booked, paid_event, signed, SECRET, status_of
from tests.conftest import FROZEN_NOW, REFERENCE_DATE


def test_stripe_retries_use_same_idempotency_key(monkeypatch):
    calls=[]
    def post(url,**kw):
        calls.append(kw)
        return httpx.Response(200,json={'id':'cs_test_synthetic','url':'https://checkout.stripe.com/test'})
    monkeypatch.setattr(httpx,'post',post)
    provider=StripeProvider('sk_test_synthetic')
    args=dict(amount=Decimal('2935.80'),currency='AED',reference='PAY-42',description='Synthetic rental')
    provider.create_link(**args);provider.create_link(**args)
    assert calls[0]['headers']['Idempotency-Key']==calls[1]['headers']['Idempotency-Key']
    assert calls[0]['data']['line_items[0][price_data][unit_amount]']=='293580'
    provider.create_link(**{**args,'reference':'PAY-43'})
    assert calls[2]['headers']['Idempotency-Key']!=calls[0]['headers']['Idempotency-Key']


def app_for(factory,persist):
    app=FastAPI();app.include_router(build_router(session_factory=factory,
        context_factory=lambda db:ToolContext(session=db,now_fn=lambda:FROZEN_NOW,reference_date=REFERENCE_DATE),
        secret=SECRET,persist_notification=persist))
    return TestClient(app,raise_server_exceptions=False)


def test_payment_and_notification_are_atomic(session_factory,booked):
    rid=booked[1]['reservation_id'];body=paid_event(rid)
    def fail(db,*args):raise RuntimeError('synthetic database failure')
    api=app_for(session_factory,fail)
    assert api.post('/payments/stripe',content=body,headers=signed(body)).status_code==500
    assert status_of(session_factory,rid)!='paid'
    def persist(db,result,reservation_id,event_id):
        enqueue(db,key=event_id,lane=reservation_id,kind='payment_notice',payload={'result':result})
    api=app_for(session_factory,persist)
    assert api.post('/payments/stripe',content=body,headers=signed(body)).status_code==200
    assert status_of(session_factory,rid)=='paid'
    api.post('/payments/stripe',content=body,headers=signed(body))
    with session_factory() as db:assert len(list(db.scalars(select(WorkItem))))==1
