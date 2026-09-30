"""Sticky topic engagement for Zulip stream conversations (issues #165, #166).

After a user @mentions the bot (or triggers it via a ``onchar`` prefix) in a
stream topic, later messages in that same topic are accepted without another
mention until an idle TTL lapses or the user explicitly stops. The conversation
unit is Zulip's ``(stream_id, topic)`` pair, mirroring Slack/Discord thread
engagement.

Engagement is **off by default**. DMs never participate — the adapter only runs
this gate for ``stream`` messages — and the ``onmessage`` chat mode, which
already answers everything, is deliberately left out so an idle topic there can
never produce a spurious expiry notice.

State lives in :class:`TopicEngagementStore`, an in-memory, per-gateway-process
map. Keys embed the stream id, topic and (for ``user`` scope) the email, so an
engagement can never leak across topics or streams.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Modes
MODE_OFF = "off"
MODE_STICKY_TOPIC = "sticky_topic"
_VALID_MODES = frozenset({MODE_OFF, MODE_STICKY_TOPIC})

# Scope: who may continue an engaged topic without a fresh @mention.
SCOPE_USER = "user"  # only the user who opened the engagement
SCOPE_TOPIC = "topic"  # anyone posting in the engaged topic
_VALID_SCOPES = frozenset({SCOPE_USER, SCOPE_TOPIC})

DEFAULT_TTL_MINUTES = 45
DEFAULT_EXPIRY_SCAN_SECONDS = 30

# Explicit stop phrases (whole-message, case-insensitive). The bare "/stop"
# command is reserved by the Hermes gateway for interrupting the current run, so
# engagement uses its own phrasings rather than claiming it.
_STOP_COMMANDS = frozenset(
    {
        "stop listening",
        "stop listen",
        "/unlisten",
        "/stop-listening",
        "/stop_listening",
        "unlisten",
        "please stop listening",
    }
)

# Session enders that should also drop engagement (/new is the gateway's own
# session reset; the bare words are accepted the same way the gateway does).
_END_SESSION_COMMANDS = frozenset({"/new", "/reset", "new", "reset"})


def _env_truthy(name: str, default: str = "true") -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw not in ("false", "0", "", "no", "off")


@dataclass
class EngagementConfig:
    """Runtime engagement settings read from the environment."""

    mode: str = MODE_OFF
    scope: str = SCOPE_USER
    ttl_seconds: float = DEFAULT_TTL_MINUTES * 60
    expiry_notice: bool = True
    expiry_scan_seconds: float = DEFAULT_EXPIRY_SCAN_SECONDS

    @classmethod
    def from_env(cls) -> "EngagementConfig":
        mode = os.getenv("ZULIP_ENGAGEMENT_MODE", MODE_OFF).strip().lower()
        if mode not in _VALID_MODES:
            logger.warning(
                "ZULIP_ENGAGEMENT_MODE=%r is not one of %s; disabling engagement",
                mode,
                sorted(_VALID_MODES),
            )
            mode = MODE_OFF

        scope = os.getenv("ZULIP_ENGAGEMENT_SCOPE", SCOPE_USER).strip().lower()
        if scope not in _VALID_SCOPES:
            logger.warning(
                "ZULIP_ENGAGEMENT_SCOPE=%r is not one of %s; falling back to %r",
                scope,
                sorted(_VALID_SCOPES),
                SCOPE_USER,
            )
            scope = SCOPE_USER

        ttl_min = cls._positive_float(
            "ZULIP_ENGAGEMENT_TTL_MINUTES", DEFAULT_TTL_MINUTES
        )
        # A scan interval under 5s would just burn CPU; treat it as unset.
        scan_seconds = cls._positive_float(
            "ZULIP_ENGAGEMENT_EXPIRY_SCAN_SECONDS", DEFAULT_EXPIRY_SCAN_SECONDS
        )
        if scan_seconds < 5:
            scan_seconds = DEFAULT_EXPIRY_SCAN_SECONDS

        return cls(
            mode=mode,
            scope=scope,
            ttl_seconds=ttl_min * 60.0,
            expiry_notice=_env_truthy("ZULIP_ENGAGEMENT_EXPIRY_NOTICE", "true"),
            expiry_scan_seconds=scan_seconds,
        )

    @staticmethod
    def _positive_float(name: str, default: float) -> float:
        raw = os.getenv(name, str(default)).strip()
        try:
            value = float(raw)
        except ValueError:
            logger.warning("%s=%r is not a number; using %s", name, raw, default)
            return float(default)
        if value <= 0:
            logger.warning("%s=%r must be positive; using %s", name, raw, default)
            return float(default)
        return value


@dataclass
class EngagementEntry:
    """One active engagement on a stream topic."""

    stream_id: str
    topic: str
    user_email: str  # lowercase; who opened the engagement
    last_active: float
    opened_at: float
    user_name: str = ""  # display name, for the expiry notice


class TopicEngagementStore:
    """In-memory sticky engagement state (per gateway process).

    Keys are NUL-delimited so they cannot collide: ``stream_id\\0topic`` for
    ``topic`` scope and ``stream_id\\0topic\\0user_email`` for ``user`` scope.
    The topic is kept verbatim (Zulip topics are case-sensitive) and the email
    lowercased, so a key identifies exactly one topic on exactly one stream.
    """

    def __init__(self, config: Optional[EngagementConfig] = None):
        self.config = config or EngagementConfig.from_env()
        self._entries: dict[str, EngagementEntry] = {}

    def _topic_key(self, stream_id: object, topic: str) -> str:
        return f"{stream_id}\0{topic or ''}"

    def _user_key(self, stream_id: object, topic: str, user_email: str) -> str:
        return f"{self._topic_key(stream_id, topic)}\0{(user_email or '').strip().lower()}"

    def _storage_key(self, stream_id: object, topic: str, user_email: str) -> str:
        if self.config.scope == SCOPE_TOPIC:
            return self._topic_key(stream_id, topic)
        return self._user_key(stream_id, topic, user_email)

    def _is_expired(self, entry: EngagementEntry, now: float) -> bool:
        return (now - entry.last_active) > self.config.ttl_seconds

    def is_engaged(
        self,
        stream_id: object,
        topic: str,
        user_email: str,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """Return True if engagement is active for this topic (and user).

        An expired entry is reported as not engaged but left in place so the
        background scanner can still post an expiry notice.
        """
        if self.config.mode == MODE_OFF:
            return False
        now = now if now is not None else time.time()
        entry = self._entries.get(self._storage_key(stream_id, topic, user_email))
        if entry is None or self._is_expired(entry, now):
            return False
        if self.config.scope == SCOPE_USER:
            return entry.user_email == (user_email or "").strip().lower()
        return True

    def mark_engaged(
        self,
        stream_id: object,
        topic: str,
        user_email: str,
        *,
        user_name: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Open an engagement, or refresh the idle TTL of an existing one."""
        if self.config.mode == MODE_OFF:
            return
        now = now if now is not None else time.time()
        email = (user_email or "").strip().lower()
        name = (user_name or "").strip()
        key = self._storage_key(stream_id, topic, email)
        existing = self._entries.get(key)
        if existing is not None:
            existing.last_active = now
            if name:
                existing.user_name = name
            logger.debug(
                "zulip engagement refreshed [stream=%s topic=%s user=%s scope=%s]",
                stream_id,
                topic,
                email,
                self.config.scope,
            )
            return
        self._entries[key] = EngagementEntry(
            stream_id=str(stream_id),
            topic=(topic or ""),
            user_email=email,
            last_active=now,
            opened_at=now,
            user_name=name,
        )
        logger.info(
            "zulip engagement started [stream=%s topic=%s user=%s scope=%s]",
            stream_id,
            topic,
            email,
            self.config.scope,
        )

    def touch(
        self,
        stream_id: object,
        topic: str,
        user_email: str,
        *,
        now: Optional[float] = None,
    ) -> None:
        """Refresh the idle TTL without otherwise changing the entry."""
        if self.config.mode == MODE_OFF:
            return
        now = now if now is not None else time.time()
        entry = self._entries.get(self._storage_key(stream_id, topic, user_email))
        if entry is not None and not self._is_expired(entry, now):
            entry.last_active = now

    def clear(
        self,
        stream_id: object,
        topic: str,
        user_email: Optional[str] = None,
    ) -> bool:
        """Drop engagement for this topic, silently (no expiry notice).

        In ``user`` scope only ``user_email``'s entry is removed; in ``topic``
        scope — or when no email is supplied — every entry for the topic is
        removed, including any user-scoped keys left over from a scope change.
        Returns True if anything was removed.
        """
        if self.config.mode == MODE_OFF:
            return False
        removed = False
        topic_key = self._topic_key(stream_id, topic)
        if self.config.scope == SCOPE_TOPIC or user_email is None:
            prefix = topic_key + "\0"
            for key in list(self._entries):
                if key == topic_key or key.startswith(prefix):
                    del self._entries[key]
                    removed = True
        else:
            key = self._user_key(stream_id, topic, user_email)
            if key in self._entries:
                del self._entries[key]
                removed = True
        if removed:
            logger.info(
                "zulip engagement cleared [stream=%s topic=%s user=%s scope=%s]",
                stream_id,
                topic,
                user_email,
                self.config.scope,
            )
        return removed

    def pop_expired(self, *, now: Optional[float] = None) -> list[EngagementEntry]:
        """Remove and return every TTL-expired engagement (for notices)."""
        now = now if now is not None else time.time()
        expired: list[EngagementEntry] = []
        for key, entry in list(self._entries.items()):
            if self._is_expired(entry, now):
                del self._entries[key]
                expired.append(entry)
                logger.info(
                    "zulip engagement expired [stream=%s topic=%s user=%s scope=%s]",
                    entry.stream_id,
                    entry.topic,
                    entry.user_email,
                    self.config.scope,
                )
        return expired

    def active_count(self, *, now: Optional[float] = None) -> int:
        """Number of engagements that have not yet expired."""
        now = now if now is not None else time.time()
        return sum(1 for e in self._entries.values() if not self._is_expired(e, now))


def format_expiry_notice_text(*, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> str:
    """Short in-topic notice posted when sticky engagement idle-expires."""
    ttl_min = max(1, int(round(ttl_minutes)))
    return (
        f"⏲️ Engagement expired — mention me again to continue "
        f"(I stopped auto-listening after {ttl_min}m of quiet)."
    )


def is_stop_listening_message(content: str) -> bool:
    """True if the user is asking the bot to stop listening on this topic.

    Bare ``stop`` / ``/stop`` is intentionally *not* matched: the gateway
    reserves it for interrupting a run.
    """
    text = (content or "").strip().lower()
    if not text:
        return False
    if text in _STOP_COMMANDS:
        return True
    if text.startswith(("/unlisten", "/stop-listening", "/stop_listening")):
        return True
    # Natural phrase — "listening" is required so bare "stop" is not claimed.
    return bool(re.fullmatch(r"(please\s+)?stop\s+listening[.!]*", text))


def is_end_session_message(content: str) -> bool:
    """True for /new or /reset style session enders that should clear engagement."""
    text = (content or "").strip().lower()
    if not text:
        return False
    first = text.split(maxsplit=1)[0]
    return first in _END_SESSION_COMMANDS or text in _END_SESSION_COMMANDS
