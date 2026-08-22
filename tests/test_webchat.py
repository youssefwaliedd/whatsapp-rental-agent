"""The local chat harness.

Development tooling, but it drives the same agent and database as everything
else, so its API is worth pinning — especially that a failing turn surfaces in
the UI rather than becoming a 500 the browser shows as a blank screen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rental_agent.agent.loop import AgentTurn
from rental_agent.webchat.app import create_app
from tests.conftest import FROZEN_NOW, REFERENCE_DATE


class StubAgent:
    """Stands in for the real agent, including its contract of recording the
    transcript — the agent owns that because it needs dedup and the evaluator
    reads it back. The chat harness only displays what the agent stored."""

    def __init__(self, turn=None, explode=False):
        self.turn = turn or AgentTurn(reply="When would you like it?", tool_calls=["get_customer"])
        self.explode = explode
        self.seen: list[str] = []

    def respond(self, ctx, message, *, provider_message_id=None):
        self.seen.append(message)
        ctx.messages.record(
            conversation_id=ctx.conversation_id, direction="inbound",
            content=message, now=ctx.now(), provider_message_id=provider_message_id,
        )
        if self.explode:
            raise RuntimeError("model exploded")
        ctx.messages.record(
            conversation_id=ctx.conversation_id, direction="outbound",
            content=self.turn.reply, now=ctx.now(),
        )
        return self.turn


@pytest.fixture
def chat(session_factory):
    agent = StubAgent()
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: agent,
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    return TestClient(app), agent


def test_the_page_loads(chat):
    client, _ = chat
    response = client.get("/")
    assert response.status_code == 200
    # Self-contained: a CDN reference would break the harness offline.
    assert "http://" not in response.text.replace("http://localhost", "")
    assert "https://" not in response.text, "no external stylesheets or fonts"


def test_the_header_names_the_configured_operator(chat):
    """The page must not hardcode a company. It used to say Sandline while the
    fleet belonged to somebody else — the same bug the system prompt had."""
    client, _ = chat
    body = client.get("/api/operator").json()

    from rental_agent.config import load_fleet

    operator, _vehicles = load_fleet()
    assert body["name"] == operator.demo_company_name
    assert body["is_demonstration"] is True


def test_a_reply_is_returned_as_the_parts_a_phone_would_show(chat):
    """The preview replays the transport's own splitting and pacing rather than
    approximating it, so the rhythm shown is the rhythm a customer gets."""
    client, agent = chat
    agent.turn = AgentTurn(reply="First line.\n\n" + ("word " * 900).strip())
    body = client.post("/api/message", json={"message": "hi"}).json()

    assert len(body["parts"]) > 1, "a long reply splits, as it would on WhatsApp"
    assert body["parts"][0]["pause"] == 0, "the first message is never delayed"
    assert all(p["pause"] > 0 for p in body["parts"][1:])


def test_a_confirmed_booking_comes_back_with_its_reaction(chat):
    client, agent = chat
    agent.turn = AgentTurn(
        reply="Booked.",
        tool_calls=["create_demo_reservation"],
        tools_succeeded=["create_demo_reservation"],
    )
    assert client.post("/api/message", json={"message": "book it"}).json()["reaction"] == "✅"


def test_a_message_gets_a_reply_and_the_tools_that_produced_it(chat):
    client, agent = chat
    body = client.post("/api/message", json={"message": "I need a car"}).json()

    assert agent.seen == ["I need a car"]
    assert body["reply"] == "When would you like it?"
    assert body["tools"] == ["get_customer"]
    assert "seconds" in body


def test_the_inspector_reports_what_is_still_missing(chat):
    client, _ = chat
    body = client.post("/api/message", json={"message": "hello"}).json()
    assert body["state"]["still_missing"] == ["pickup_at", "return_at"]
    assert body["state"]["stage"] == "new_lead"


def test_history_survives_a_reload(chat):
    client, _ = chat
    client.post("/api/message", json={"message": "first"})
    client.post("/api/message", json={"message": "second"})

    history = client.get("/api/state").json()["history"]
    assert [m["text"] for m in history if m["direction"] == "inbound"] == ["first", "second"]


def test_a_failing_turn_shows_in_the_ui_rather_than_500ing(session_factory):
    """A 500 renders as a blank screen; an error in the transcript is debuggable."""
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: StubAgent(explode=True),
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    response = TestClient(app).post("/api/message", json={"message": "hi"})
    assert response.status_code == 200
    assert "model exploded" in response.json()["error"]


def test_reset_starts_a_new_conversation_without_deleting_the_old_one(chat):
    """The transcript is what the evaluator reads — a demo you cannot review
    afterwards is worth less than one you can."""
    client, _ = chat
    client.post("/api/message", json={"message": "first conversation"})
    client.post("/api/reset")
    client.post("/api/message", json={"message": "second conversation"})

    history = client.get("/api/state").json()["history"]
    assert [m["text"] for m in history if m["direction"] == "inbound"] == ["second conversation"]


def test_bookings_appear_in_the_inspector(session_factory, booking_ctx):
    from tests.test_booking import book

    reservation = book(booking_ctx)
    booking_ctx.session.commit()

    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: StubAgent(),
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    body = TestClient(app).get(
        "/api/state", params={"handle": "+971500000001"}
    ).json()

    references = [b["reference"] for b in body["state"]["bookings"]]
    assert reservation["reservation_id"] in references


def test_an_escalation_is_visible(session_factory):
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: StubAgent(AgentTurn(reply="A colleague is taking over.", escalated=True)),
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    body = TestClient(app).post("/api/message", json={"message": "I crashed"}).json()
    assert body["escalated"] is True


def test_the_chat_window_receives_the_photos_the_agent_chose(chat):
    """The local window is where this gets tested before a WhatsApp number
    exists, so it has to carry the same media the transport would send."""
    client, agent = chat
    agent.turn = AgentTurn(
        reply="Here's the G63.",
        media=[{
            "vehicle_id": "veh_13",
            "display_name": "Mercedes-Benz G63 — 2025",
            "images": ["assets/vehicles/veh_13.png", "assets/vehicles/veh_13_spec.png"],
            "caption": None,
        }],
    )
    body = client.post("/api/message", json={"message": "can i see it"}).json()

    assert [i["vehicle"] for i in body["media"]] == ["Mercedes-Benz G63 — 2025"]
    assert body["media"][0]["images"] == [
        "/assets/vehicles/veh_13.png",
        "/assets/vehicles/veh_13_spec.png",
    ]


def test_photo_urls_are_servable_by_the_mounted_assets_route(chat):
    """A path the browser cannot fetch renders as a broken image, which is a
    worse demo than no photo at all."""
    client, agent = chat
    agent.turn = AgentTurn(
        reply="Here it is.",
        media=[{"vehicle_id": "veh_13", "display_name": "G63",
                "images": ["assets/vehicles/veh_13.png"], "caption": None}],
    )
    url = client.post("/api/message", json={"message": "show me"}).json()["media"][0]["images"][0]

    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
