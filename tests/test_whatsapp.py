"""The WhatsApp transport.

Verified against realistic Cloud API envelopes with no phone number, no token
and no network — the parsing is pure, and everything the webhook depends on is
injected.

Two properties matter more than the rest and are tested hardest: an unsigned
request must never be processed, and the endpoint must acknowledge before it
starts a turn, because Meta redelivers anything it does not get a 200 from
quickly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from rental_agent.agent.loop import AgentTurn
from rental_agent.store.models import Evaluation
from rental_agent.tools.registry import execute_tool
from rental_agent.whatsapp import payloads
from rental_agent.whatsapp.client import SendResult, WhatsAppClient, split_message
from rental_agent.whatsapp.settings import WhatsAppSettings
from rental_agent.whatsapp.webhook import UNSUPPORTED_REPLY, create_app
from tests.conftest import FROZEN_NOW, REFERENCE_DATE

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "sandline-demo"


# --------------------------------------------------------------------------
# Realistic payloads
# --------------------------------------------------------------------------


def text_payload(body="I need a black G63", message_id="wamid.TEST1", sender="971500000001"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "PHONE_ID",
                            },
                            "contacts": [
                                {"profile": {"name": "Youssef"}, "wa_id": sender}
                            ],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1756000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_payload():
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": "wamid.SENT1",
                                    "status": "delivered",
                                    "timestamp": "1756000001",
                                    "recipient_id": "971500000001",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def image_payload():
    payload = text_payload()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "image"
    message["image"] = {"id": "MEDIA_ID", "mime_type": "image/jpeg"}
    return payload


def voice_payload():
    payload = text_payload()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "audio"
    message["audio"] = {"id": "MEDIA_ID", "voice": True}
    return payload


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_a_text_message_is_flattened_out_of_the_envelope():
    [message] = payloads.parse_messages(text_payload())
    assert message.text == "I need a black G63"
    assert message.from_number == "971500000001"
    assert message.message_id == "wamid.TEST1"
    assert message.contact_name == "Youssef"
    assert message.unsupported is False


def test_delivery_receipts_are_not_mistaken_for_conversation():
    """Statuses arrive down the same pipe. Treating one as a customer turn
    would have the agent replying to its own delivery receipt."""
    assert payloads.parse_messages(status_payload()) == []
    assert len(payloads.parse_statuses(status_payload())) == 1
    assert payloads.is_status_only(status_payload()) is True


def test_a_button_reply_reads_as_its_label():
    payload = text_payload()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "interactive"
    message["interactive"] = {
        "type": "button_reply",
        "button_reply": {"id": "reserve", "title": "Reserve this car"},
    }
    [parsed] = payloads.parse_messages(payload)
    assert parsed.text == "Reserve this car"
    assert parsed.unsupported is False


def test_an_image_is_treated_as_a_document_that_arrived():
    """We cannot read it, but the arrival is itself a fact — it is what
    separates a real document check from the agent taking "here you go" at its
    word."""
    [message] = payloads.parse_messages(image_payload())
    assert message.unsupported is False
    assert message.media_kind == "image"
    assert "[sent a photo]" in message.text


def test_a_voice_note_is_still_unreadable():
    """A photo of a licence is evidence. A voice note is not something this
    agent can act on at all."""
    [message] = payloads.parse_messages(voice_payload())
    assert message.unsupported is True
    assert message.media_kind is None


def test_several_messages_in_one_payload_are_all_returned():
    payload = text_payload()
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append(
        {"from": "971500000002", "id": "wamid.TEST2", "type": "text", "text": {"body": "hello"}}
    )
    assert len(payloads.parse_messages(payload)) == 2


def test_an_empty_payload_is_survivable():
    assert payloads.parse_messages({}) == []
    assert payloads.parse_statuses({}) == []


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------


def test_a_correct_signature_verifies():
    body = b'{"hello":"world"}'
    assert payloads.verify_signature(body, sign(body), APP_SECRET) is True


def test_a_tampered_body_fails():
    body = b'{"hello":"world"}'
    assert payloads.verify_signature(b'{"hello":"evil"}', sign(body), APP_SECRET) is False


@pytest.mark.parametrize("header", [None, "", "garbage", "sha1=abc", "sha256=wrong"])
def test_a_missing_or_malformed_signature_fails(header):
    assert payloads.verify_signature(b"{}", header, APP_SECRET) is False


def test_no_app_secret_means_nothing_verifies():
    body = b"{}"
    assert payloads.verify_signature(body, sign(body), "") is False


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def test_a_short_reply_is_one_message():
    assert split_message("Hello there") == ["Hello there"]


def test_a_long_reply_splits_on_paragraphs_and_never_truncates():
    """A quote cut mid-number is worse than two messages."""
    text = "\n\n".join(f"Paragraph {i} " + "x" * 400 for i in range(15))
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert sum(len(p) for p in parts) >= len(text) - 4 * len(parts)


def test_sending_without_credentials_reports_rather_than_raises():
    """A failed send must not take the turn down with it."""
    result = WhatsAppClient(WhatsAppSettings(phone_number_id="", access_token="")).send_text(
        "971500000001", "hi"
    )
    assert result.ok is False
    assert "not configured" in result.error


def test_a_text_send_builds_the_documented_payload():
    sent = []
    client = WhatsAppClient(
        WhatsAppSettings(phone_number_id="PID", access_token="TOK"),
        transport=lambda payload: sent.append(payload) or {"messages": [{"id": "wamid.OUT"}]},
    )
    result = client.send_text("971500000001", "Your booking is confirmed")

    assert result.ok and result.message_ids == ["wamid.OUT"]
    assert sent[0]["messaging_product"] == "whatsapp"
    assert sent[0]["type"] == "text"
    assert sent[0]["text"]["body"] == "Your booking is confirmed"
    # A vehicle card should render as our image, not a scraped link preview.
    assert sent[0]["text"]["preview_url"] is False


def test_an_image_send_uses_a_public_link():
    sent = []
    client = WhatsAppClient(
        WhatsAppSettings(phone_number_id="PID", access_token="TOK"),
        transport=lambda p: sent.append(p) or {"messages": [{"id": "x"}]},
    )
    client.send_image("971500000001", "https://example.com/veh_13.png", caption="G63")
    assert sent[0]["type"] == "image"
    assert sent[0]["image"]["link"].endswith("veh_13.png")
    assert sent[0]["image"]["caption"] == "G63"


def test_a_transport_failure_is_returned_not_raised():
    def explode(_payload):
        raise RuntimeError("network down")

    client = WhatsAppClient(
        WhatsAppSettings(phone_number_id="PID", access_token="TOK"), transport=explode
    )
    result = client.send_text("971500000001", "hi")
    assert result.ok is False and "network down" in result.error


def test_a_card_url_needs_a_public_base():
    assert WhatsAppClient(WhatsAppSettings()).card_url("assets/vehicles/veh_13.png") is None
    client = WhatsAppClient(WhatsAppSettings(media_base_url="https://example.com/"))
    assert client.card_url("assets/vehicles/veh_13.png") == (
        "https://example.com/assets/vehicles/veh_13.png"
    )


# --------------------------------------------------------------------------
# The webhook
# --------------------------------------------------------------------------


class RecordingClient(WhatsAppClient):
    """Records every outbound call instead of making it.

    Subclasses the real client rather than faking it, so a signature change in
    the transport shows up here as a failure rather than as a test that quietly
    keeps passing against a shape that no longer exists.
    """

    def __init__(self, media_base_url=""):
        super().__init__(
            WhatsAppSettings(
                phone_number_id="PID",
                access_token="TOK",
                staff_number="971500009999",
                media_base_url=media_base_url,
            )
        )
        self.texts: list[tuple[str, str]] = []
        self.read: list[str] = []
        self.typing: list[str] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.images: list[tuple[str, str, str | None]] = []
        self.buttons: list[tuple[str, str, list[dict]]] = []
        self.templates: list[tuple[str, str, list[str] | None]] = []
        #: Incremented so each send returns a distinct id, the way Meta does —
        #: reply-threading depends on those ids being distinguishable.
        self._sent = 0

    def send_text(self, to, text, typing_for=None):
        self.texts.append((to, text))
        return SendResult(ok=True, message_ids=["wamid.OUT"])

    def send_image(self, to, image_url, caption=None):
        self.images.append((to, image_url, caption))
        return SendResult(ok=True, message_ids=["wamid.IMG"])

    def send_buttons(self, to, body, buttons):
        self._sent += 1
        self.buttons.append((to, body, buttons))
        return SendResult(ok=True, message_ids=[f"wamid.ASK{self._sent}"])

    def send_reaction(self, to, message_id, emoji):
        self.reactions.append((to, message_id, emoji))
        return SendResult(ok=True)

    def send_template(self, to, name, *, language="en", body_params=None):
        self.templates.append((to, name, body_params))
        return SendResult(ok=True, message_ids=["wamid.TPL"])

    def mark_read(self, message_id):
        self.read.append(message_id)
        return SendResult(ok=True)

    def send_typing(self, message_id):
        self.typing.append(message_id)
        self.read.append(message_id)  # a typing indicator marks read too
        return SendResult(ok=True)


class StubAgent:
    """Stands in for the model, not for the machinery around it.

    A turn that reports `escalated` also writes the escalation record, because
    the real agent does — the tool call is what creates it. A double that
    claimed escalation without the row would let the case machinery pass tests
    against a state that cannot occur.
    """

    def __init__(self, turn: AgentTurn):
        self.turn = turn
        self.calls: list[tuple[str, str | None]] = []
        self.relays: list[str] = []
        self.recorded_media: list[list[str] | None] = []

    def respond(self, ctx, message, *, provider_message_id=None, media=None):
        self.calls.append((message, provider_message_id))
        self.recorded_media.append(media)
        # The real agent records the inbound message. That record is what the
        # 24-hour service window is measured from, so a double that skipped it
        # would make every conversation look like one nobody may write to.
        ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="inbound",
            content=message,
            now=ctx.now(),
            provider_message_id=provider_message_id,
            media=media,
        )
        if self.turn.escalated and not self.turn.duplicate:
            execute_tool(
                ctx, "escalate_conversation", {"reason": "other", "detail": message}
            )
        return self.turn

    def relay(self, ctx, directive):
        self.relays.append(directive)
        return AgentTurn(reply=f"[relayed] {directive.splitlines()[0]}")


@pytest.fixture
def harness(session_factory):
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply="Happy to help — when do you need it?"))
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: agent,
        settings=WhatsAppSettings(
            phone_number_id="PID", access_token="TOK",
            app_secret=APP_SECRET, verify_token=VERIFY_TOKEN,
            staff_number="971500009999",
        ),
        client=outbound,
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    return TestClient(app), outbound, agent


def post(client, payload, *, secret=APP_SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"},
    )


def test_the_verification_handshake_echoes_the_challenge(harness):
    client, _, _ = harness
    response = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"})
    assert response.status_code == 200
    assert response.text == "12345"


def test_a_wrong_verify_token_is_refused(harness):
    client, _, _ = harness
    response = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"})
    assert response.status_code == 403


def test_a_signed_message_is_answered(harness):
    client, outbound, agent = harness
    response = post(client, text_payload())

    assert response.status_code == 200
    assert agent.calls == [("I need a black G63", "wamid.TEST1")]
    assert outbound.texts == [("971500000001", "Happy to help — when do you need it?")]
    assert outbound.read == ["wamid.TEST1"]


def test_an_unsigned_request_is_never_processed(harness):
    """The URL is discoverable; only Meta can sign for it."""
    client, outbound, agent = harness
    body = json.dumps(text_payload()).encode()
    response = client.post("/webhook", content=body,
                           headers={"X-Hub-Signature-256": "sha256=forged"})

    assert response.status_code == 403
    assert agent.calls == []
    assert outbound.texts == []


def test_a_status_update_is_acknowledged_and_ignored(harness):
    client, outbound, agent = harness
    assert post(client, status_payload()).status_code == 200
    assert agent.calls == []
    assert outbound.texts == []


def test_a_voice_note_gets_an_explanation_not_silence(harness):
    client, outbound, agent = harness
    post(client, voice_payload())
    assert agent.calls == []
    assert outbound.texts == [("971500000001", UNSUPPORTED_REPLY)]


def test_a_photo_with_an_invalid_media_id_requests_a_replacement(harness):
    """A webhook marker alone must not become a successful document receipt."""
    client, outbound, agent = harness
    post(client, image_payload())

    assert agent.calls == []
    assert "could not save" in outbound.texts[-1][1]


def test_a_redelivery_is_not_answered_twice(session_factory):
    """Meta redelivers on any hiccup. The dedup already in the agent has to be
    honoured here or the customer gets two replies."""
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply="", duplicate=True))
    app = create_app(
        session_factory=session_factory, agent_factory=lambda: agent,
        settings=WhatsAppSettings(phone_number_id="PID", access_token="TOK",
                                  app_secret=APP_SECRET, verify_token=VERIFY_TOKEN),
        client=outbound, reference_date=REFERENCE_DATE, now_fn=lambda: FROZEN_NOW,
    )
    post(TestClient(app), text_payload())
    assert outbound.texts == []


def test_an_escalation_notifies_staff(session_factory):
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply="A colleague is taking over.", escalated=True))
    app = create_app(
        session_factory=session_factory, agent_factory=lambda: agent,
        settings=WhatsAppSettings(phone_number_id="PID", access_token="TOK",
                                  app_secret=APP_SECRET, verify_token=VERIFY_TOKEN,
                                  staff_number="971500009999"),
        client=outbound, reference_date=REFERENCE_DATE, now_fn=lambda: FROZEN_NOW,
    )
    post(TestClient(app), text_payload("I crashed the car"))

    assert "971500000001" in [to for to, _ in outbound.texts]   # the customer
    assert [to for to, _, _ in outbound.buttons] == ["971500009999"]  # the colleague

    _, note, buttons = outbound.buttons[0]
    assert "I crashed the car" in note

    # Their section 3 asks for full context, and an interactive body is capped
    # at about a thousand characters. So the briefing is its own message, and it
    # arrives before the question rather than after it.
    to_staff = [text for to, text in outbound.texts if to == "971500009999"]
    assert to_staff, "the owner was asked to decide with no briefing at all"
    briefing = to_staff[0]
    assert "*Customer*" in briefing
    assert "971500000001" in briefing            # how to reach them
    assert "them: I crashed the car" in briefing  # how it got here
    # The stub escalates as "other", which is not a decision reason — so this is
    # a handover, and the owner is told to take over rather than asked to pick.
    assert "This needs a person" in note
    assert [b["title"] for b in buttons] == ["I've taken it from here"]


def test_a_crashing_turn_still_returns_200(session_factory):
    """A 500 makes Meta redeliver, which would replay the same crash forever."""
    class Exploding:
        def respond(self, *a, **k):
            raise RuntimeError("boom")

    app = create_app(
        session_factory=session_factory, agent_factory=Exploding,
        settings=WhatsAppSettings(phone_number_id="PID", access_token="TOK",
                                  app_secret=APP_SECRET, verify_token=VERIFY_TOKEN),
        client=RecordingClient(), reference_date=REFERENCE_DATE, now_fn=lambda: FROZEN_NOW,
    )
    assert post(TestClient(app), text_payload()).status_code == 200


def test_a_new_whatsapp_number_becomes_a_customer_with_their_name(session_factory):
    """The profile name is the only thing WhatsApp gives us for free — worth
    keeping, so a returning customer can be greeted properly."""
    from rental_agent.context import ToolContext

    outbound = RecordingClient()
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: StubAgent(AgentTurn(reply="Hello")),
        settings=WhatsAppSettings(phone_number_id="PID", access_token="TOK",
                                  app_secret=APP_SECRET, verify_token=VERIFY_TOKEN),
        client=outbound, reference_date=REFERENCE_DATE, now_fn=lambda: FROZEN_NOW,
    )
    post(TestClient(app), text_payload())

    with session_factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda: FROZEN_NOW,
                          reference_date=REFERENCE_DATE)
        customer = ctx.customers.by_whatsapp_id("971500000001")
        assert customer is not None
        assert customer.name == "Youssef"
        assert ctx.conversations.open_for_customer(customer.customer_id) is not None


def test_health_reports_what_is_still_missing(session_factory):
    app = create_app(session_factory=session_factory, agent_factory=lambda: None,
                     settings=WhatsAppSettings())
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["whatsapp_configured"] is False
    assert "WHATSAPP_ACCESS_TOKEN" in body["missing_settings"]


# --------------------------------------------------------------------------
# Markup — WhatsApp is not markdown
# --------------------------------------------------------------------------


def test_markdown_bold_is_converted_before_sending():
    """A model reaching for **bold** out of habit would put literal asterisks
    around the customer's total, at exactly the moment they are deciding whether
    to trust the figure."""
    from rental_agent.whatsapp.client import to_whatsapp_markup

    assert to_whatsapp_markup("The total is **AED 7,560**") == "The total is *AED 7,560*"


def test_whatsapp_bold_is_left_alone():
    from rental_agent.whatsapp.client import to_whatsapp_markup

    assert to_whatsapp_markup("The total is *AED 7,560*") == "The total is *AED 7,560*"


def test_headings_and_bullets_become_whatsapp_friendly():
    from rental_agent.whatsapp.client import to_whatsapp_markup

    result = to_whatsapp_markup("## Options\n- Kia Pegas\n- Nissan Sunny")
    assert "#" not in result
    assert result.count("•") == 2


def test_a_lone_asterisk_is_not_mangled():
    from rental_agent.whatsapp.client import to_whatsapp_markup

    assert to_whatsapp_markup("2 * 3 = 6") == "2 * 3 = 6"


def test_the_normaliser_runs_on_the_send_path():
    sent = []
    client = WhatsAppClient(
        WhatsAppSettings(phone_number_id="PID", access_token="TOK"),
        transport=lambda p: sent.append(p) or {"messages": [{"id": "x"}]},
    )
    client.send_text("971500000001", "Your total is **AED 7,560**")
    assert sent[0]["text"]["body"] == "Your total is *AED 7,560*"


# --------------------------------------------------------------------------
# WhatsApp-native behaviour — typing, reactions, photo sequences, pacing
#
# What these test is not "does the API accept it" but "does the customer get
# something a person would plausibly have sent". The failures worth catching
# here are social rather than technical: a thumbs-up on an accident report, a
# caption repeated under three photos, a booking confirmation for a booking that
# did not happen.
# --------------------------------------------------------------------------


def webhook(session_factory, client_obj, agent_obj, **settings_kwargs):
    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: agent_obj,
        settings=WhatsAppSettings(
            phone_number_id="PID", access_token="TOK",
            app_secret=APP_SECRET, verify_token=VERIFY_TOKEN,
            staff_number="971500009999", **settings_kwargs,
        ),
        client=client_obj,
        reference_date=REFERENCE_DATE,
        now_fn=lambda: FROZEN_NOW,
    )
    return TestClient(app)


def test_the_customer_sees_typing_while_the_agent_thinks(harness):
    """A turn takes seconds. Blue ticks alone read as being left on read."""
    client, outbound, _ = harness
    post(client, text_payload())
    assert outbound.typing == ["wamid.TEST1"]


def test_a_confirmed_booking_is_marked_on_the_customers_message(session_factory):
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(
        reply="Booked — DEMO-1042.",
        tool_calls=["create_demo_reservation"],
        tools_succeeded=["create_demo_reservation"],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("book it"))

    assert outbound.reactions == [("971500000001", "wamid.TEST1", "✅")]


def test_an_accident_report_is_never_reacted_to(session_factory):
    """The single most damaging message this system could send is a thumbs-up on
    "I've just had an accident". Silence on escalation is a rule, not an
    accident of the mapping."""
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(
        reply="Are you safe? A colleague is calling you now.",
        escalated=True,
        tool_calls=["escalate_conversation", "get_active_reservation"],
        tools_succeeded=["escalate_conversation", "get_active_reservation"],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("i crashed the car"))

    assert outbound.reactions == []


def test_escalation_silences_a_reaction_the_turn_would_otherwise_have_earned(session_factory):
    """A turn can cancel a booking and then escalate. Whatever else happened,
    the customer is in trouble and the right number of emoji is zero."""
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(
        reply="A colleague is taking this over.",
        escalated=True,
        tool_calls=["cancel_demo_reservation", "escalate_conversation"],
        tools_succeeded=["cancel_demo_reservation", "escalate_conversation"],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("this is unacceptable"))

    assert outbound.reactions == []


def test_a_failed_booking_earns_no_confirmation(session_factory):
    """`tool_calls` records what was attempted. Reacting on that would tick a
    booking that errored — the exact class of false claim this system exists to
    prevent, delivered as an emoji."""
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(
        reply="That didn't go through — let me try again.",
        tool_calls=["create_demo_reservation"],
        tools_succeeded=[],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("book it"))

    assert outbound.reactions == []


def test_photos_are_sent_after_the_words(session_factory):
    outbound = RecordingClient(media_base_url="https://cards.example.com")
    agent = StubAgent(AgentTurn(
        reply="Here's the G63 — AED 1,080 a day.",
        media=[{
            "vehicle_id": "veh_11",
            "display_name": "Mercedes-Benz G63 — 2025",
            "images": [
                "assets/vehicles/veh_11.png",
                "assets/vehicles/veh_11_spec.png",
                "assets/vehicles/veh_11_features.png",
            ],
            "caption": None,
        }],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("can i see it"))

    assert len(outbound.images) == 3
    assert outbound.images[0][1] == "https://cards.example.com/assets/vehicles/veh_11.png"
    assert outbound.texts, "the reply itself must still go out"


def test_only_the_first_photo_carries_the_caption(session_factory):
    """WhatsApp prints a caption under every image it is attached to, so
    repeating it turns three photos of one car into one sentence printed three
    times."""
    outbound = RecordingClient(media_base_url="https://cards.example.com")
    agent = StubAgent(AgentTurn(
        reply="Here it is.",
        media=[{
            "vehicle_id": "veh_11",
            "display_name": "Mercedes-Benz G63 — 2025",
            "images": ["assets/vehicles/veh_11.png", "assets/vehicles/veh_11_spec.png"],
            "caption": "The black G63, ready Friday",
        }],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("can i see it"))

    captions = [caption for _, _, caption in outbound.images]
    assert captions == ["The black G63, ready Friday", None]


def test_photos_are_skipped_rather_than_sent_as_local_paths(session_factory):
    """Meta fetches image URLs itself, so an unconfigured base URL must drop the
    photos and log it — a local path renders as a blank bubble on the phone,
    which is worse than no photo at all."""
    outbound = RecordingClient(media_base_url="")
    agent = StubAgent(AgentTurn(
        reply="Here it is.",
        media=[{"vehicle_id": "veh_11", "display_name": "G63",
                "images": ["assets/vehicles/veh_11.png"], "caption": None}],
    ))
    post(webhook(session_factory, outbound, agent), text_payload("can i see it"))

    assert outbound.images == []
    assert outbound.texts, "the reply must still be sent"


def test_the_reaction_map_is_configuration_not_code(session_factory):
    """An operator retunes the feel by editing rules.json. If the mapping were
    hardcoded, "stop putting emoji on my customers' messages" would be a
    deploy."""
    from rental_agent.config import load_rules
    from rental_agent.whatsapp import reactions as reactions_mod

    configured = reactions_mod.configured(load_rules())
    assert configured["on_booking_confirmed"] == "✅"
    assert configured["on_escalation"] is None


def test_escalation_silence_is_declared_in_config_not_left_to_omission():
    """A missing key and an explicit null read the same at runtime and very
    differently to whoever edits the file next."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    raw = json.loads((root / "config" / "rules.json").read_text())
    reactions = raw["messaging"]["reactions"]

    assert "on_escalation" in reactions
    assert reactions["on_escalation"] is None


