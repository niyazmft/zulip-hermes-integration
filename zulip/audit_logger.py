"""Persistent audit logger for security-relevant events.

Writes JSON-line events to a rotating log file under the plugin's
data directory. Each event is a single JSON object with a timestamp,
event type, and metadata.

Log rotation: when the active file exceeds MAX_FILE_SIZE, it is
renamed with a timestamp suffix and a new file is started. Old
rotated files beyond MAX_ROTATED_FILES are pruned.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .logger import mask_pii

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB
MAX_ROTATED_FILES = 3


def _delivery_details(
    chat_id: str,
    topic: Optional[str] = None,
    message_id: Optional[Any] = None,
) -> dict[str, Any]:
    """Shared details for the delivery-outcome events.

    Only identifiers are accepted here: there is no parameter for a message
    body, so one cannot reach the log by accident.
    """
    details: dict[str, Any] = {"chat_id": chat_id}
    if topic is not None:
        details["topic"] = topic
    if message_id is not None:
        details["message_id"] = message_id
    return details


class AuditLogger:
    """File-based audit logger for security events.

    Events are serialized as JSON lines and appended atomically.
    Writes are serialized via an internal queue to preserve ordering.
    """

    def __init__(self, data_dir: str, account_id: str = "default"):
        self._log_dir = Path(data_dir).expanduser() / "audit"
        self._log_path = self._log_dir / f"{account_id}.audit.log"
        self._account_id = account_id
        self._write_queue: asyncio_lock = None  # type: ignore
        self._init_lock()

    def _init_lock(self) -> None:
        """Initialize the async write lock."""
        import asyncio
        self._write_queue = asyncio.locks.Lock()

    def _ensure_dir(self) -> None:
        """Ensure the log directory exists."""
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _rotate_if_needed(self) -> None:
        """Rotate the log file if it exceeds the maximum size."""
        try:
            stat = self._log_path.stat()
            if stat.st_size < MAX_FILE_SIZE:
                return
        except FileNotFoundError:
            return

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        rotated_path = self._log_path.with_suffix(f".audit.log.{timestamp}")

        try:
            self._log_path.rename(rotated_path)
        except OSError:
            return

        # Prune old rotated files
        try:
            files = sorted(
                f for f in self._log_dir.iterdir()
                if f.name.startswith(self._log_path.name) and f != self._log_path
            )
            for old_file in files[:-MAX_ROTATED_FILES]:
                old_file.unlink(missing_ok=True)
        except OSError:
            pass

    async def log(self, event: dict[str, Any]) -> None:
        """Write an audit event to the log file.

        Events are serialized as JSON lines and appended atomically.
        Writes are serialized to preserve ordering.
        """
        if self._write_queue is None:
            self._init_lock()

        async with self._write_queue:
            try:
                self._ensure_dir()
                self._rotate_if_needed()
                line = json.dumps(event, default=str) + "\n"
                # Atomic append via tempfile + rename
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(self._log_dir),
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    f.write(line)
                    temp_path = f.name
                # Append to the real log file
                with open(self._log_path, "a", encoding="utf-8") as f:
                    with open(temp_path, "r", encoding="utf-8") as tmp:
                        f.write(tmp.read())
                Path(temp_path).unlink(missing_ok=True)
            except OSError as e:
                logger.warning("audit log write failed: %s", e)

    async def log_event(
        self,
        event_type: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Convenience: log a typed event with optional details."""
        event: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "event": event_type,
            "account_id": self._account_id,
        }
        if details:
            event["details"] = details
        await self.log(event)

    async def log_monitor_start(self) -> None:
        await self.log_event("monitor_start")

    async def log_monitor_stop(self, reason: str = "finished") -> None:
        await self.log_event("monitor_stop", {"reason": reason})

    async def log_auth_failure(self, error: str) -> None:
        await self.log_event("auth_failure", {"error": error})

    async def log_rate_limit_exceeded(
        self, sender_id: str, limit: int
    ) -> None:
        await self.log_event(
            "rate_limit_exceeded",
            {"sender_id": sender_id, "limit": limit},
        )

    async def log_policy_block(
        self, sender_id: str, reason: str, kind: str
    ) -> None:
        await self.log_event(
            "policy_block",
            {"sender_id": sender_id, "reason": reason, "kind": kind},
        )

    # --- Delivery outcomes (issue #145) ---
    #
    # One event per outbound reply, at the boundary where it is handed to
    # Zulip or dropped, so "the bot didn't answer" is a lookup rather than a
    # report. ``dispatch_turn`` pairs with the ``deliver_*`` events: together
    # they tell "ran and had nothing to send" apart from "never ran".
    #
    # None of these take a message body, and the values they do take are
    # written exactly as passed — callers mask anything sensitive with
    # ``mask_pii`` before calling, as with the other typed helpers.

    async def log_dispatch_turn(
        self,
        chat_id: str,
        topic: Optional[str] = None,
        message_id: Optional[Any] = None,
    ) -> None:
        """Record that a turn was dispatched to the agent."""
        await self._log_delivery_event(
            "dispatch_turn", _delivery_details(chat_id, topic, message_id)
        )

    async def log_deliver_payload(
        self,
        chat_id: str,
        topic: Optional[str] = None,
        message_id: Optional[Any] = None,
    ) -> None:
        """Record that a reply was handed to Zulip."""
        await self._log_delivery_event(
            "deliver_payload", _delivery_details(chat_id, topic, message_id)
        )

    async def log_deliver_skipped(
        self, reason: str, chat_id: str, topic: Optional[str] = None
    ) -> None:
        """Record a reply that was deliberately not delivered.

        ``reason`` is required and must be a machine-readable token (for
        example ``"no_trigger"``): a skip with no reason is exactly the silent
        drop this event exists to make findable.
        """
        if not reason or not str(reason).strip():
            raise ValueError(
                "deliver_skipped requires a machine-readable reason"
            )
        details = _delivery_details(chat_id, topic)
        details["reason"] = reason
        await self._log_delivery_event("deliver_skipped", details)

    async def log_deliver_empty(
        self, chat_id: str, topic: Optional[str] = None
    ) -> None:
        """Record a run that finished with nothing to deliver."""
        await self._log_delivery_event(
            "deliver_empty", _delivery_details(chat_id, topic)
        )

    async def log_deliver_failed(
        self, error: str, chat_id: str, topic: Optional[str] = None
    ) -> None:
        """Record a send that raised or timed out."""
        details = _delivery_details(chat_id, topic)
        details["error"] = error
        await self._log_delivery_event("deliver_failed", details)

    async def log_approval_outcome(
        self,
        *,
        choice: str,
        decider: str,
        request_id: str = "",
        session_key: str = "",
        pattern_key: str = "",
        cancelled: Optional[str] = None,
        chat_id: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> None:
        """Record how an exec approval resolved (issue #222).

        One event per resolved approval, so "who allowed this, and did anyone?"
        is a lookup rather than a guess: ``choice`` is the host's own token
        (``once`` / ``session`` / ``always`` / ``deny`` / ``timeout`` /
        ``cancelled`` / …), ``decider`` is the human address (masked by the
        caller) or a machine token (``timeout`` / ``policy`` / ``cancelled`` /
        ``undelivered`` / ``unknown``), and ``request_id`` ties the entry to the
        prompt the plugin minted an id for.

        A failed write is logged and dropped, never raised: the approval has
        already resolved by the time this runs, and an audit must not change
        what happened.
        """
        details: dict[str, Any] = {"choice": choice, "decider": decider}
        if request_id:
            details["request_id"] = request_id
        if session_key:
            details["session_key"] = session_key
        if pattern_key:
            details["pattern_key"] = pattern_key
        if cancelled:
            details["cancelled"] = cancelled
        if chat_id:
            details["chat_id"] = chat_id
        if topic is not None:
            details["topic"] = topic
        await self._log_delivery_event("approval_outcome", details)

    async def log_approval_rejected(
        self,
        *,
        reason: str,
        decider: str,
        request_id: str = "",
        session_key: str = "",
        chat_id: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> None:
        """Record a decision that was refused (#228).

        A refused decision is not a decision: the gateway never sees it, the
        prompt stays open, and #222's unanswered default still applies. The entry
        exists so the attempt is a lookup rather than a guess — ``decider`` is the
        person who clicked (masked by the caller), and ``reason`` says which rule
        fired (``not_owner`` / ``no_owner``).
        """
        details: dict[str, Any] = {"reason": reason, "decider": decider}
        if request_id:
            details["request_id"] = request_id
        if session_key:
            details["session_key"] = session_key
        if chat_id:
            details["chat_id"] = chat_id
        if topic is not None:
            details["topic"] = topic
        await self._log_delivery_event("approval_rejected", details)

    async def _log_delivery_event(
        self, event_type: str, details: dict[str, Any]
    ) -> None:
        """Write a delivery event, reporting a failed write rather than dropping it.

        Auditing must not change whether a reply reached the room, so a failed
        write is logged instead of propagated — but never swallowed, because a
        silent failure here is how an undelivered reply goes unrecorded.
        """
        try:
            await self.log_event(event_type, details)
        except Exception as e:
            logger.warning(
                "delivery audit write failed [event=%s]: %s",
                event_type,
                mask_pii(str(e)),
            )
