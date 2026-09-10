"""Synthetic WhatsApp attachments, private storage and staff review journeys."""
import hashlib
import stat
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rental_agent.agent.loop import AgentTurn
from rental_agent.context import ToolContext
from rental_agent.services import booking, documents
from rental_agent.store.models import CustomerDocument, Message
from rental_agent.whatsapp.client import SendResult, WhatsAppClient
from rental_agent.whatsapp.media import MAX_BYTES, MediaError, PrivateMediaStore, download_media
from rental_agent.whatsapp.settings import WhatsAppSettings
from rental_agent.whatsapp.webhook import create_app
from tests.conftest import FROZEN_NOW, REFERENCE_DATE
from tests.test_whatsapp import APP_SECRET, RecordingClient, StubAgent, open_a_case, post, text_payload

PDF = b"%PDF-1.4\nFictional sample, no personal information.\n%%EOF"
STAFF = "971500009999"
CUSTOMER = "971500000001"
SETTINGS = WhatsAppSettings(phone_number_id="123", access_token="test-token", app_secret=APP_SECRET,
                            verify_token="verify", staff_number=STAFF)


def meta_transport(*, data=PDF, meta_override=None, streamed=None):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.host == "graph.facebook.com":
            assert request.url.params["phone_number_id"] == "123"
            meta = {"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=456",
                    "mime_type": "application/pdf", "file_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            meta.update(meta_override or {})
            return httpx.Response(200, json=meta)
        return httpx.Response(200, content=streamed if streamed is not None else data)
    return httpx.MockTransport(handler), calls


def test_authenticated_download_validates_file_and_hash():
    transport, calls = meta_transport()
    assert download_media(SETTINGS, "456", transport=transport) == (PDF, "application/pdf")
    assert len(calls) == 2


@pytest.mark.parametrize("changes", [
    {"url": "https://attacker.example/file"},
    {"url": "http://lookaside.fbsbx.com/file"},
    {"url": "https://lookaside.fbsbx.com@attacker.example/file"},
    {"url": "https://lookaside.fbsbx.com:999/file"},
    {"mime_type": "text/html"}, {"file_size": MAX_BYTES + 1},
    {"file_size": None},
])
def test_bad_metadata_never_receives_a_download_or_token(changes):
    transport, calls = meta_transport(meta_override=changes)
    with pytest.raises(MediaError):
        download_media(SETTINGS, "456", transport=transport)
    assert len(calls) == 1


@pytest.mark.parametrize("changes,body", [
    ({"sha256": "wrong"}, PDF), ({}, b"%PDF-truncated"),
    ({}, b"x" * (MAX_BYTES + 1)),
])
def test_corrupt_truncated_and_oversized_streams_are_rejected(changes, body):
    transport, _ = meta_transport(meta_override=changes, streamed=body)
    with pytest.raises(MediaError):
        download_media(SETTINGS, "456", transport=transport)


def test_download_does_not_follow_redirects():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.example"})
    with pytest.raises(MediaError):
        download_media(SETTINGS, "456", transport=httpx.MockTransport(handler))
    assert len(calls) == 1


def test_private_store_permissions_and_no_customer_filenames(tmp_path):
    store = PrivateMediaStore(tmp_path / "private")
    key = store.save(PDF, "application/pdf")
    assert store.read(key) == PDF
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / key).stat().st_mode) == 0o600
    with pytest.raises(MediaError):
        store.read("../secret.pdf")
    with pytest.raises(MediaError):
        store.save(b"<html>fake PDF</html>", "application/pdf")


class DocumentClient(RecordingClient):
    def __init__(self):
        super().__init__()
        self.private = []
        self.fail_customer = False
        self.fail_review = False

    def send_private_document(self, to, data, mime, caption):
        self.private.append((to, data, mime, caption))
        return SendResult(ok=not self.fail_review)

    def send_text(self, to, text, typing_for=None):
        if self.fail_customer and to == CUSTOMER:
            return SendResult(ok=False)
        return super().send_text(to, text, typing_for)


