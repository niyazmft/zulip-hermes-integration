"""Health probe for Zulip connectivity.

Lightweight pre-flight check that validates credentials and connectivity
without side effects (no message reads, no state changes).
"""

import logging
import os
import urllib.request
from typing import Mapping, Optional

from .logger import mask_pii

logger = logging.getLogger(__name__)

# Opt-in that relaxes the transport checks below for a self-hosted realm on a
# trusted network. Named here so every message can point at it.
INSECURE_HTTP_ENV = "ZULIP_ALLOW_INSECURE_HTTP"

_FALSY = {"0", "false", "no", "off"}

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


def allow_insecure_http_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """``ZULIP_ALLOW_INSECURE_HTTP`` — opt-in for a self-hosted http:// realm.

    Default off. This is read *here*, at normalization time, rather than
    threaded through every call site by hand — deliberately. The sibling had a
    bug where the option was silently discarded on later calls because the URL
    was re-normalized without it, so a realm that validated at setup failed at
    runtime; reading the environment where the decision is made makes that
    failure mode impossible rather than merely tested.
    """
    raw = (os.environ if env is None else env).get(INSECURE_HTTP_ENV)
    if raw is None or str(raw).strip() == "":
        return False
    return str(raw).strip().lower() not in _FALSY


def base_url_error(raw: str) -> str:
    """Explain a refused base URL, naming the opt-in when that is the cause.

    An operator who typed ``http://`` should learn the switch exists instead of
    guessing; the sibling's base-URL errors failed generically at first. The two
    refusal reasons are reported separately on purpose — accepting a cleartext
    URL risks interception, accepting a private address is an SSRF decision, and
    an operator should know which one they are making.
    """
    if not _normalize_base_url(raw, allow_insecure_http=True):
        return (
            f"invalid ZULIP_SITE {mask_pii(raw)!r}: expected an https:// URL "
            f"for a public host"
        )

    if raw.strip().lower().startswith("http://"):
        detail = "the bot API key would be sent as HTTP Basic in cleartext"
    else:
        detail = "it is a private/internal address"
    return (
        f"refused insecure ZULIP_SITE {mask_pii(raw)!r}: {detail}, so it "
        f"requires the explicit {INSECURE_HTTP_ENV}=1 opt-in"
    )


def _normalize_base_url(
    raw: str, allow_insecure_http: Optional[bool] = None
) -> Optional[str]:
    """Validate and normalize a Zulip base URL.

    Requires ``https://`` and a public host by default, because Zulip sends the
    bot API key as HTTP Basic on **every** request: a plain-http realm would put
    that credential on the wire in cleartext.

    ``allow_insecure_http`` is the operator opt-in for a self-hosted realm on a
    trusted network (default: read :data:`INSECURE_HTTP_ENV`). It relaxes the
    scheme check and the internal/localhost checks **together** — a LAN Zulip is
    itself a private address, so relaxing only one would still reject the exact
    case the opt-in exists for.

    This does not touch :func:`_validate_media_url`: its host can be influenced
    by an inbound message, so it must stay strict regardless of the opt-in.

    Returns the normalized URL with any trailing slash removed, or None.
    """
    insecure_allowed = (
        allow_insecure_http_enabled()
        if allow_insecure_http is None
        else bool(allow_insecure_http)
    )

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
        logger.warning("probe: rejected non-HTTP scheme in URL: %s", mask_pii(raw))
        return None

    if scheme == "http" and not insecure_allowed:
        logger.warning(
            "probe: refused insecure http:// ZULIP_SITE (the bot API key is sent "
            "as HTTP Basic on every request); set %s=1 to allow it for a "
            "self-hosted realm on a trusted network [site=%s]",
            INSECURE_HTTP_ENV,
            mask_pii(raw),
        )
        return None

    # Extract hostname
    hostname = rest.split("/")[0].split(":")[0]
    if not hostname:
        return None

    # Reject internal IPs
    if _is_internal_host(hostname):
        if not insecure_allowed:
            logger.warning("probe: rejected internal IP in URL: %s", mask_pii(raw))
            return None
        logger.warning(
            "probe: allowing private/internal ZULIP_SITE under %s [site=%s]",
            INSECURE_HTTP_ENV,
            mask_pii(raw),
        )

    # Reject localhost names
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        if not insecure_allowed:
            logger.warning("probe: rejected localhost in URL: %s", mask_pii(raw))
            return None
        logger.warning(
            "probe: allowing localhost ZULIP_SITE under %s [site=%s]",
            INSECURE_HTTP_ENV,
            mask_pii(raw),
        )

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
        return {"ok": False, "error": base_url_error(site)}

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
