"""Media upload handling for Zulip inbound and outbound messages.

- Extract upload URLs from Zulip HTML content
- Download attachments with size validation
- Save to temp files for AI processing
- Outbound file upload via /user_uploads
"""

import asyncio
import errno
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
    Security: verifies the resolved file is under tmp, data_dir, or an
    operator allowlist dir; rejects symlinks using atomic stat with
    follow_symlinks=False to prevent TOCTOU races.

    Relative paths (e.g. a bare ``haiku.txt`` from the agent workspace) are
    resolved against candidate roots in order — the given path, the bot
    workspace, the data dir, then tmpdir — mirroring the sibling OpenClaw
    plugin's relative-attachment resolution (#268). Each candidate is still
    gated by the allowlist and the O_NOFOLLOW symlink check.
    """
    tmp_dir = Path(tempfile.gettempdir()).resolve()
    allowed_data = Path(data_dir).expanduser().resolve()
    workspace_dir = Path(tempfile.gettempdir()).resolve() / "hermes_bot_workspace"
    allowed_roots = [tmp_dir, allowed_data]

    # HERMES_MEDIA_ALLOW_DIRS: same operator allowlist env var the gateway
    # core honors in gateway.platforms.base._media_delivery_allowed_roots
    # (colon- or comma-separated absolute dirs). By the time send_image_file/
    # send_document reach here, the gateway has ALREADY run
    # filter_media_delivery_paths against this exact allowlist plus its
    # denylist — so this check is defense-in-depth, not the primary gate,
    # and must not be stricter than the gateway's own decision or an
    # approved path (e.g. a browser-harness screenshot cache dir) gets
    # silently dropped a second time here.
    for chunk in os.environ.get("HERMES_MEDIA_ALLOW_DIRS", "").split(os.pathsep):
        for raw_root in chunk.split(","):
            raw_root = raw_root.strip()
            if not raw_root:
                continue
            try:
                allowed_roots.append(Path(raw_root).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue

    def _is_within(p: Path, root: Path) -> bool:
        # True containment, not string prefix: str.startswith() would wrongly
        # accept a sibling like "/tmp/allowed-but-not-really" as being under
        # "/tmp/allowed". Path.relative_to() only succeeds for real descendants
        # (or the root itself).
        try:
            p.relative_to(root)
            return True
        except ValueError:
            return False

    def _resolve_candidate(candidate: str) -> Path:
        """Resolve one candidate path, rejecting symlinks and unauthorized roots."""
        original = Path(candidate)

        # Atomic symlink rejection using O_NOFOLLOW to prevent TOCTOU races.
        # Open the file with O_NOFOLLOW so the kernel rejects symlinks atomically.
        try:
            fd = os.open(str(original), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise ValueError(f"File not found: {candidate}")
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ValueError(f"Symlink rejected: {candidate}")
            raise ValueError(f"Cannot access file: {candidate}: {e}")

        # If we got a file descriptor, it's not a symlink (O_NOFOLLOW would have
        # raised ELOOP/EMLINK for symlinks). Now resolve the path for the
        # authorized-path check.
        try:
            resolved = Path(os.path.realpath(original))
        finally:
            os.close(fd)

        if not any(_is_within(resolved, root) for root in allowed_roots):
            raise ValueError(
                f"Refusing to upload from unauthorized path: {candidate}. "
                f"Allowed: {', '.join(str(r) for r in allowed_roots)}"
            )
        return resolved

    # Candidate resolution: try the given path first, then (for relative
    # paths) the bot workspace, data dir, and tmpdir. Each is gated by the
    # allowlist + O_NOFOLLOW check above.
    candidates = [file_path]
    if not os.path.isabs(file_path):
        candidates += [
            str(workspace_dir / file_path),
            str(allowed_data / file_path),
            str(tmp_dir / file_path),
        ]

    resolved = None
    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            resolved = _resolve_candidate(candidate)
            break
        except ValueError as e:
            last_error = e
            continue

    if resolved is None:
        raise last_error or ValueError(f"Unable to read file: {file_path}")

    # Re-open for reading (safe: already validated)
    with open(resolved, "rb") as f:
        result = await asyncio.to_thread(client.upload_file, f)

    if result.get("result") != "success":
        raise RuntimeError(f"Upload failed: {result}")

    uri = result.get("uri", "")
    if not uri:
        raise RuntimeError("Upload response missing uri")

    if uri.startswith("/"):
        base = getattr(client, "base_url", "")
        # client.base_url (python-zulip-api) always ends in "/api/" (see
        # zulip.Client.__init__: it strips trailing slashes then
        # unconditionally appends "/api/"), and the upload endpoint's own
        # `uri` in the response is server-root-relative (e.g.
        # "/user_uploads/..."), NOT api-relative. Concatenating the two
        # verbatim produces "https://host/api//user_uploads/..." — a
        # double slash that 404s. Strip the "/api/" (or "/api") suffix so
        # we land back at the server root before appending uri.
        if base.endswith("/api/"):
            base = base[: -len("/api/")]
        elif base.endswith("/api"):
            base = base[: -len("/api")]
        else:
            base = base.rstrip("/")
        uri = base + uri

    return uri
