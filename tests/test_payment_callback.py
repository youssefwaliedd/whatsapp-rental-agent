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


def paid_event(reservation_id: str, *, amount_minor: int = 272790, event_id: str = "evt_1") -> bytes:
    return json.dumps({
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_123",
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