# --------------------------------------------------------------------------
# Human in the loop — the owner's decision reaching the right customer
#
# The brief's section 3, and the part with the most ways to go quietly wrong.
# The failures worth catching are: the owner being treated as a customer, a
# decision landing on the wrong person's case, an ambiguous reply being guessed
# at, and a decision that never reaches the customer being recorded as resolved.
# --------------------------------------------------------------------------


STAFF = "971500009999"


def owner_payload(text="", message_id="wamid.OWNER1", reply_to=None, button_id=None):
    message = {"from": STAFF, "id": message_id, "timestamp": "1756713600"}
    if button_id:
        message["type"] = "interactive"
        message["interactive"] = {
            "type": "button_reply",
            "button_reply": {"id": button_id, "title": "Approve"},
        }
    else:
        message["type"] = "text"
        message["text"] = {"body": text}
    if reply_to:
        message["context"] = {"id": reply_to}

    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "97144000000", "phone_number_id": "PID"},
        "messages": [message],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def escalating_harness(session_factory, reply="Let me check with a colleague."):
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply=reply, escalated=True))
    client = webhook(session_factory, outbound, agent)
    return client, outbound, agent


def open_a_case(session_factory, said="the AED 400 late fee is unfair"):
    client, outbound, agent = escalating_harness(session_factory)
    post(client, text_payload(said))
    return client, outbound, agent


