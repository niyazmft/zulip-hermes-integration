"""Persistent message deduplication store with TTL and LRU eviction.

Prevents duplicate message processing across gateway restarts.
Debounced disk I/O avoids synchronous writes on every message.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SAVE_DEBOUNCE_MS = 5000  # Save at most once every 5 seconds


class ZulipDedupeStore:
    """Tracks recently seen message IDs with disk persistence."""

    def __init__(
        self,
        account_id: str,
        data_dir: str,
        ttl_ms: int = 300_000,  # 5 minutes
        max_size: int = 2000,
    ):
        self.account_id = account_id
        self._data_dir = Path(data_dir).expanduser()
        self.ttl_ms = ttl_ms
        self.max_size = max_size
        self._cache: dict[str, int] = {}
        self._dirty = False
        self._save_timer: Optional[threading.Timer] = None
        self._lock = threading.RLock()

    def _persistence_path(self) -> Path:
        safe_id = "".join(c if c.isalnum() else "_" for c in self.account_id)
        return self._data_dir / f"zulip_dedupe_{safe_id}.json"

    def load(self) -> None:
        """Load persisted dedupe state from disk."""
        path = self._persistence_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            with self._lock:
                self._cache = {k: v for k, v in entries}
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass

    def save(self) -> None:
        """Persist current cache to disk atomically."""
        path = self._persistence_path()
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                entries = list(self._cache.items())
                self._dirty = False
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._data_dir),
                suffix=".tmp",
                delete=False,
            ) as f:
                json.dump(entries, f)
                temp_path = f.name
            os.replace(temp_path, path)
        except OSError as e:
            logger.error(
                "zulip dedupe save failed [account=%s error=%s]",
                self.account_id,
                e,
            )

    def _schedule_save(self) -> None:
        """Debounce disk writes to avoid I/O on every message."""
        with self._lock:
            self._dirty = True
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(SAVE_DEBOUNCE_MS / 1000, self.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def flush(self) -> None:
        """Flush any pending save immediately."""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
                self._save_timer = None
            if self._dirty:
                self.save()

    def check(self, key: str, now: Optional[int] = None) -> bool:
        """Check if key was seen recently.

        Returns True if already in cache (and not expired), False if new.
        If new, adds it to the cache.
        """
        if not key:
            return False

        now = now or int(time.time() * 1000)
        with self._lock:
            existing = self._cache.get(key)
            if existing is not None and (self.ttl_ms <= 0 or now - existing < self.ttl_ms):
                # Seen and not expired — touch to keep fresh
                self._touch(key, now)
                return True

            # New key
            self._touch(key, now)
            self._prune(now)

        # Schedule save outside the lock
        self._schedule_save()
        return False

    def _touch(self, key: str, now: int) -> None:
        """Move key to front (most recently used)."""
        self._cache.pop(key, None)
        self._cache[key] = now

    def _prune(self, now: int) -> None:
        """Remove expired entries and enforce max size."""
        # Entries are ordered by insertion/touch time (LRU at beginning)
        if self.ttl_ms > 0:
            cutoff = now - self.ttl_ms
            expired = [k for k, v in self._cache.items() if v < cutoff]
            for k in expired:
                del self._cache[k]

        if self.max_size > 0:
            while len(self._cache) > self.max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]

    def size(self) -> int:
        """Return current cache size."""
        with self._lock:
            return len(self._cache)
