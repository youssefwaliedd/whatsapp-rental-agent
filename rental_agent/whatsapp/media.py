"""Bounded, authenticated Meta media downloads and private local storage.

Files are never served by FastAPI. Automatic checks send bytes only to the
dedicated document reader, not the conversational agent. Production uses the
encrypted storage backend with restricted backups.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .settings import GRAPH_BASE, WhatsAppSettings

MAX_BYTES = 10 * 1024 * 1024
MIME_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}


class MediaError(Exception):
    pass


class TransientMediaError(MediaError):
    """A retryable provider/network failure, without sensitive response text."""


def validate_file(data: bytes, mime: str) -> None:
    signatures = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n", "application/pdf": b"%PDF-"}
    if mime not in signatures or not data.startswith(signatures[mime]):
        raise MediaError("Please send a JPG, PNG or PDF file.")
    if len(data) > MAX_BYTES:
        raise MediaError("Please send a file smaller than 10 MB.")


class PrivateMediaStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("WHATSAPP_DOCUMENT_DIR", ".private_documents")).resolve()
        public_assets = Path(__file__).resolve().parents[2] / "assets"
        if self.root == public_assets or public_assets in self.root.parents:
            raise MediaError("The document directory must be outside public assets.")

    def save(self, data: bytes, mime: str) -> str:
        validate_file(data, mime)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        key = uuid4().hex + MIME_EXTENSIONS[mime]
        fd = os.open(self.root / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except OSError:
            (self.root / key).unlink(missing_ok=True)
            raise
        return key

    def read(self, key: str) -> bytes:
        if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|pdf)", key):
            raise MediaError("Invalid storage reference.")
        path = self.root / key
        if path.is_symlink():
            raise MediaError("Invalid storage reference.")
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise MediaError("Stored file is too large.")
        return data

    def delete(self, key: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|pdf)", key):
            raise MediaError("Invalid storage reference.")
        (self.root / key).unlink(missing_ok=True)


def download_media(settings: WhatsAppSettings, media_id: str, *, transport=None) -> tuple[bytes, str]:
    if (not settings.configured or not isinstance(media_id, str)
            or not media_id.isascii() or not media_id.isdigit()):
        raise MediaError("Could not retrieve the attachment. Please attach it again.")
    headers = {"Authorization": f"Bearer {settings.access_token}"}
    try:
        with httpx.Client(timeout=20, follow_redirects=False, transport=transport) as client:
            response = client.get(f"{GRAPH_BASE}/{media_id}", headers=headers,
                                  params={"phone_number_id": settings.phone_number_id})
            response.raise_for_status()
            meta = response.json()
            mime = meta.get("mime_type", "")
            size = meta.get("file_size")
            if mime not in MIME_EXTENSIONS:
                raise MediaError("Please send a JPG, PNG or PDF file.")
            if not isinstance(size, int) or size <= 0 or size > MAX_BYTES:
                raise MediaError("Please send a file smaller than 10 MB.")
            url = meta.get("url", "")
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname not in {"lookaside.fbsbx.com", "lookaside.facebook.com"}
                    or parsed.username or parsed.password or parsed.port not in {None, 443}):
                raise MediaError("Could not retrieve the attachment. Please attach it again.")
            data = bytearray()
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise MediaError("Please send a file smaller than 10 MB.")
            body = bytes(data)
            validate_file(body, mime)
            if len(body) != size or hashlib.sha256(body).hexdigest() != meta.get("sha256"):
                raise MediaError("The attachment was incomplete. Please attach it again.")
            return body, mime
    except MediaError:
        raise
    except httpx.HTTPError as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        if status is None or status == 429 or status >= 500:
            raise TransientMediaError("Attachment service is temporarily unavailable") from None
        raise MediaError("Could not retrieve the attachment. Please attach it again.") from None
    except (ValueError, TypeError, AttributeError):
        # Do not expose signed URLs, credentials or processor response bodies.
        raise MediaError("Could not retrieve the attachment. Please attach it again.") from None
