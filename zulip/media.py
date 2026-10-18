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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, unquote

from . import runtime_scope
from .logger import mask_pii

logger = logging.getLogger(__name__)

# Match a Zulip upload path, in the forms the plugin actually receives.
#
# The content is raw Markdown, not HTML: the plugin never asks Zulip for
# ``apply_markdown``, so a link arrives as ``[shot.png](/user_uploads/2/ab/c/shot.png)``.
# The old pattern was written for ``href="..."`` and kept the Markdown link's
# closing token -- ``/user_uploads/2/ab/c/shot.png)`` -- so the download 404'd
# (issue #271). A trailing ``)``, ``]``, quote or backtick now ends the match.
#
# The optional leading group matters for a different reason: it lets a
# protocol-relative ``//other.example/user_uploads/...`` be matched WHOLE, so
# the origin check below sees another site. Matching only from
# ``/user_uploads/`` would resolve it against our own base and hide the
# cross-origin link.
UPLOAD_PATH_RE = re.compile(
    r"(?:[a-zA-Z][a-zA-Z0-9+.\-]*://)?[^\s()<>\[\]\"'`]*?/user_uploads/"
    r"\d+/[a-zA-Z0-9_/-]+/[^\s()<>\[\]\"'`]+"
)

DEFAULT_MAX_MB = 5


# --- Upload denylist (Issue #138) ---
#
# The allowlist is a *root* check, and the data dir is an allowed root on
# purpose (the bot workspace lives under it), so a root check can never refuse
# the files that matter most: the audit log, the queue/dedupe state and the
# persisted allowlist all sit inside an allowed root. The threat is prompt
# injection — an agent reads a config file and attaches it — so the fix has to
# be an explicit, name-based refusal evaluated *before* the allowlist.
#
# Matching is deliberately conservative: a false refusal costs one refused
# upload plus an audit line, while a false accept can publish a credential.
_DENY_NAME_SUBSTRINGS = (
    "credential",
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "token",
    "passwd",
    "password",
)
_DENY_NAME_PATTERNS = (
    # .env, config.env, .env.local
    re.compile(r"(^|\.)env($|\.)", re.IGNORECASE),
    # the audit log and its rotated siblings ({account}.audit.log.N)
    re.compile(r"\.audit\.log($|\.)", re.IGNORECASE),
    # our own persisted state, by the names the stores actually use
    re.compile(r"^zulip_allowlist\.json$", re.IGNORECASE),
    re.compile(r"^zulip_(dedupe|queue)_.+\.json$", re.IGNORECASE),
)
_DENY_DIR_PARTS = frozenset({"credentials", "sessions", "transcripts"})


class SensitiveUploadRefused(ValueError):
    """An upload named a credential, config or state file (Issue #138).

    Subclasses ValueError so existing callers and tests keep catching it, but
    the candidate loop re-raises it explicitly rather than treating it as a
    miss — see :func:`upload_file_to_zulip`.
    """


def deny_reason(path: Path) -> Optional[str]:
    """Why ``path`` must never be attached, or None if it is acceptable.

    Checks sensitive directory components anywhere in the path, then the
    basename against the credential-shaped substrings and the explicit state
    file patterns.
    """
    for part in path.parts:
        if part.lower() in _DENY_DIR_PARTS:
            return f"sensitive directory: {part}"

    name = path.name.lower()
    for fragment in _DENY_NAME_SUBSTRINGS:
        if fragment in name:
            return f"credential-shaped filename: {fragment}"
    for pattern in _DENY_NAME_PATTERNS:
        if pattern.search(name):
            return f"matches {pattern.pattern}"
    return None


async def _audit_refused_upload(
    data_dir: str,
    account_id: Optional[str],
    file_path: str,
    error: Exception,
) -> None:
    """Record a refused upload. Best-effort: auditing cannot change the outcome.

    The path is masked and no file content is ever read for the event — the
    whole point is that this material must not leave the host.
    """
    try:
        from .audit_logger import AuditLogger

        await AuditLogger(
            data_dir=data_dir, account_id=account_id or "default"
        ).log_event(
            "media_upload_blocked",
            {
                "file": mask_pii(str(file_path)),
                "reason": str(error),
                "direction": "outbound",
            },
        )
    except Exception as e:
        # Never silent: a swallowed failure here is exactly how a refusal goes
        # unrecorded. (This bit during development — a missing import in this
        # helper was invisible because the exception vanished.)
        logger.warning(
            "could not audit refused upload [err=%s]", mask_pii(str(e))
        )