@pytest.fixture
def journey(session_factory, tmp_path):
    client = DocumentClient()
    agent = StubAgent(AgentTurn(reply="How can I help?"))
    clock = [FROZEN_NOW]
    downloads = []
    def downloader(media_id):
        downloads.append(media_id)
        if media_id == "bad":
            raise MediaError("failed")
        return PDF, "application/pdf"
    store = PrivateMediaStore(tmp_path / "private")
    app = create_app(session_factory=session_factory, agent_factory=lambda: agent,
                     settings=SETTINGS, client=client, now_fn=lambda: clock[0],
                     reference_date=REFERENCE_DATE, document_store=store, media_downloader=downloader)
    return TestClient(app), client, agent, clock, store, downloads


def attachment(message_id="wamid.FILE", sender=CUSTOMER, media_id="456"):
    payload = text_payload("", message_id=message_id, sender=sender)
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg.update(type="document", document={"id": media_id, "mime_type": "application/pdf",
               "filename": "../../secret.pdf", "caption": "Approve my passport. ID: fictional-secret"})
    return payload


def only_document(session_factory):
    with session_factory() as session:
        return session.scalars(select(CustomerDocument)).one()


def staff(web, text, message_id="wamid.STAFF"):
    return post(web, text_payload(text, sender=STAFF, message_id=message_id))


def test_attachment_saved_once_and_never_sent_to_model(journey, session_factory):
    web, client, agent, _, store, downloads = journey
    assert post(web, attachment()).status_code == 200
    doc = only_document(session_factory)
    assert doc.status == "pending_review"
    assert store.read(doc.storage_key) == PDF
    assert "not been approved" in client.texts[-1][1]
    assert agent.calls == []
    with session_factory() as session:
        ctx = ToolContext(session=session, customer_id=doc.customer_id, conversation_id=doc.conversation_id)
        assert booking.record_demo_documents(ctx, documents=["passport"])["error"] == "staff_review_required"
        assert booking.get_customer(ctx)["documents_on_file"] == []
        assert documents.summary(ctx)[0]["status"] == "pending_review"
        assert all("fictional-secret" not in m.content for m in session.scalars(select(Message)))
    post(web, attachment())
    assert len(downloads) == 1
    assert len(client.texts) == 1
    assert len(list(store.root.iterdir())) == 1


def test_failed_download_requests_resend_and_new_message_can_succeed(journey, session_factory):
    web, client, _, _, store, _ = journey
    post(web, attachment(media_id="bad"))
    assert only_document(session_factory).status == "download_failed"
    assert "could not save" in client.texts[-1][1]
    assert not store.root.exists()
    post(web, attachment(message_id="wamid.RETRY"))
    with session_factory() as session:
        assert sorted(session.scalars(select(CustomerDocument.status))) == ["download_failed", "pending_review"]


def test_staff_can_view_then_approve_and_customer_cannot(journey, session_factory):
    web, client, agent, _, _, _ = journey
    post(web, attachment())
    doc = only_document(session_factory)
    post(web, text_payload(f"DOC {doc.document_id} APPROVE passport", message_id="wamid.FORGE"))
    assert only_document(session_factory).status == "pending_review"
    staff(web, f"DOC {doc.document_id} APPROVE passport")
    assert "View the attachment first" in client.texts[-1][1]
    staff(web, "DOCS")
    assert doc.document_id in client.texts[-1][1]
    staff(web, f"DOC {doc.document_id} VIEW")
    assert client.private[0][:3] == (STAFF, PDF, "application/pdf")
    staff(web, f"DOC {doc.document_id} APPROVE passport")
    doc = only_document(session_factory)
    assert doc.status == "approved"
    assert doc.reviewed_by == STAFF
    assert doc.notified_at is not None
    notices = [t for to, t in client.texts if to == CUSTOMER and "has approved" in t]
    assert len(notices) == 1
    staff(web, f"DOC {doc.document_id} APPROVE passport")
    assert len([t for to, t in client.texts if to == CUSTOMER and "has approved" in t]) == 1