# -- routing ---------------------------------------------------------------


def test_the_owner_is_never_treated_as_a_customer(session_factory):
    """Before this split, an owner replying to an escalation got a customer
    record and the sales agent tried to rent them a car."""
    client, outbound, agent = open_a_case(session_factory)
    agent.calls.clear()

    post(client, owner_payload("approve"))

    assert agent.calls == [], "the owner's reply must never reach the sales agent"


def test_a_tapped_button_resolves_the_case(session_factory):
    client, outbound, agent = open_a_case(session_factory)
    code = outbound.buttons[0][2][0]["id"].split(":")[1]

    post(client, owner_payload(button_id=f"approve:{code}"))

    assert agent.relays, "the customer must be told"
    assert "approved it" in agent.relays[0]


def test_a_swipe_to_reply_resolves_the_case(session_factory):
    """WhatsApp puts the quoted message id in `context.id`, which is the precise
    route back to one case out of several."""
    client, outbound, agent = open_a_case(session_factory)
    asked = "wamid.ASK1"

    post(client, owner_payload("yes go ahead", reply_to=asked))

    assert agent.relays
    assert "approved it" in agent.relays[0]


def test_a_decision_reaches_the_customer_who_is_waiting(session_factory):
    client, outbound, agent = open_a_case(session_factory)
    outbound.texts.clear()

    post(client, owner_payload("approve"))

    recipients = [to for to, _ in outbound.texts]
    assert "971500000001" in recipients, "the customer hears the outcome"
    assert STAFF in recipients, "the owner gets an acknowledgement"


