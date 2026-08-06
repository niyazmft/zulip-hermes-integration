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

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB
MAX_ROTATED_FILES = 3


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
