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
from typing import Callable, Iterable, Optional

import time

logger = logging.getLogger(__name__)

# What a pre-#162 queue actually requested when ``event_types`` was first
# introduced. Legacy metadata carries no record of its requested set, so we
# assume this rather than forcing a re-registration (and a possible event gap)
# on every upgrade. (Issue #162)
LEGACY_EVENT_TYPES = ["message"]


def normalize_event_types(event_types: Optional[Iterable[str]]) -> list:
    """Return a stable, deduplicated event-type list.

    ``None`` means legacy metadata that predates event-type recording, which
    requested ``["message"]``. Any other value is ordered and deduplicated so
    comparisons do not depend on caller ordering. (Issue #162)
    """
    if event_types is None:
        return list(LEGACY_EVENT_TYPES)
    seen = set()
    result = []
    for name in event_types:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def event_types_match(
    recorded: Optional[Iterable[str]], needed: Optional[Iterable[str]]
) -> bool:
    """Whether a persisted queue's event types satisfy the needed set.

    Order-insensitive, and ``None`` on either side is normalized to the legacy
    ``["message"]`` set. A persisted queue must be re-registered when this is
    False: ``/register`` fixes ``event_types`` for the queue's whole lifetime,
    so a reused queue silently never delivers the newly-needed types. (#162)
    """
    return set(normalize_event_types(recorded)) == set(normalize_event_types(needed))


class QueueMetadata:
    """Represents persisted queue state."""

    def __init__(
        self,
        queue_id: str,
        last_event_id: int,
        registered_at: int = 0,
        event_types: Optional[Iterable[str]] = None,
    ):
        self.queue_id = queue_id
        self.last_event_id = last_event_id
        self.registered_at = registered_at or int(time.time() * 1000)
        # The set of Zulip event types this queue was registered for. Zulip
        # fixes event_types for the queue's whole lifetime, so this must be
        # persisted to know whether a reused queue can still deliver what we
        # need. (Issue #162)
        self.event_types = normalize_event_types(event_types)

    def to_dict(self) -> dict:
        return {
            "queue_id": self.queue_id,
            "last_event_id": self.last_event_id,
            "registered_at": self.registered_at,
            "event_types": list(self.event_types),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueMetadata":
        return cls(
            queue_id=data["queue_id"],
            last_event_id=data["last_event_id"],
            registered_at=data.get("registered_at", 0),
            # Missing key (legacy file) or explicit null both mean: registered
            # before we recorded event types, i.e. ["message"]. (Issue #162)
            event_types=data.get("event_types"),
        )


class ZulipQueueManager:
    """Manages Zulip event queue registration with disk persistence."""

    def __init__(
        self,
        account_id: str,
        data_dir: str,
        register_fn: Callable[[], dict],
        needed_event_types_fn: Optional[Callable[[], Iterable[str]]] = None,
    ):
        self.account_id = account_id
        self._data_dir = Path(data_dir).expanduser()
        self._register_fn = register_fn
        # Supplies the event-type set this process needs. A persisted queue is
        # only reused while it matches; otherwise we re-register so the newly
        # needed types are actually delivered. Defaults to legacy ["message"].
        # (Issue #162)
        self._needed_event_types_fn = needed_event_types_fn or (
            lambda: list(LEGACY_EVENT_TYPES)
        )
        self._current_queue: Optional[QueueMetadata] = None
        self._registration_promise: Optional[asyncio.Future] = None
        # Debounced save state
        self._dirty = False
        self._save_timer: Optional[asyncio.TimerHandle] = None
        self._debounce_delay = 5.0  # seconds (increased from 2.0 for lower write frequency)

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
            # Restrict file permissions to owner-only (0600)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            logger.error(
                "zulip queue save failed [account=%s error=%s]",
                self.account_id,
                e,
            )

    def needed_event_types(self) -> list:
        """The event-type set this process requires from its queue."""
        return normalize_event_types(self._needed_event_types_fn())

    async def ensure_queue(self) -> QueueMetadata:
        """Return existing queue or register a new one.

        Re-registers when the queue's recorded event types no longer match the
        needed set, so a queue registered for ``["message"]`` is not silently
        reused after a feature starts requesting ``"reaction"``. (Issue #162)
        """
        if self._current_queue:
            if event_types_match(
                self._current_queue.event_types, self.needed_event_types()
            ):
                return self._current_queue
            logger.info(
                "zulip queue event types changed [account=%s recorded=%s needed=%s] "
                "re-registering",
                self.account_id,
                self._current_queue.event_types,
                self.needed_event_types(),
            )
            self.mark_queue_expired()

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
        needed = self.needed_event_types()
        persisted = self.load()
        if persisted:
            if event_types_match(persisted.event_types, needed):
                return persisted
            logger.info(
                "zulip queue re-registration required [account=%s recorded=%s needed=%s]",
                self.account_id,
                persisted.event_types,
                needed,
            )
            self.mark_queue_expired()

        max_attempts = 5
        base_delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                result = self._register_fn()
                metadata = QueueMetadata(
                    queue_id=result["queue_id"],
                    last_event_id=result["last_event_id"],
                    # Record the set we just asked for. The register function
                    # requests exactly this set; if it ever drifts, the next
                    # restart's match check catches it. (Issue #162)
                    event_types=needed,
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
        """Update the last seen event ID and schedule a debounced save."""
        if self._current_queue and event_id > self._current_queue.last_event_id:
            self._current_queue.last_event_id = event_id
            self._dirty = True
            self._schedule_save()

    def _schedule_save(self) -> None:
        """Debounce disk writes to avoid I/O on every event batch."""
        if self._save_timer is not None:
            self._save_timer.cancel()
        loop = asyncio.get_event_loop()
        self._save_timer = loop.call_later(
            self._debounce_delay,
            lambda: asyncio.ensure_future(self._flush_save()),
        )

    async def _flush_save(self) -> None:
        """Flush pending save if dirty."""
        self._save_timer = None
        if self._dirty and self._current_queue:
            self._dirty = False
            self.save(self._current_queue)

    async def flush(self) -> None:
        """Flush any pending save immediately. Used during shutdown."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        if self._dirty and self._current_queue:
            self._dirty = False
            self.save(self._current_queue)

    def get_queue(self) -> Optional[QueueMetadata]:
        """Return current queue metadata without triggering registration."""
        return self._current_queue