class MediaRefused(Exception):
    """An inbound upload was refused, and the reason is safe to show a human.

    Deliberately not a bare ``ValueError``: the inbound path must be able to
    tell "this attachment is not ours / too large / did not answer" from a bug
    in our own code, because the first becomes one line in the agent's turn and
    the second must not take the turn down (issue #271).
    """


@dataclass
class InboundMedia:
    """What the inbound path collected for one message.

    Mirrors :class:`gateway.platforms.event.MessageEvent`'s media fields, so the
    caller can assign them straight across. ``refusals`` holds one agent-facing
    line per upload that could not be read -- the agent is told, in the turn,
    that it has no file and why, instead of meeting a dead link.
    """

    paths: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    text_inlined: list[bool] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def has_images(self) -> bool:
        return any(t.startswith("image/") for t in self.media_types)


def _basic_auth(email: str, api_key: str) -> str:
    """Zulip's HTTP Basic credential: base64(email:api_key)."""
    import base64

    return base64.b64encode(f"{email}:{api_key}".encode()).decode()


def extract_upload_urls(html_content: str, base_url: str) -> list[str]:
    """Find Zulip upload URLs in Markdown message content.

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


async def fetch_upload(
    url: str, auth_header: str, max_bytes: int, base_url: str
) -> dict:
    """Download one inbound upload, same-site only, without following redirects.

    Returns ``{"data": bytes, "content_type": str, "filename": str}``. A refusal
    raises :class:`MediaRefused` with a short message fit for a chat turn.

    Three properties, each because the alternative was a hole:

    * **same scheme and host as the Zulip site** -- the path in a message is
      attacker-supplyable, so a download that trusts it would let a user point
      the bot's authenticated request at any host it can reach.
    * **no redirects** -- ``requests`` follows them by default, which would
      defeat that check one hop later.
    * **a size cap enforced before and after reading** -- ``Content-Length`` is
      a claim, and an absent one is not zero.
    """
    base = urlparse(base_url)
    target = urlparse(url)
    if (
        target.scheme != base.scheme
        or target.netloc != base.netloc
        or not target.path.startswith("/user_uploads/")
    ):
        raise MediaRefused("not an upload on this Zulip site")

    try:
        import requests
    except ImportError:
        raise ImportError("requests package required for media download")

    def _fetch():
        return requests.get(
            url,
            headers={"Authorization": f"Basic {auth_header}"},
            stream=True,
            timeout=30,
            allow_redirects=False,
        )

    try:
        response = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise MediaRefused(f"the download failed: {e}") from e

    if 300 <= response.status_code < 400:
        raise MediaRefused(
            "the upload replied with a redirect; refusing to follow it off the site"
        )
    if not response.ok:
        raise MediaRefused(f"the server answered {response.status_code}")

    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise MediaRefused(
                    f"the upload is larger than the allowed limit "
                    f"({declared} > {max_bytes} bytes)"
                )
        except ValueError:
            pass  # a malformed header is a claim we cannot use; the body check stands

    data = response.content
    if len(data) > max_bytes:
        raise MediaRefused(
            f"the upload is larger than the allowed limit "
            f"({len(data)} > {max_bytes} bytes)"
        )

    return {
        "data": data,
        "content_type": response.headers.get("Content-Type", "application/octet-stream"),
        "filename": _resolve_filename(url, response.headers.get("Content-Disposition")),
    }


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


def _host_cache_media_bytes(
    data: bytes, filename: str, mime_type: str
) -> Optional[dict]:
    """Let the host classify and cache inbound bytes, when it can.

    Hermes' own cache is where the host looks for media and, on the profile it
    launched with, where it cleans up. ``cache_media_bytes`` classifies the
    bytes, checks that an image really is one, and returns a path plus the MIME
    type ``MessageEvent.media_types`` expects. It exists on every supported host
    (``__min_hermes__`` 0.18.2 included), but this stays a *capability*: an
    absent or broken host must degrade to a temp file, never take the turn down.
    """
    try:
        from gateway.platforms.base import cache_media_bytes
    except ImportError:
        # The symbol is absent on this host: fall back to a temp file. Guarding
        # on ImportError specifically (not Exception) is what the static guard in
        # tests/test_guarded_imports.py can verify (#132/#271).
        return None

    try:
        cached = cache_media_bytes(data, filename=filename, mime_type=mime_type)
    except Exception as e:  # a host cache that raises is not a reason to drop a turn
        logger.warning("host media cache failed [err=%s]", e)
        return None

    if cached is None:
        return None

    return {
        "path": cached.path,
        "media_type": cached.media_type,
        "kind": cached.kind,
    }


def cache_inbound_upload(data: bytes, filename: str, mime_type: str) -> dict:
    """Persist one inbound upload where the agent can open it.

    Prefers the host cache (see :func:`_host_cache_media_bytes`); falls back to a
    system temp file when the host has no cache to offer. Returns
    ``{"path", "media_type", "kind"}``.
    """
    try:
        host_cached = _host_cache_media_bytes(data, filename, mime_type)
        if host_cached:
            return host_cached
    except Exception as e:  # patched or unusual host helper; still cache the file
        logger.warning("host media cache raised [err=%s]", e)

    fd, path = tempfile.mkstemp(suffix=f"_{filename}")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)

    media_type = mime_type or "application/octet-stream"
    return {
        "path": path,
        "media_type": media_type,
        "kind": "image" if media_type.startswith("image/") else "document",
    }


def resolve_media_max_mb() -> int:
    """Read max upload size from environment."""
    raw = runtime_scope.get_setting("ZULIP_MEDIA_MAX_MB", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_MAX_MB


async def collect_inbound_media(adapter: Any, content: str) -> InboundMedia:
    """Turn the upload links in ONE message into host-ready media.

    Called on the live message only -- never on the quoted-history block, which
    is also part of the agent-facing body. Extracting after that prepend would
    fetch every attachment ever mentioned in the topic, an unbounded set a user
    can grow at will (issue #271).

    One refusal never costs the turn: the agent is told, in the text, that it has
    no file and why -- a dead Markdown link is what this function exists to
    replace.
    """
    result = InboundMedia()
    urls = extract_upload_urls(content, getattr(adapter, "site", "") or "")
    if not urls:
        return result

    max_bytes = resolve_media_max_mb() * 1024 * 1024
    auth = _basic_auth(
        getattr(adapter, "email", "") or "", getattr(adapter, "api_key", "") or ""
    )

    for url in urls:
        try:
            fetched = await fetch_upload(url, auth, max_bytes, adapter.site)
            cached = await asyncio.to_thread(
                cache_inbound_upload,
                fetched["data"],
                fetched["filename"],
                fetched["content_type"],
            )
        except MediaRefused as exc:
            result.refusals.append(f"[an upload could not be read: {exc}]")
            continue
        except Exception as exc:
            logger.warning(
                "inbound upload failed [url=%s]: %s", mask_pii(url), mask_pii(str(exc))
            )
            result.refusals.append("[an upload could not be read: the download failed]")
            continue

        result.paths.append(cached["path"])
        result.media_types.append(cached["media_type"])
        # We never inline a file's content into the turn's text, so the host is
        # told so explicitly rather than left to hermes' legacy default.
        result.text_inlined.append(False)

    return result


async def upload_file_to_zulip(
    client: Any,
    file_path: str,
    data_dir: str,
    account_id: Optional[str] = None,
) -> str:
    """Upload a local file to Zulip server.

    Returns the uploaded file URL.
    Security: refuses credential/config/state files by name (Issue #138) before
    the allowlist roots are consulted, then verifies the resolved file is under
    tmp, data_dir, or an operator allowlist dir; rejects symlinks using atomic
    stat with follow_symlinks=False to prevent TOCTOU races.

    ``account_id`` only affects where a refused-upload audit event is written.

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
    allow_dirs_raw = runtime_scope.get_setting("HERMES_MEDIA_ALLOW_DIRS", "") or ""
    for chunk in allow_dirs_raw.split(os.pathsep):
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

        # Denylist BEFORE the allowlist (Issue #138): the data dir is itself an
        # allowed root and holds the audit log, queue/dedupe state and the
        # persisted allowlist, so a root check can never refuse them — and an
        # operator allow dir must not be able to authorize a credential file.
        reason = deny_reason(resolved)
        if reason:
            raise SensitiveUploadRefused(
                f"Refusing to upload sensitive file: {candidate} ({reason})"
            )

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
    try:
        for candidate in candidates:
            try:
                resolved = _resolve_candidate(candidate)
                break
            except SensitiveUploadRefused:
                # Fail fast: do not go looking for another root with the same
                # name — the name is exactly what made it sensitive, and a
                # fallback would turn a refusal into a *different* file.
                raise
            except ValueError as e:
                last_error = e
                continue
    except SensitiveUploadRefused as refused:
        await _audit_refused_upload(data_dir, account_id, file_path, refused)
        raise

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