# -- refusing to guess ------------------------------------------------------


def test_an_ambiguous_owner_reply_is_asked_again_not_guessed(session_factory):
    """'No' and 'no problem' mean opposite things. Guessing resolves a real
    customer's case wrongly, so the buttons go back instead."""
    client, outbound, agent = open_a_case(session_factory)
    outbound.buttons.clear()

    post(client, owner_payload("hmm, depends how long they've rented from us"))

    assert agent.relays == [], "nothing may reach the customer"
    assert outbound.buttons, "the owner is asked again"
    assert "couldn't read that as a yes or a no" in outbound.buttons[0][1]


def test_two_open_cases_with_no_route_are_not_guessed_between(session_factory):
    """With one case open a bare 'approve' is unambiguous. With two it is not,
    and resolving the wrong customer's case is worse than asking."""
    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply="Checking.", escalated=True))
    client = webhook(session_factory, outbound, agent)

    post(client, text_payload("the fee is unfair", message_id="wamid.A", sender="971500000001"))
    post(client, text_payload("i want a refund", message_id="wamid.B", sender="971500000002"))
    assert len(outbound.buttons) == 2

    post(client, owner_payload("approve"))

    assert agent.relays == []
    staff_texts = [t for to, t in outbound.texts if to == STAFF]
    assert any("couldn't tell which case" in t for t in staff_texts)


