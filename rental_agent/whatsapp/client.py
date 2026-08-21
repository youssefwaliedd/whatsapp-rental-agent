"""Sending messages through the Cloud API.

Kept deliberately thin: build the request, post it, report what happened. It
never raises into the conversation loop — a failed send is returned as a result
so the caller can log it and carry on, because losing the ability to reply is
not a reason to also lose the customer's message.

WhatsApp caps a text message at 4096 characters, so long replies are split on
paragraph boundaries rather than truncated.

Beyond plain text this speaks the rest of the WhatsApp vocabulary — typing
indicators, reactions, and photo sequences — because a customer judges an agent
partly on whether it behaves like a participant in the app or like something
piping text into it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .pacing import Pacer
from .settings import GRAPH_BASE, WhatsAppSettings

#: Cloud API hard limit for a text body.
MAX_BODY = 4096

#: WhatsApp uses *bold*, _italic_, ~strike~ — not markdown. A model reaching for
#: **bold** out of habit would show a customer literal asterisks, so the markup
#: is normalised on the way out rather than left to the prompt to remember.
_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
#: Split below the limit so a paragraph is never cut mid-sentence.
SPLIT_TARGET = 3500
#: Cloud API caption limit for an image.
MAX_CAPTION = 1024
#: Cloud API limits on an interactive button message. Enforced when building
#: the payload so an over-long label fails a test rather than a live escalation.
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_BUTTON_ID = 256
MAX_BUTTON_BODY = 1024
#: A typing indicator lasts about 25 seconds, or until a message is sent. Worth
#: knowing rather than assuming it holds for a slow turn.
TYPING_TTL_SECONDS = 25


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None


def to_whatsapp_markup(text: str) -> str:
    """Convert stray markdown into what WhatsApp actually renders.

    `**total**` would reach the customer as asterisks around their price, which
    looks broken at exactly the moment they are deciding to trust the figure.
    """
    text = _MD_BOLD.sub(r"*\1*", text or "")
    text = _MD_HEADING.sub("", text)
    return _MD_BULLET.sub("• ", text)


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
    def __init__(
        self,
        settings: WhatsAppSettings | None = None,
        transport: Any = None,
        pacer: Pacer | None = None,
    ):
        self.settings = settings or WhatsAppSettings()
        #: Injectable so every send path is testable without a network.
        self._transport = transport
        #: Owns the only sleep in the transport. Injectable for the same reason.
        self.pacer = pacer or Pacer()

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

    def send_text(self, to: str, text: str, typing_for: str | None = None) -> SendResult:
        """Send a reply, split across messages if it exceeds the body limit.

        The first part goes immediately. Anything after it is paced, with the
        typing indicator re-shown in the gap, so a long answer arrives the way a
        person would send it rather than all at once. `typing_for` is the
        customer's message id, which is what a typing indicator attaches to.
        """
        parts = split_message(to_whatsapp_markup(text))
        if not parts:
            return SendResult(ok=True)

        ids: list[str] = []
        for index, part in enumerate(parts):
            if index > 0:
                # Show the indicator first, then wait — otherwise the customer
                # watches a silent gap and only then sees "typing".
                if typing_for:
                    self.send_typing(typing_for)
                self.pacer.before_part(index, part)

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
            image["caption"] = to_whatsapp_markup(caption)[:MAX_CAPTION]
        return self._send(
            {"messaging_product": "whatsapp", "to": to, "type": "image", "image": image}
        )

    def send_images(
        self,
        to: str,
        image_urls: list[str],
        caption: str | None = None,
        typing_for: str | None = None,
    ) -> SendResult:
        """Send several photos of one vehicle, paced like a person sending them.

        Only the first carries the caption. WhatsApp renders a caption under
        every image it is attached to, so repeating it turns three photos of one
        car into the same sentence printed three times.

        A failure part-way through is reported with the ids that did land, so
        the caller knows the customer saw something rather than nothing.
        """
        ids: list[str] = []
        for index, url in enumerate(image_urls):
            if index > 0:
                if typing_for:
                    self.send_typing(typing_for)
                # An image needs no composing time; pace it on the caption so a
                # bare photo sequence still arrives at a human rhythm.
                self.pacer.wait(self.pacer.pacing.min_seconds)

            result = self.send_image(to, url, caption if index == 0 else None)
            if not result.ok:
                return SendResult(ok=False, message_ids=ids, error=result.error)
            ids.extend(result.message_ids)
        return SendResult(ok=True, message_ids=ids)

    def send_buttons(
        self, to: str, body: str, buttons: list[dict[str, str]]
    ) -> SendResult:
        """Ask a question with tappable answers.

        Used for owner decisions rather than customer conversation: "no" and "no
        problem" mean opposite things, and misreading a busy owner's one-word
        reply resolves a real customer's case wrongly. A button cannot be
        misread.

        The Cloud API allows at most three buttons with 20-character titles, so
        both are enforced here rather than discovered as a 400 at the moment an
        escalation needs to go out.
        """
        if not buttons:
            return self.send_text(to, body)

        return self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": to_whatsapp_markup(body)[:MAX_BUTTON_BODY]},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": button["id"][:MAX_BUTTON_ID],
                                    "title": button["title"][:MAX_BUTTON_TITLE],
                                },
                            }
                            for button in buttons[:MAX_BUTTONS]
                        ]
                    },
                },
            }
        )

    def send_reaction(self, to: str, message_id: str, emoji: str) -> SendResult:
        """React to one specific customer message.

        An empty emoji removes an existing reaction, which is the Cloud API's
        own convention — so a caller with nothing to say sends nothing at all
        rather than an empty string that would silently clear a previous mark.
        """
        if not emoji:
            return SendResult(ok=True)
        return self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji},
            }
        )

    def mark_read(self, message_id: str) -> SendResult:
        """Show the customer their message was seen while the agent thinks.

        Turns take a while; blue ticks make that read as attention rather than
        silence.
        """
        return self._send(
            {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        )

    def send_typing(self, message_id: str) -> SendResult:
        """Mark read *and* show the typing bubble, in one request.

        The Cloud API attaches a typing indicator to the read receipt rather
        than exposing it separately, so this replaces `mark_read` wherever a
        reply is actually coming. It expires after roughly
        `TYPING_TTL_SECONDS`, or the moment a message is sent — so a turn slower
        than that needs it re-sent, which is why the paced send paths do.
        """
        return self._send(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            }
        )

    # -- media -----------------------------------------------------------

    def card_url(self, image_path: str) -> str | None:
        """Turn a fleet.json image path into a public URL Meta can fetch."""
        base = (self.settings.media_base_url or "").rstrip("/")
        if not base:
            return None
        return f"{base}/{image_path.lstrip('/')}"
