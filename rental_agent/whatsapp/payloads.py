"""Parsing what Meta actually sends.

The Cloud API webhook is a deeply nested envelope that carries several unrelated
things down the same pipe: customer messages, delivery receipts, read receipts,
and account notifications. Only the first is a conversation.

Everything here is a pure function over a payload dict, so the whole parsing
layer is verified against realistic envelopes without a phone number, a token,
or a network.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

#: Message types a customer can send that we can act on as text.
_TEXTUAL = {"text", "button", "interactive"}


@dataclass
class InboundMessage:
    """One customer message, flattened out of the envelope."""

    message_id: str
    from_number: str
    text: str
    timestamp: str | None = None
    contact_name: str | None = None
    message_type: str = "text"
    #: True for a message we can read but not act on — an image, a location.
    unsupported: bool = False
    #: The message this one is a reply to. WhatsApp fills this in on a
    #: swipe-to-reply, and it is how an owner's answer finds the case it belongs
    #: to when several are open at once.
    reply_to: str | None = None
    #: The id of a tapped button, which carries our own routing information.
    #: Unambiguous in a way a typed reply is not.
    button_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Check `X-Hub-Signature-256` against the raw request body.

    Meta signs the exact bytes it sent, so this must run on the raw body before
    any JSON parsing or re-serialisation — a round trip through a dict changes
    key order and whitespace and the signature stops matching.

    Compared with `compare_digest`, because a naive `==` on a MAC leaks timing.
    """
    if not app_secret or not header:
        return False
    if not header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def _button_id_of(message: dict[str, Any]) -> str | None:
    """The id behind a tapped button, whichever shape it arrives in.

    Template buttons carry a `payload`; interactive reply buttons carry an `id`.
    Both are ours — we generated them — so both are trustworthy routing.
    """
    kind = message.get("type", "")
    if kind == "button":
        return (message.get("button") or {}).get("payload") or None
    if kind == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key) or {}
            if reply.get("id"):
                return reply["id"]
    return None


def _text_of(message: dict[str, Any]) -> tuple[str, bool]:
    """The customer's words, and whether we could read them at all."""
    kind = message.get("type", "")

    if kind == "text":
        return (message.get("text") or {}).get("body", ""), False

    if kind == "button":
        return (message.get("button") or {}).get("text", ""), False

    if kind == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key) or {}
            if reply.get("title"):
                return reply["title"], False
        return "", True

    # An image, a voice note, a location. The customer said something we cannot
    # read — worth acknowledging rather than dropping silently.
    return "", True


def parse_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Every customer message in a webhook payload.

    Delivery and read receipts arrive through the same endpoint and are not
    conversation — they are ignored here rather than mistaken for turns.
    """
    messages: list[InboundMessage] = []

    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            if not value.get("messages"):
                continue  # a status update, not a message

            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts", []) or []
            }

            for message in value["messages"]:
                text, unsupported = _text_of(message)
                messages.append(
                    InboundMessage(
                        message_id=message.get("id", ""),
                        from_number=message.get("from", ""),
                        text=text,
                        timestamp=message.get("timestamp"),
                        contact_name=names.get(message.get("from")),
                        message_type=message.get("type", "unknown"),
                        unsupported=unsupported,
                        reply_to=(message.get("context") or {}).get("id"),
                        button_id=_button_id_of(message),
                        raw=message,
                    )
                )
    return messages


def parse_statuses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Delivery and read receipts. Useful for the evaluator, not for the agent."""
    statuses = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            statuses.extend((change.get("value") or {}).get("statuses", []) or [])
    return statuses


def is_status_only(payload: dict[str, Any]) -> bool:
    return not parse_messages(payload) and bool(parse_statuses(payload))