# -- the customer's experience while waiting --------------------------------


def test_the_agent_does_not_resume_guessing_while_a_case_is_open(session_factory):
    """The escalation existed to stop the agent answering this. Answering the
    customer's next message would resume exactly that."""
    client, outbound, agent = open_a_case(session_factory)
    agent.calls.clear()
    outbound.texts.clear()

    post(client, text_payload("any update?", message_id="wamid.TEST2"))

    assert agent.calls == [], "the agent must stay out of it until a person answers"
    assert outbound.texts, "but the customer must not be left on read"
    assert "still waiting to hear back" in outbound.texts[0][1]


def test_the_conversation_resumes_once_the_case_is_relayed(session_factory):
    client, outbound, agent = open_a_case(session_factory)
    post(client, owner_payload("approve"))
    agent.calls.clear()
    agent.turn = AgentTurn(reply="Of course — when would you like it?")

    post(client, text_payload("great, can i book another one", message_id="wamid.TEST3"))

    assert agent.calls, "with the case closed the agent takes over again"


# -- what the agent is told -------------------------------------------------


def test_the_relay_forbids_generalising_the_decision(session_factory):
    """One 'fine, waive it this once' must not become the standing policy the
    next customer is quoted."""
    client, outbound, agent = open_a_case(session_factory)

    post(client, owner_payload("approve"))

    directive = agent.relays[0]
    assert "this customer only" in directive
    assert "not a change to" in directive


