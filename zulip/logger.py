"""Structured logging and PII masking for Zulip adapter observability.

Produces machine-parseable logs in the format:
    message [k=v k=v]

PII masking rules:
- Emails: n***@domain.com
- Numeric IDs: 12***78 (length-aware)
- Stream names: st***am
- Prefixed values: user:..., dm:..., stream:...
"""

import re
from typing import Optional

# Patterns for detecting sensitive data in log messages
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_API_KEY_RE = re.compile(r"\b([A-Za-z0-9]{32,}|[A-Za-z0-9+/=]{40,})\b")
_IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b|"
    r"(?:[0-9a-fA-F]{1,4}:){1,7}:|"
    r"(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}\b"
)


def format_zulip_log(message: str, **fields) -> str:
    """Format a log message with key=value fields.

    Skips None, empty string, and False values.
    """
    parts = []
    for key, value in fields.items():
        if value is None or value == "" or value is False:
            continue
        if isinstance(value, (dict, list)):
            import json
            parts.append(f"{key}={json.dumps(value)}")
        else:
            parts.append(f"{key}={value}")
    if parts:
        return f"{message} [{ ' '.join(parts) }]"
    return message


def mask_pii(value: Optional[str | int]) -> str:
    """Mask sensitive information for safe logging.

    Handles emails, numeric IDs, stream names, and prefixed values.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # Handle prefixed values
    if s.startswith("user:"):
        return f"user:{mask_pii(s[5:])}"
    if s.startswith("dm:"):
        return f"dm:{mask_pii(s[3:])}"
    if s.startswith("zulip:"):
        return f"zulip:{mask_pii(s[6:])}"
    if s.startswith("stream:"):
        rest = s[7:]
        parts = rest.split(":", 1)
        masked = mask_pii(parts[0]) if parts[0] else "***"
        if len(parts) > 1:
            return f"stream:{masked}:{mask_pii(parts[1])}"
        return f"stream:{masked}"

    # Email
    if "@" in s:
        user, domain = s.split("@", 1)
        if user and domain:
            masked_user = f"{user[0]}***" if len(user) > 1 else "***"
            return f"{masked_user}@{domain}"

    # Numeric ID
    if s.isdigit():
        if len(s) <= 2:
            return "**"
        if len(s) <= 5:
            return f"{s[0]}***{s[-1]}"
        return f"{s[:2]}***{s[-2:]}"

    # API key (32+ alphanumeric or 40+ base64 characters)
    if _API_KEY_RE.fullmatch(s):
        return f"{s[:4]}***{s[-4:]}" if len(s) > 8 else "***"

    # IPv4 address
    if _IPV4_RE.fullmatch(s):
        parts = s.split(".")
        return f"{parts[0]}.{parts[1]}.***.***"

    # IPv6 address
    if _IPV6_RE.fullmatch(s):
        return "ipv6:***"

    # General string
    if len(s) <= 2:
        return "**"
    return f"{s[:2]}***{s[-2:]}"
