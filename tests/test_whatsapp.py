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


def test_an_image_is_flagged_as_unreadable_rather_than_dropped():
    [message] = payloads.parse_messages(image_payload())
    assert message.unsupported is True
    assert message.message_type == "image"


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

    def send_text(self, to, text, typing_for=None):
        self.texts.append((to, text))
        return SendResult(ok=True, message_ids=["wamid.OUT"])

    def send_image(self, to, image_url, caption=None):
        self.images.append((to, image_url, caption))
        return SendResult(ok=True, message_ids=["wamid.IMG"])

    def send_reaction(self, to, message_id, emoji):
        self.reactions.append((to, message_id, emoji))
        return SendResult(ok=True)

    def mark_read(self, message_id):
        self.read.append(message_id)
        return SendResult(ok=True)

    def send_typing(self, message_id):
        self.typing.append(message_id)
        self.read.append(message_id)  # a typing indicator marks read too
        return SendResult(ok=True)


class StubAgent:
    def __init__(self, turn: AgentTurn):
        self.turn = turn
        self.calls: list[tuple[str, str | None]] = []

    def respond(self, ctx, message, *, provider_message_id=None):
        self.calls.append((message, provider_message_id))
        return self.turn


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


def test_an_image_gets_an_explanation_not_silence(harness):
    client, outbound, agent = harness
    post(client, image_payload())
    assert agent.calls == []
    assert outbound.texts == [("971500000001", UNSUPPORTED_REPLY)]


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

    recipients = [to for to, _ in outbound.texts]
    assert "971500000001" in recipients      # the customer
    assert "971500009999" in recipients      # the colleague
    staff_note = next(t for to, t in outbound.texts if to == "971500009999")
    assert "Escalation" in staff_note and "I crashed the car" in staff_note


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