def test_a_decline_may_not_be_softened(session_factory):
    client, outbound, agent = open_a_case(session_factory)

    post(client, owner_payload("decline"))

    directive = agent.relays[0]
    assert "declined it" in directive
    assert "do not soften a decline" in directive.lower()


def test_the_owners_own_words_are_carried_not_paraphrased(session_factory):
    client, outbound, agent = open_a_case(session_factory)

    post(client, owner_payload("yes but only half of it, and only this once"))

    assert "only half of it, and only this once" in agent.relays[0]


# --------------------------------------------------------------------------
# The 24-hour service window
#
# WhatsApp only delivers free-form messages for 24 hours after a customer
# writes. An owner answering an escalation the next morning is entirely normal,
# and before this the reply was rejected by Meta, the case was marked resolved,
# and the customer heard nothing — a silent failure with no error anywhere.
# --------------------------------------------------------------------------


def age_conversation(session_factory, hours):
    """The clock is frozen, so age the conversation instead — push every
    recorded message back in time until the window has closed."""
    from datetime import timedelta

    from sqlalchemy import select

    from rental_agent.store.models import Message

    with session_factory() as session:
        for m in session.scalars(select(Message)):
            m.created_at = m.created_at - timedelta(hours=hours)
        session.commit()


def _ctx(session):
    """A context bound to the one demo conversation these tests create."""
    from rental_agent.context import ToolContext

    ctx = ToolContext(session=session, now_fn=lambda: FROZEN_NOW, reference_date=REFERENCE_DATE)
    customer, _ = ctx.customers.get_or_create("971500000001", FROZEN_NOW)
    conversation, _ = ctx.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
    ctx.customer_id = customer.customer_id
    ctx.conversation_id = conversation.conversation_id
    return ctx


