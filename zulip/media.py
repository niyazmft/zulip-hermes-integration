"""Media upload handling for Zulip inbound and outbound messages.

- Extract upload URLs from Zulip HTML content
- Download attachments with size validation
- Save to temp files for AI processing
- Outbound file upload via /user_uploads
"""

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, unquote

logger = logging.getLogger(__name__)

# Match Zulip upload paths in HTML
UPLOAD_PATH_RE = re.compile(r"/user_uploads/\d+/[a-zA-Z0-9_/-]+/[^\s\"'<>]+")

DEFAULT_MAX_MB = 5


def extract_upload_urls(html_content: str, base_url: str) -> list[str]:
    """Find Zulip upload URLs in HTML message content.

    Returns absolute URLs filtered to same origin only.
    """
    if not html_content or "/user_uploads/" not in html_content:
        return []

    base_origin = ""
    try:
        base_origin = urlparse(base_url).netloc
    except Exception:
        pass

    urls = set()
    for match in UPLOAD_PATH_RE.finditer(html_content):
        raw = match.group(0)
        absolute = urljoin(base_url, raw)
        try:
            parsed = urlparse(absolute)
            if base_origin and parsed.netloc != base_origin:
                continue
            if "/user_uploads/" not in parsed.path:
                continue
            urls.add(absolute)
        except Exception:
            continue

    return sorted(urls)


async def download_upload(
    url: str,
    auth_header: str,
    max_bytes: int,
    base_url: str,
) -> dict:
    """Download a Zulip upload with size validation.

    Uses requests via asyncio.to_thread for compatibility with sync SDK.

    Returns {"path": str, "content_type": str, "filename": str}
    or raises on failure.
    """
    try:
        import requests
    except ImportError:
        raise ImportError("requests package required for media download")

    base_origin = urlparse(base_url).netloc
    target = urlparse(url)
    if target.netloc != base_origin or "/user_uploads/" not in target.path:
        raise ValueError("Refusing to download from non-Zulip origin")

    def _fetch():
        return requests.get(
            url,
            headers={"Authorization": f"Basic {auth_header}"},
            stream=True,
            timeout=30,
        )

    response = await asyncio.to_thread(_fetch)

    if not response.ok:
        raise RuntimeError(f"Download failed: {response.status_code}")

    content_length = response.headers.get("Content-Length")
    if content_length:
        length = int(content_length)
        if length > max_bytes:
            raise RuntimeError(f"Upload exceeds max size ({length} > {max_bytes})")

    data = response.content
    if len(data) > max_bytes:
        raise RuntimeError(f"Upload exceeds max size ({len(data)} > {max_bytes})")

    content_type = response.headers.get("Content-Type", "application/octet-stream")
    filename = _resolve_filename(url, response.headers.get("Content-Disposition"))

    fd, path = tempfile.mkstemp(suffix=f"_{filename}")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)

    return {"path": path, "content_type": content_type, "filename": filename}


def _resolve_filename(url: str, content_disposition: Optional[str]) -> str:
    """Resolve filename from Content-Disposition or URL path."""
    filename = ""
    if content_disposition:
        m = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
        if m:
            filename = unquote(m.group(1))
        else:
            m = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
            if m:
                filename = m.group(1)

    if not filename:
        filename = Path(urlparse(url).path).name or "upload.bin"

    return filename


def resolve_media_max_mb() -> int:
    """Read max upload size from environment."""
    raw = os.getenv("ZULIP_MEDIA_MAX_MB", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_MAX_MB


async def upload_file_to_zulip(
    client: Any,
    file_path: str,
    data_dir: str,
) -> str:
    """Upload a local file to Zulip server.

    Returns the uploaded file URL.
    Security: verifies file_path is under tmp or data_dir.
    """
    resolved = Path(file_path).resolve()
    tmp_dir = Path(tempfile.gettempdir()).resolve()
    allowed_data = Path(data_dir).expanduser().resolve()

    if not (
        str(resolved).startswith(str(tmp_dir))
        or str(resolved).startswith(str(allowed_data))
    ):
        raise ValueError(
            f"Refusing to upload from unauthorized path: {file_path}. "
            f"Allowed: {tmp_dir} or {allowed_data}"
        )

    with open(resolved, "rb") as f:
        result = await client.upload_file(f)

    if result.get("result") != "success":
        raise RuntimeError(f"Upload failed: {result}")

    uri = result.get("uri", "")
    if not uri:
        raise RuntimeError("Upload response missing uri")

    if uri.startswith("/"):
        base = getattr(client, "base_url", "")
        uri = base + uri

    return uri