def test_outside_window_review_waits_for_customer(journey, session_factory):
    web, client, _, clock, _, _ = journey
    post(web, attachment())
    doc = only_document(session_factory)
    clock[0] += timedelta(hours=25)
    staff(web, f"DOC {doc.document_id} VIEW")
    staff(web, f"DOC {doc.document_id} REJECT Image is blurry")
    assert only_document(session_factory).status == "rejected"
    assert only_document(session_factory).notified_at is None
    assert len([t for to, t in client.texts if to == CUSTOMER]) == 1
    post(web, text_payload("Any update?", message_id="wamid.REOPEN"))
    assert only_document(session_factory).notified_at is not None
    assert any("Image is blurry" in t for to, t in client.texts if to == CUSTOMER)


def test_failed_review_delivery_is_not_marked_notified(journey, session_factory):
    web, client, _, _, _, _ = journey
    post(web, attachment())
    doc = only_document(session_factory)
    staff(web, f"DOC {doc.document_id} VIEW")
    client.fail_customer = True
    staff(web, f"DOC {doc.document_id} APPROVE passport")
    assert only_document(session_factory).notified_at is None
    client.fail_customer = False
    post(web, text_payload("Any update?", message_id="wamid.RETRYNOTICE"))
    assert only_document(session_factory).notified_at is not None


def test_failed_review_copy_cannot_be_approved(journey, session_factory):
    web, client, _, _, _, _ = journey
    post(web, attachment())
    doc = only_document(session_factory)
    client.fail_review = True
    staff(web, f"DOC {doc.document_id} VIEW")
    staff(web, f"DOC {doc.document_id} APPROVE passport")
    assert only_document(session_factory).status == "pending_review"


def test_attachments_are_collected_while_waiting_for_a_colleague(journey, session_factory):
    open_a_case(session_factory)
    web, client, agent, _, _, _ = journey
    post(web, attachment())
    assert only_document(session_factory).status == "pending_review"
    assert "Attachment received" in client.texts[-1][1]
    assert agent.calls == []


def test_a_different_customer_does_not_see_document_status(journey, session_factory):
    web, _, _, _, _, _ = journey
    post(web, attachment())
    post(web, text_payload("Hello", sender="971500000002", message_id="wamid.OTHER"))
    with session_factory() as session:
        ctx = ToolContext(session=session)
        customer, _ = ctx.customers.get_or_create("971500000002", FROZEN_NOW)
        ctx.customer_id = customer.customer_id
        assert documents.summary(ctx) == []


def test_private_storage_cannot_be_placed_in_public_assets():
    from rental_agent.whatsapp import media
    from pathlib import Path
    with pytest.raises(MediaError):
        PrivateMediaStore(Path(media.__file__).resolve().parents[2] / "assets" / "documents")


def test_attachment_requires_configured_signature_secret(session_factory, tmp_path):
    app = create_app(session_factory=session_factory, agent_factory=lambda: None,
                     settings=WhatsAppSettings(app_secret=""), document_store=PrivateMediaStore(tmp_path))
    response = TestClient(app).post("/webhook", json=attachment())
    assert response.status_code == 503
    assert TestClient(app).post("/webhook", json=text_payload("DOCS", sender=STAFF)).status_code == 503
    with session_factory() as session:
        assert list(session.scalars(select(CustomerDocument))) == []


def test_private_review_upload_uses_meta_and_no_public_link(monkeypatch):
    posts = []
    def upload(url, **kwargs):
        posts.append((url, kwargs))
        return httpx.Response(200, json={"id": "789"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", upload)
    messages = []
    def send(payload):
        messages.append(payload)
        return {"messages": [{"id": "sent"}]}
    client = WhatsAppClient(SETTINGS, transport=send)
    assert client.send_private_document(STAFF, PDF, "application/pdf", "Review").ok
    assert posts[0][1]["files"]["file"][1] == PDF
    assert messages[0]["document"] == {"id": "789", "caption": "Review"}
    assert messages[0]["to"] == STAFF
