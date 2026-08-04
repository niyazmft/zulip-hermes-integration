"""Health probe for Zulip connectivity.

Lightweight pre-flight check that validates credentials and connectivity
without side effects (no message reads, no state changes).
"""

import logging
import urllib.request
from typing import Optional

from .logger import mask_pii

logger = logging.getLogger(__name__)

# Private IP ranges to reject (SSRF protection)
_PRIVATE_PREFIXES = [
    "127.",
    "10.",
    "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.",
    "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
    "192.168.",
    "169.254.",
    "0.",
    "255.",
]

_AWS_METADATA_IP = "169.254.169.254"


def _is_internal_host(hostname: str) -> bool:
    """Check if hostname resolves to an internal/private IP."""
    hostname = hostname.lower().strip()
    if hostname == _AWS_METADATA_IP:
        return True
    for prefix in _PRIVATE_PREFIXES:
        if hostname.startswith(prefix):
            return True
    return False


def _normalize_base_url(raw: str) -> Optional[str]:
    """Validate and normalize a Zulip base URL.

    Rejects non-HTTP schemes and internal IPs to prevent SSRF.
    Returns normalized URL with trailing slash removed, or None if invalid.
    """
    url = raw.strip()
    if not url:
        return None

    # Ensure scheme is http or https
    lower = url.lower()
    if lower.startswith("http://"):
        scheme = "http"
        rest = url[7:]
    elif lower.startswith("https://"):
        scheme = "https"
        rest = url[8:]
    else:
        logger.warning("probe: rejected non-HTTP scheme in URL: %s", raw)
        return None

    # Extract hostname
    hostname = rest.split("/")[0].split(":")[0]
    if not hostname:
        return None

    # Reject internal IPs
    if _is_internal_host(hostname):
        logger.warning("probe: rejected internal IP in URL: %s", raw)
        return None

    # Reject localhost names
    if hostname in ("localhost", "localhost.localdomain"):
        logger.warning("probe: rejected localhost in URL: %s", raw)
        return None

    # Remove trailing slash for consistency
    return f"{scheme}://{rest.rstrip(chr(47))}"


def _validate_media_url(url: str) -> bool:
    """Validate a media URL for SSRF safety.

    Returns True if the URL is safe to fetch (public HTTP(S) only).
    Rejects:
    - Non-HTTP(S) protocols (file://, ftp://, etc.)
    - Internal/private IP addresses
    - Localhost
    - Empty URLs
    """
    if not url or not url.strip():
        return False

    url = url.strip()

    # Only allow http and https schemes
    lower = url.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        logger.warning("media URL rejected: non-HTTP scheme [url=%s]", mask_pii(url))
        return False

    # Extract hostname
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
    except Exception:
        logger.warning("media URL rejected: unparseable [url=%s]", mask_pii(url))
        return False

    # Reject internal IPs
    if _is_internal_host(hostname):
        logger.warning("media URL rejected: internal host [url=%s]", mask_pii(url))
        return False

    # Reject localhost names
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        logger.warning("media URL rejected: localhost [url=%s]", mask_pii(url))
        return False

    return True


async def probe_zulip(
    site: str,
    email: str,
    api_key: str,
    timeout: int = 10,
) -> dict:
    """Probe Zulip server connectivity and authentication.

    Returns {"ok": True, "bot": {"id": "...", "email": "...", "full_name": "..."}}
    or {"ok": False, "error": "..."}.

    This is a read-only operation — no side effects on the server.
    """
    base_url = _normalize_base_url(site)
    if not base_url:
        return {"ok": False, "error": "invalid baseUrl"}

    auth_header = f"Basic {__import__('base64').b64encode(f'{email}:{api_key}'.encode()).decode()}"

    req = urllib.request.Request(
        f"{base_url}/api/v1/users/me",
        headers={"Authorization": auth_header},
    )

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(None, urllib.request.urlopen, req),
            timeout=timeout,
        )
        data = __import__("json").loads(response.read().decode("utf-8"))

        if data.get("result") != "success":
            return {"ok": False, "error": data.get("msg", "Zulip API error")}

        return {
            "ok": True,
            "bot": {
                "id": str(data.get("user_id", "")),
                "email": data.get("email"),
                "full_name": data.get("full_name"),
            },
        }

    except asyncio.TimeoutError:
        return {"ok": False, "error": f"probe timed out after {timeout}s"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"probe failed: {e}"}