def test_a_decision_inside_the_window_goes_straight_to_the_customer(session_factory):
    client, outbound, agent = open_a_case(session_factory)
    outbound.texts.clear()

    post(client, owner_payload("approve"))

    assert agent.relays, "the agent wrote the reply"
    assert "971500000001" in [to for to, _ in outbound.texts]
    assert outbound.templates == [], "no template needed while the window is open"


def test_a_decision_after_the_window_shuts_is_not_silently_lost(session_factory):
    """The bug: Meta rejects the free-form reply, the case is marked resolved,
    and nobody ever tells the customer."""
    client, outbound, agent = open_a_case(session_factory)
    age_conversation(session_factory, 30)
    outbound.texts.clear()

    post(client, owner_payload("approve"))

    assert outbound.templates, "a template must go out to reopen the conversation"
    to, name, params = outbound.templates[0]
    assert to == "971500000001"
    assert name == "case_update"


def test_a_decision_owed_across_the_window_is_still_owed(session_factory):
    """A template earns a reply; it does not deliver the answer. Until the
    customer actually hears the decision, the case is not resolved."""
    from rental_agent.services import handover

    client, outbound, agent = open_a_case(session_factory)
    age_conversation(session_factory, 30)
    post(client, owner_payload("approve"))

    with session_factory() as session:
        ctx = _ctx(session)
        case = handover.decided_awaiting_relay(ctx, ctx.conversation_id or "")
        assert case is not None, "still owed"
        assert case.relayed_at is None
        assert case.reopen_requested_at is not None


def test_the_answer_arrives_the_moment_the_customer_replies(session_factory):
    """Their reply reopens the window, so the decision goes out before anything
    else — they have been owed it since yesterday."""
    from rental_agent.services import handover

    client, outbound, agent = open_a_case(session_factory)
    age_conversation(session_factory, 30)
    post(client, owner_payload("approve"))
    outbound.texts.clear()
    agent.calls.clear()

    post(client, text_payload("hi, any news?", message_id="wamid.BACK"))

    assert agent.relays, "the decision is delivered"
    assert "971500000001" in [to for to, _ in outbound.texts]
    assert agent.calls == [], "the sales agent does not answer over the top of it"

    with session_factory() as session:
        ctx = _ctx(session)
        assert handover.decided_awaiting_relay(ctx, ctx.conversation_id or "") is None


def test_a_timeout_nudge_is_not_sent_into_a_closed_window(session_factory):
    """No point burning a template on "sorry, still waiting" when the decision
    itself will need one."""
    client, outbound, agent = open_a_case(session_factory)
    age_conversation(session_factory, 30)
    outbound.texts.clear()

    # Any inbound event triggers the overdue sweep.
    post(client, owner_payload("hmm let me think"))

    assert [t for to, t in outbound.texts if to == "971500000001"] == []


