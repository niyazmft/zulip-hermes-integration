"""Persistent Zulip event queue manager.

Survives gateway restarts by persisting queue_id and last_event_id to disk.
Handles BAD_EVENT_QUEUE_ID by re-registering transparently.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

import time

logger = logging.getLogger(__name__)


class QueueMetadata:
    """Represents persisted queue state."""

    def __init__(self, queue_id: str, last_event_id: int, registered_at: int = 0):
        self.queue_id = queue_id
        self.last_event_id = last_event_id
        self.registered_at = registered_at or int(time.time() * 1000)

    def to_dict(self) -> dict:
        return {
            "queue_id": self.queue_id,
            "last_event_id": self.last_event_id,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueMetadata":
        return cls(
            queue_id=data["queue_id"],
            last_event_id=data["last_event_id"],
            registered_at=data.get("registered_at", 0),
        )


class ZulipQueueManager:
    """Manages Zulip event queue registration with disk persistence."""

    def __init__(
        self,
        account_id: str,
        data_dir: str,
        register_fn: Callable[[], dict],
    ):
        self.account_id = account_id
        self._data_dir = Path(data_dir).expanduser()
        self._register_fn = register_fn
        self._current_queue: Optional[QueueMetadata] = None
        self._registration_promise: Optional[asyncio.Future] = None

    def _persistence_path(self) -> Path:
        safe_id = "".join(c if c.isalnum() else "_" for c in self.account_id)
        return self._data_dir / f"zulip_queue_{safe_id}.json"

    def load(self) -> Optional[QueueMetadata]:
        """Load queue metadata from disk. Returns None if no valid file."""
        path = self._persistence_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            metadata = QueueMetadata.from_dict(data)
            logger.info(
                "zulip queue loaded [account=%s queue_id=%s last_event_id=%d]",
                self.account_id,
                metadata.queue_id,
                metadata.last_event_id,
            )
            self._current_queue = metadata
            return metadata
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None

    def save(self, metadata: QueueMetadata) -> None:
        """Persist queue metadata atomically (write temp, then rename)."""
        path = self._persistence_path()
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._data_dir),
                suffix=".tmp",
                delete=False,
            ) as f:
                json.dump(metadata.to_dict(), f)
                temp_path = f.name
            os.replace(temp_path, path)
        except OSError as e:
            logger.error(
                "zulip queue save failed [account=%s error=%s]",
                self.account_id,
                e,
            )

    async def ensure_queue(self) -> QueueMetadata:
        """Return existing queue or register a new one."""
        if self._current_queue:
            return self._current_queue

        if self._registration_promise:
            return await self._registration_promise

        future = asyncio.get_event_loop().create_future()
        self._registration_promise = future
        try:
            metadata = await self._perform_registration()
            self._current_queue = metadata
            future.set_result(metadata)
            return metadata
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._registration_promise = None

    async def _perform_registration(self) -> QueueMetadata:
        """Attempt to load from disk, or register a new queue with retry."""
        persisted = self.load()
        if persisted:
            return persisted

        max_attempts = 5
        base_delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                result = self._register_fn()
                metadata = QueueMetadata(
                    queue_id=result["queue_id"],
                    last_event_id=result["last_event_id"],
                )
                self.save(metadata)
                logger.info(
                    "zulip queue registered [account=%s queue_id=%s last_event_id=%d]",
                    self.account_id,
                    metadata.queue_id,
                    metadata.last_event_id,
                )
                return metadata
            except Exception as e:
                logger.warning(
                    "zulip queue registration failed [account=%s attempt=%d/%d error=%s]",
                    self.account_id,
                    attempt,
                    max_attempts,
                    e,
                )
                if attempt >= max_attempts:
                    raise RuntimeError(
                        "Queue registration failed after all retries"
                    ) from e
                delay = base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        raise RuntimeError("Queue registration failed after all retries")

    def mark_queue_expired(self) -> None:
        """Clear in-memory queue and delete persisted file."""
        if self._current_queue:
            logger.info(
                "zulip queue expired [account=%s queue_id=%s]",
                self.account_id,
                self._current_queue.queue_id,
            )
        self._current_queue = None
        path = self._persistence_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def update_last_event_id(self, event_id: int) -> None:
        """Update the last seen event ID and save to disk."""
        if self._current_queue and event_id > self._current_queue.last_event_id:
            self._current_queue.last_event_id = event_id
            self.save(self._current_queue)

    def get_queue(self) -> Optional[QueueMetadata]:
        """Return current queue metadata without triggering registration."""
        return self._current_queue
