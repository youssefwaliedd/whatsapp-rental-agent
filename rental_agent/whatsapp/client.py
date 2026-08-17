"""Sending messages through the Cloud API.

Kept deliberately thin: build the request, post it, report what happened. It
never raises into the conversation loop — a failed send is returned as a result
so the caller can log it and carry on, because losing the ability to reply is
not a reason to also lose the customer's message.

WhatsApp caps a text message at 4096 characters, so long replies are split on
paragraph boundaries rather than truncated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import GRAPH_BASE, WhatsAppSettings

#: Cloud API hard limit for a text body.
MAX_BODY = 4096
#: Split below the limit so a paragraph is never cut mid-sentence.
SPLIT_TARGET = 3500


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None


def split_message(text: str, limit: int = SPLIT_TARGET) -> list[str]:
    """Break a long reply on paragraph, then sentence, then hard boundaries.

    A quote cut mid-number is worse than two messages, so this never truncates.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 3:
            cut = window.rfind("\n")
        if cut < limit // 3:
            cut = window.rfind(". ")
            cut = cut + 1 if cut != -1 else -1
        if cut < limit // 3:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


class WhatsAppClient:
    def __init__(self, settings: WhatsAppSettings | None = None, transport: Any = None):
        self.settings = settings or WhatsAppSettings()
        #: Injectable so every send path is testable without a network.
        self._transport = transport

    # -- plumbing --------------------------------------------------------

    @property
    def _url(self) -> str:
        return f"{GRAPH_BASE}/{self.settings.phone_number_id}/messages"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(payload)

        response = httpx.post(
            self._url,
            json=payload,
            headers={"Authorization": f"Bearer {self.settings.access_token}"},
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json()

    def _send(self, payload: dict[str, Any]) -> SendResult:
        if not self.settings.configured:
            return SendResult(
                ok=False,
                error="WhatsApp is not configured — set WHATSAPP_PHONE_NUMBER_ID "
                "and WHATSAPP_ACCESS_TOKEN in .env",
            )
        try:
            body = self._post(payload)
        except Exception as exc:  # noqa: BLE001 - a failed send must not kill the turn
            return SendResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return SendResult(
            ok=True,
            message_ids=[m.get("id", "") for m in body.get("messages", []) or []],
        )

    # -- sending ---------------------------------------------------------

    def send_text(self, to: str, text: str) -> SendResult:
        """Send a reply, split across messages if it exceeds the body limit."""
        parts = split_message(text)
        if not parts:
            return SendResult(ok=True)

        ids: list[str] = []
        for part in parts:
            result = self._send(
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to,
                    "type": "text",
                    # Link previews off: a vehicle card should render as the
                    # image we sent, not as a scraped link card.
                    "text": {"preview_url": False, "body": part},
                }
            )
            if not result.ok:
                return SendResult(ok=False, message_ids=ids, error=result.error)
            ids.extend(result.message_ids)
        return SendResult(ok=True, message_ids=ids)

    def send_image(self, to: str, image_url: str, caption: str | None = None) -> SendResult:
        """Send a vehicle card by public URL.

        Meta fetches the URL itself, so it must be reachable from the internet —
        a localhost path silently fails to render for the customer.
        """
        image: dict[str, Any] = {"link": image_url}
        if caption:
            image["caption"] = caption[:1024]  # Cloud API caption limit
        return self._send(
            {"messaging_product": "whatsapp", "to": to, "type": "image", "image": image}
        )

    def mark_read(self, message_id: str) -> SendResult:
        """Show the customer their message was seen while the agent thinks.

        Turns take a while; blue ticks make that read as attention rather than
        silence.
        """
        return self._send(
            {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        )

    # -- media -----------------------------------------------------------

    def card_url(self, image_path: str) -> str | None:
        """Turn a fleet.json image path into a public URL Meta can fetch."""
        base = (self.settings.media_base_url or "").rstrip("/")
        if not base:
            return None
        return f"{base}/{image_path.lstrip('/')}"
