"""Outbound secret guard (Issue #136).

The plugin owns the last hop before a message reaches Zulip, so this is the one
place a deterministic control can stop a credential from leaving it.

**Why it exists.** An agent that can read the host config or environment can
simply *type* a credential into chat. A path allowlist on uploads structurally
cannot cover that: nothing is uploaded. The sibling plugin shipped this guard
after a live leak, where an agent read the host config and pasted six credential
values into a Zulip DM.

**What it does not do.** It never stops a file from being *read* — that is
gateway tool policy. It stops the plugin from *transmitting* a known credential
value.

Two modes, and both are needed:

* :func:`find_leaked_secrets` — the outbound choke point (``send`` /
  ``_standalone_send``): refuse the message.
* :func:`redact_secrets` — for paths that do not go through ``send``, notably
  in-place message edits used by the activity trace (#139). Blocking there
  would leave a stale status message; redacting keeps the write useful while
  still keeping the credential out of the room.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import runtime_scope
from typing import Any, Iterable, Mapping, Optional, Sequence

# Values shorter than this are ignored, so innocuous short strings are never
# treated as credentials (and ordinary prose is never blocked).
MIN_SECRET_LENGTH = 12

# Config or environment keys whose string values are treated as credentials.
SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|apikey|token|secret|passwd|password|credential)",
    re.IGNORECASE,
)

# Depth limit for walking a config tree.
MAX_WALK_DEPTH = 6

REDACTION_MARKER = "[redacted]"

_TRUTHY_OFF = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class KnownSecret:
    """A credential value plus a *safe* name for where it came from.

    ``name`` is the only part ever logged or audited: the message describing a
    leak must not itself become one.
    """

    name: str
    value: str


def is_credential_key(key: str) -> bool:
    """True when a config/env key looks like it holds a credential."""
    return bool(SECRET_KEY_PATTERN.search(key))


def _add(by_value: dict[str, str], name: str, value: Any) -> None:
    """Record a credential value, keeping the first name that claimed it.

    The same secret in two places is reported once, under its first-seen name.
    """
    if not isinstance(value, str):
        return
    trimmed = value.strip()
    if len(trimmed) < MIN_SECRET_LENGTH:
        return
    by_value.setdefault(trimmed, name)


def collect_known_secrets(
    cfg: Any = None,
    *,
    extra: Iterable[tuple[str, Any]] = (),
    env: Optional[Mapping[str, str]] = None,
) -> list[KnownSecret]:
    """Collect credential-shaped string values from config, env and extras.

    Three sources, because they cover different leak paths:

    * ``cfg`` — a host/platform config tree, walked for credential-shaped keys.
    * ``env`` — credential-shaped environment variables. On a Hermes host this
      is where provider keys actually live, and it is what an agent reads when
      it decides to paste one into chat.
    * ``extra`` — explicit ``(name, value)`` pairs the caller knows are
      credentials even though nothing about the name says so.

    Only values that are strings, credential-shaped by key, and at least
    :data:`MIN_SECRET_LENGTH` characters long are collected.
    """
    by_value: dict[str, str] = {}

    def walk(node: Any, path_parts: list[str], depth: int) -> None:
        if depth > MAX_WALK_DEPTH or node is None:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                here = [*path_parts, str(key)]
                if isinstance(value, str) and is_credential_key(str(key)):
                    _add(by_value, ".".join(here), value)
                walk(value, here, depth + 1)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, [*path_parts, str(index)], depth + 1)

    walk(cfg, [], 0)

    for key, value in (os.environ if env is None else env).items():
        if is_credential_key(str(key)):
            _add(by_value, f"env.{key}", value)

    for name, value in extra:
        _add(by_value, name, value)

    return [KnownSecret(name=name, value=value) for value, name in by_value.items()]


def find_leaked_secrets(
    text: str, secrets: Sequence[KnownSecret] | Iterable[KnownSecret]
) -> list[KnownSecret]:
    """Return the known credentials whose value appears verbatim in ``text``."""
    if not text:
        return []
    return [secret for secret in secrets if secret.value and secret.value in text]


def describe_leaked_secrets(hits: Sequence[KnownSecret] | Iterable[KnownSecret]) -> str:
    """Human-readable summary naming *where* the credentials came from.

    Never includes a value, for the reason above.
    """
    hits = list(hits)
    names = ", ".join(hit.name for hit in hits)
    return f"{len(hits)} credential value(s) from the host config ({names})"


def redact_secrets(
    text: str, secrets: Sequence[KnownSecret] | Iterable[KnownSecret]
) -> tuple[str, int]:
    """Replace every known credential value in ``text`` with a marker.

    Defence in depth for writes that bypass :meth:`ZulipAdapter.send` — notably
    the in-place edits the activity trace uses (#139). Returns the new text and
    how many credentials were replaced.

    Longest value first, so a credential that contains another credential as a
    substring cannot leave a fragment of the longer one behind.
    """
    if not text:
        return text, 0

    result = text
    redacted = 0
    for secret in sorted(secrets, key=lambda item: len(item.value), reverse=True):
        if not secret.value or secret.value not in result:
            continue
        result = result.replace(secret.value, REDACTION_MARKER)
        redacted += 1
    return result, redacted


def block_secret_leaks_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """``ZULIP_BLOCK_SECRET_LEAKS`` — enabled unless explicitly turned off.

    Resolves through the active Hermes profile when no explicit ``env`` is
    supplied, so the flag cannot leak across profiles under multiplexing (#156).
    """
    if env is None:
        raw = runtime_scope.get_setting("ZULIP_BLOCK_SECRET_LEAKS")
    else:
        raw = env.get("ZULIP_BLOCK_SECRET_LEAKS")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in _TRUTHY_OFF
