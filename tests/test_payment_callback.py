"""Learning that the customer actually paid.

Without this the system asks for money and never finds out it arrived: Stripe
said `paid`, the database said `link_sent`, and a customer asking "is that
confirmed?" would have been told their payment was outstanding — worse than
never asking, because now it looks like the money is lost.

Two things it refuses to do, and both are tested here rather than trusted:
it will not act on an event it cannot authenticate, and it will not record the
same payment twice when Stripe retries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rental_agent.context import ToolContext
from rental_agent.payments.webhook import build_router, verify
from rental_agent.tools.registry import execute_tool

from .conftest import FROZEN_NOW, REFERENCE_DATE, dt

SECRET = "whsec_test_secret"


def signed(body: bytes, secret: str = SECRET, *, at: float | None = None) -> dict[str, str]:
    stamp = str(int(at or time.time()))
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={stamp},v1={digest}"}


def paid_event(reservation_id: str, *, amount_minor: int = 25200, event_id: str = "evt_1") -> bytes:
    return json.dumps({
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "PAY-1",
            "payment_intent": "pi_test_1",
            "livemode": False,
            "payment_status": "paid",
            "amount_total": amount_minor,
            "currency": "aed",
            "metadata": {"reservation_id": reservation_id},
        }},
    }).encode()


@pytest.fixture
def booked(booking_ctx):
    vehicle = booking_ctx.engine.list_fleet()[0]
    quote = execute_tool(booking_ctx, "create_demo_quote", {
        "vehicle_id": vehicle.id, "pickup_at": dt(10, 17).isoformat(),
        "return_at": dt(12, 17).isoformat(),
    })
    reservation = execute_tool(
        booking_ctx, "create_demo_reservation", {"quote_id": quote["quote_id"]}
    )
    execute_tool(booking_ctx, "create_payment_link", {
        "reservation_id": reservation["reservation_id"]
    })
    booking_ctx.session.commit()
    return quote, reservation


@pytest.fixture
def client(session_factory, booked):
    told: list[tuple] = []
    app = FastAPI()
    app.include_router(build_router(
        session_factory=session_factory,
        context_factory=lambda s: ToolContext(
            session=s, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE
        ),
        on_paid=lambda result, reservation_id: told.append((result, reservation_id)),
        secret=SECRET,
    ))
    return TestClient(app), told


def status_of(session_factory, reservation_id: str) -> str:
    with session_factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda: FROZEN_NOW,
                          reference_date=REFERENCE_DATE)
        return ctx.reservations.get(reservation_id).payment_status


# --- the signature ----------------------------------------------------------


def test_an_unsigned_event_is_refused(client, booked):
    api, _ = client
    _, reservation = booked
    body = paid_event(reservation["reservation_id"])
    assert api.post("/payments/stripe", content=body).status_code == 400


def test_a_tampered_event_is_refused(client, booked, session_factory):
    api, _ = client
    _, reservation = booked
    body = paid_event(reservation["reservation_id"])
    headers = signed(body)
    # Same signature, different body — the amount doubled in transit.
    tampered = paid_event(reservation["reservation_id"], amount_minor=545580)
    assert api.post("/payments/stripe", content=tampered, headers=headers).status_code == 400
    assert status_of(session_factory, reservation["reservation_id"]) != "paid"


def test_a_replayed_event_is_refused(client, booked):
    api, _ = client
    _, reservation = booked
    body = paid_event(reservation["reservation_id"])
    stale = signed(body, at=time.time() - 4000)
    assert api.post("/payments/stripe", content=body, headers=stale).status_code == 400


@pytest.mark.parametrize("header", [None, "", "t=123", "v1=abc", "nonsense"])
def test_malformed_signature_headers_are_refused(header):
    assert verify(b"{}", header, SECRET) is False


# --- recording the money ----------------------------------------------------


def test_a_signed_payment_is_recorded(client, booked, session_factory):
    api, _ = client
    _, reservation = booked
    body = paid_event(reservation["reservation_id"])
    response = api.post("/payments/stripe", content=body, headers=signed(body))

    assert response.status_code == 200
    assert response.json()["recorded"] is True
    assert status_of(session_factory, reservation["reservation_id"]) == "paid"


def test_the_amount_recorded_is_the_one_that_arrived(client, booked):
    api, _ = client
    _, reservation = booked
    # A customer paying an old link, or a conversion, makes these differ. The
    # provider's record is the one that is true.
    body = paid_event(reservation["reservation_id"], amount_minor=100000)
    result = api.post("/payments/stripe", content=body, headers=signed(body)).json()

    assert Decimal(result["amount"]) == Decimal("1000.00")
    assert result["payment_status"] == "payment_review"
    assert result["amount_differs"] is True
    assert Decimal(result["expected"]) == Decimal(reservation["total_charge"])


def test_a_retried_event_does_not_pay_twice(client, booked, session_factory):
    api, told = client
    _, reservation = booked
    body = paid_event(reservation["reservation_id"])
    headers = signed(body)

    first = api.post("/payments/stripe", content=body, headers=headers).json()
    second = api.post("/payments/stripe", content=body, headers=headers).json()

    assert first["recorded"] is True
    assert second["recorded"] is False
    assert second["reason"] == "already_recorded"
    # And the customer is told once, not twice.
    assert len(told) == 1


def test_the_customer_is_told(client, booked):
    api, told = client
    _, reservation = booked
    body = paid_event(reservation["reservation_id"])
    api.post("/payments/stripe", content=body, headers=signed(body))

    assert told and told[0][1] == reservation["reservation_id"]
    assert told[0][0]["currency"] == "AED"


# --- events that are not a payment ------------------------------------------


def test_an_unrelated_event_is_ignored(client):
    api, _ = client
    body = json.dumps({"id": "evt_2", "type": "customer.created", "data": {"object": {}}}).encode()
    assert api.post("/payments/stripe", content=body, headers=signed(body)).json()["ignored"]


def test_an_unpaid_session_is_ignored(client, booked, session_factory):
    api, _ = client
    _, reservation = booked
    body = json.dumps({
        "id": "evt_3", "type": "checkout.session.completed",
        "data": {"object": {"payment_status": "unpaid",
                            "metadata": {"reservation_id": reservation["reservation_id"]}}},
    }).encode()
    api.post("/payments/stripe", content=body, headers=signed(body))
    assert status_of(session_factory, reservation["reservation_id"]) != "paid"


def test_a_payment_for_an_unknown_booking_is_not_invented(client):
    api, _ = client
    body = paid_event("DEMO-9999", event_id="evt_4")
    result = api.post("/payments/stripe", content=body, headers=signed(body)).json()
    assert result["recorded"] is False
    assert result["reason"] == "unknown_reservation"


def post_event(api, body, **updates):
    data = json.loads(body)
    data['data']['object'].update(updates)
    raw = json.dumps(data).encode()
    return api.post('/payments/stripe', content=raw, headers=signed(raw)).json()


@pytest.mark.parametrize('updates,reason', [
    ({'currency': 'usd'}, 'currency_mismatch'),
    ({'amount_total': 1}, 'amount_mismatch'),
    ({'livemode': True}, 'payment_mode_mismatch'),
])
def test_mismatched_receipts_are_recorded_for_review(client, booked, session_factory, updates, reason):
    api, _ = client
    _, reservation = booked
    result = post_event(api, paid_event(reservation['reservation_id']), **updates)
    assert result['recorded']
    assert reason in result['issues']
    assert status_of(session_factory, reservation['reservation_id']) == 'payment_review'
    assert 'do not pay again' in result['message'].lower()


@pytest.mark.parametrize('amount', [None, True, '25200', -10, 0])
def test_missing_or_invalid_amount_never_defaults_to_the_quote(client, booked, session_factory, amount):
    api, _ = client
    _, reservation = booked
    result = post_event(api, paid_event(reservation['reservation_id']), amount_total=amount)
    assert not result['recorded']
    assert status_of(session_factory, reservation['reservation_id']) != 'paid'


def test_unknown_checkout_cannot_pay_a_known_booking(client, booked, session_factory):
    api, _ = client
    _, r = booked
    result = post_event(api, paid_event(r['reservation_id']), id='cs_someone_else')
    assert result['reason'] == 'unknown_checkout'
    assert status_of(session_factory, r['reservation_id']) == 'link_sent'


def test_cancelled_booking_payment_is_reviewed(client, booked, booking_ctx, session_factory):
    api, _ = client
    _, r = booked
    execute_tool(booking_ctx, 'cancel_demo_reservation', {'reservation_id':r['reservation_id'], 'reason':'test'})
    booking_ctx.session.commit()
    result = post_event(api, paid_event(r['reservation_id']))
    assert 'booking_not_confirmed' in result['issues']
    assert status_of(session_factory, r['reservation_id']) == 'payment_review'


def test_superseded_link_payment_cannot_silently_settle_latest_checkout(client, booked, booking_ctx):
    api, _ = client
    _, r = booked
    execute_tool(booking_ctx, 'create_payment_link', {'reservation_id': r['reservation_id']})
    booking_ctx.session.commit()
    result = post_event(api, paid_event(r['reservation_id']))
    assert 'superseded_checkout' in result['issues']


@pytest.mark.parametrize('kind,status', [('checkout.session.expired','expired'), ('checkout.session.async_payment_failed','failed')])
def test_terminal_checkout_status_and_late_events(client, booked, session_factory, kind, status):
    api, _ = client
    _, r = booked
    event = json.loads(paid_event(r['reservation_id']))
    event['type'] = kind
    raw=json.dumps(event).encode()
    result = api.post('/payments/stripe',content=raw,headers=signed(raw)).json()
    assert result['payment_status'] == status
    post_event(api, paid_event(r['reservation_id']))
    assert status_of(session_factory,r['reservation_id']) == 'paid'
    api.post('/payments/stripe',content=raw,headers=signed(raw))
    assert status_of(session_factory,r['reservation_id']) == 'paid'


def test_partial_then_full_refund_and_out_of_order_event(client, booked, session_factory):
    api, _ = client
    _, r = booked
    post_event(api, paid_event(r['reservation_id']))
    def refund(amount):
        raw=json.dumps({'id':f'evt_refund_{amount}','type':'charge.refunded','data':{'object':{
            'payment_intent':'pi_test_1','amount_refunded':amount,'currency':'aed'}}}).encode()
        return api.post('/payments/stripe',content=raw,headers=signed(raw)).json()
    assert refund(10000)['payment_status']=='partially_refunded'
    assert refund(25200)['payment_status']=='refunded'
    assert refund(10000)['recorded'] is False
    assert status_of(session_factory,r['reservation_id'])=='refunded'


def test_paid_cancellation_opens_refund_review(client, booked, session_factory):
    api,_=client
    _,r=booked
    post_event(api,paid_event(r['reservation_id']))
    with session_factory() as session:
        ctx=ToolContext(session=session,now_fn=lambda:FROZEN_NOW,reference_date=REFERENCE_DATE,
                        customer_id=r['customer_id'])
        row=ctx.reservations.get(r['reservation_id']);ctx.conversation_id=row.conversation_id
        result=execute_tool(ctx,'cancel_demo_reservation',{'reservation_id':r['reservation_id'],'reason':'changed plans'})
        assert result['payment_status']=='refund_pending'
        assert 'No refund has been issued' in result['payment_guidance']
        session.commit()


def test_signature_rotation_accepts_any_valid_v1_signature():
    body=b'{}';header=signed(body)['stripe-signature']
    assert verify(body,header+',v1=bad',SECRET)
    assert verify(body,'v1=bad,'+header,SECRET)


def test_malformed_signed_body_is_a_client_error(client):
    api,_=client
    for body in [b'not json',b'[]',b'{"data":null}']:
        assert api.post('/payments/stripe',content=body,headers=signed(body)).status_code==400


def test_checkout_return_verifies_processor_and_webhook_is_idempotent(client, booked, session_factory, monkeypatch):
    from rental_agent.webchat.app import create_app
    api,_=client
    _,r=booked
    checkout=json.loads(paid_event(r['reservation_id']))['data']['object']
    reads=[]
    def retrieve(self, reference):
        reads.append(reference)
        return checkout
    monkeypatch.setattr('rental_agent.payments.stripe.StripeProvider.retrieve_checkout', retrieve)
    monkeypatch.setenv('STRIPE_API_KEY','sk_test_fake')
    app=create_app(session_factory=session_factory, agent_factory=lambda:None,
                   now_fn=lambda:FROZEN_NOW,reference_date=REFERENCE_DATE)
    browser=TestClient(app)
    query={'session_id':'PAY-1','handle':'+971500000001'}
    assert 'Payment received' in browser.get('/payments/return',params=query).text
    assert reads==['PAY-1']
    result=post_event(api,paid_event(r['reservation_id']))
    assert result['reason']=='already_recorded'
    history=browser.get('/api/state',params={'handle':'+971500000001'}).json()['history']
    assert sum('Payment received:' in m['text'] for m in history)==1


def test_return_does_not_read_another_customers_checkout(booked, session_factory, monkeypatch):
    from rental_agent.webchat.app import create_app
    def unexpected(*args):
        raise AssertionError('Must not query an unowned checkout')
    monkeypatch.setattr('rental_agent.payments.stripe.StripeProvider.retrieve_checkout',unexpected)
    app=create_app(session_factory=session_factory,agent_factory=lambda:None,
        now_fn=lambda:FROZEN_NOW,reference_date=REFERENCE_DATE)
    response=TestClient(app).get('/payments/return',params={'session_id':'PAY-1','handle':'+971500000999'})
    assert 'No payment has been verified' in response.text


def test_zero_decimal_currency_receipt(booking_ctx, booked):
    from rental_agent.services.payments import mark_paid
    _,r=booked
    row=booking_ctx.reservations.get(r['reservation_id'])
    row.currency='JPY';row.total_charge=Decimal('252')
    row.history=[{**h, 'currency':'JPY'} if h.get('event')=='payment_link_created' else h for h in row.history]
    result=mark_paid(booking_ctx,reservation_id=row.reservation_id,amount_minor=252,currency='JPY',reference='PAY-1')
    assert result['payment_status']=='paid'
    assert Decimal(result['amount'])==Decimal('252')


def test_holding_payment_does_not_settle_rental(booking_ctx, booked):
    from rental_agent.services.payments import mark_paid
    _,r=booked
    row=booking_ctx.reservations.get(r['reservation_id'])
    row.history=[{**h,'purpose':'holding','amount':'100.00'} if h.get('event')=='payment_link_created' else h for h in row.history]
    result=mark_paid(booking_ctx,reservation_id=row.reservation_id,amount_minor=10000,currency='AED',reference='PAY-1')
    assert result['payment_status']=='holding_paid'
    assert 'does not mean' in result['message']
