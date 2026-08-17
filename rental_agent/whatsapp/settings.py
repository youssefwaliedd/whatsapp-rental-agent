"""WhatsApp Cloud API configuration.

All four values come from the Meta app dashboard and live in `.env`, never in
code. Two of them are secrets.

Nothing here is required to *build* or test the transport — the webhook parses
and verifies simulated payloads without credentials. They are only needed to
actually send and receive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..env import load_dotenv

load_dotenv()

#: Meta pins the Graph API version in the URL. Pinning it here too means an
#: upstream default change cannot silently alter behaviour mid-demo.
GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


@dataclass(frozen=True)
class WhatsAppSettings:
    #: Numeric id from WhatsApp → API Setup. Not the phone number itself.
    phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    #: Secret. Temporary tokens from the dashboard expire after 24 hours.
    access_token: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    #: Secret. App settings → Basic → App secret. Proves a webhook came from Meta.
    app_secret: str = os.getenv("WHATSAPP_APP_SECRET", "")
    #: A string you invent; Meta echoes it back during webhook setup.
    verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "")

    #: Where escalations are sent. A colleague's WhatsApp number, in full
    #: international form without a plus.
    staff_number: str = os.getenv("WHATSAPP_STAFF_NUMBER", "")

    #: Public base URL the vehicle cards are served from, so Meta can fetch
    #: them. GitHub Pages is the intended host.
    media_base_url: str = os.getenv("WHATSAPP_MEDIA_BASE_URL", "")

    @property
    def configured(self) -> bool:
        """Whether sending is possible. Receiving needs only `app_secret`."""
        return bool(self.phone_number_id and self.access_token)

    @property
    def can_verify_signatures(self) -> bool:
        return bool(self.app_secret)

    def missing(self) -> list[str]:
        """Which settings are absent, for a clear message rather than a 401."""
        names = {
            "WHATSAPP_PHONE_NUMBER_ID": self.phone_number_id,
            "WHATSAPP_ACCESS_TOKEN": self.access_token,
            "WHATSAPP_APP_SECRET": self.app_secret,
            "WHATSAPP_VERIFY_TOKEN": self.verify_token,
        }
        return [name for name, value in names.items() if not value]