def test_the_paths_that_skip_a_turn_still_record_the_customers_message(session_factory):
    """Found by the simulator on its first real run. Relaying an owed decision
    and holding a waiting customer both answer *without* running an agent turn,
    and the agent is what normally writes the message to the transcript.

    Two things break when it is lost: the transcript the evaluator reads is
    missing a customer turn, and the 24-hour window is measured from exactly
    that record — so it never reopens, and the next message would wrongly go out
    as a template."""
    from rental_agent.whatsapp import window as window_mod

    client, outbound, agent = open_a_case(session_factory)

    # Held while the case is open.
    post(client, text_payload("any update?", message_id="wamid.HELD"))
    # Answered after the window shut.
    age_conversation(session_factory, 30)
    post(client, owner_payload("approve"))
    post(client, text_payload("hi, any news?", message_id="wamid.BACK"))

    with session_factory() as session:
        ctx = _ctx(session)
        said = [
            m.content
            for m in ctx.messages.for_conversation(ctx.conversation_id or "")
            if m.direction == "inbound"
        ]
        assert "any update?" in said, "the held message must reach the transcript"
        assert "hi, any news?" in said, "so must the one that reopened the window"
        assert window_mod.is_open(ctx, ctx.conversation_id or ""), "their reply reopened it"


def test_a_redelivery_of_a_held_message_is_not_answered_twice(session_factory):
    """Meta redelivers. The holding path records the message itself now, so it
    needs its own dedup — the agent's is not in play."""
    client, outbound, agent = open_a_case(session_factory)
    post(client, text_payload("any update?", message_id="wamid.HELD"))
    outbound.texts.clear()

    post(client, text_payload("any update?", message_id="wamid.HELD"))

    assert outbound.texts == []


def test_an_operators_own_photograph_is_fetched_from_their_server(harness):
    """Their images are stored absolute. Meta fetches image URLs itself, so
    prefixing a base onto one would produce nonsense — and copying them into the
    repo would go stale the moment they change a car."""
    _, outbound, _ = harness
    absolute = "https://deltarentalsdubai.com/wp-content/uploads/2026/03/GLS-Maybach-1.jpg"
    assert outbound.card_url(absolute) == absolute


def test_a_generated_card_still_needs_a_public_base(harness):
    _, outbound, _ = harness
    outbound.settings = WhatsAppSettings(
        phone_number_id="PID", access_token="TOK", media_base_url="https://cards.example.com"
    )
    assert outbound.card_url("assets/vehicles/veh_01.png") == (
        "https://cards.example.com/assets/vehicles/veh_01.png"
    )


def test_a_finished_conversation_is_evaluated_on_the_next_inbound(session_factory):
    """The half of the learning loop that is safe to run unattended: it consults
    no model and changes nothing a customer sees. Everything after it — turning
    findings into lessons, proving them, activating them — stays deliberate.

    What is checked here is the wiring: that a conversation which has ended gets
    judged without anybody asking, and is not judged twice. Whether the checks
    find the right things is `test_evaluation`'s job.
    """
    from datetime import timedelta

    from rental_agent.context import ToolContext

    def seed(outcome, hours_ago, handle):
        with session_factory() as setup:
            ctx = ToolContext(session=setup, now_fn=lambda: FROZEN_NOW,
                              reference_date=REFERENCE_DATE)
            customer, _ = ctx.customers.get_or_create(handle, FROZEN_NOW)
            conversation, _ = ctx.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
            for direction, text in [("inbound", "how much?"), ("outbound", "Let me check.")]:
                ctx.messages.record(conversation_id=conversation.conversation_id,
                                    direction=direction, content=text, now=FROZEN_NOW)
            conversation.sales_outcome = outcome
            conversation.last_message_at = FROZEN_NOW - timedelta(hours=hours_ago)
            setup.commit()
            return conversation.conversation_id

    ended = seed("dropped", 48, "+971500000077")
    still_going = seed("booked", 1, "+971500000078")

    outbound = RecordingClient()
    agent = StubAgent(AgentTurn(reply="Happy to help."))
    app = create_app(
        session_factory=session_factory, agent_factory=lambda: agent,
        settings=WhatsAppSettings(phone_number_id="PID", access_token="TOK",
                                  app_secret=APP_SECRET, verify_token=VERIFY_TOKEN),
        client=outbound, reference_date=REFERENCE_DATE, now_fn=lambda: FROZEN_NOW,
    )
    client = TestClient(app)
    post(client, text_payload("hi", message_id="wamid.LATER1"))

    with session_factory() as check:
        judged = [e.conversation_id for e in check.query(Evaluation).all()]
    # The finished one, and not the one the customer is still in the middle of.
    assert judged == [ended]
    assert still_going not in judged

    # And a second message does not judge it again — mistake counts track
    # distinct conversations, and judging one twice would inflate them.
    post(client, text_payload("still there?", message_id="wamid.LATER2"))

    with session_factory() as check:
        assert [e.conversation_id for e in check.query(Evaluation).all()] == [ended]
