"""Persisted Zulip display-name cache (issue #152).

Zulip event payloads do not always carry a usable display name, so any feature
that renders a human-readable name either resolves it per message — an extra
API call on the hot path — or falls back to an id. This module keeps a small
``user_id → display_name`` map in memory, persisted at
``{data_dir}/cache/zulip_display_names.json``, with a TTL and a size bound so
names resolve cheaply and survive restarts.

Persistence mirrors the other stores (``dedupe_store``, ``policy``): an atomic
temp-file write followed by ``os.replace`` and an owner-only ``0600`` chmod. A
missing, corrupt, or unreadable file is never fatal — the cache simply starts
empty.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 1 day
DEFAULT_MAX_SIZE = 1000


class DisplayNameCache:
    """``user_id → display_name`` with TTL, bounded size, and disk persistence."""

    def __init__(
        self,
        data_dir: Union[str, os.PathLike],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_size: int = DEFAULT_MAX_SIZE,
    ):
        self._data_dir = Path(data_dir).expanduser()
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        # user_id (str) → (name, stored_at epoch seconds)
        self._entries: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        self.load()

    @property
    def path(self) -> Path:
        """Location of the persisted cache file."""
        return self._data_dir / "cache" / "zulip_display_names.json"

    def load(self) -> None:
        """Load the cache from disk. A missing/corrupt/unreadable file is ignored."""
        path = self.path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("zulip: unreadable display-name cache, starting fresh: %s", e)
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            logger.warning("zulip: corrupt display-name cache, starting fresh: %s", e)
            return
        if not isinstance(data, dict):
            return
        stored = data.get("display_names")
        if not isinstance(stored, dict):
            return
        entries: dict[str, tuple[str, float]] = {}
        for key, value in stored.items():
            if not isinstance(value, dict):
                continue
            name = value.get("name")
            stored_at = value.get("stored_at")
            if isinstance(name, str) and name and isinstance(stored_at, (int, float)):
                entries[str(key)] = (name, float(stored_at))
        with self._lock:
            self._entries = entries
            self._prune_locked(time.time())

    def save(self) -> None:
        """Persist the cache atomically with owner-only (0600) permissions."""
        path = self.path
        with self._lock:
            stored = {
                key: {"name": name, "stored_at": stored_at}
                for key, (name, stored_at) in self._entries.items()
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                suffix=".tmp",
                delete=False,
            ) as fh:
                json.dump({"version": 1, "display_names": stored}, fh)
                temp_path = fh.name
            os.replace(temp_path, path)
            # Restrict file permissions to owner-only (0600)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            logger.warning("zulip: display-name cache save failed: %s", e)

    def get(self, user_id: Union[str, int], now: Optional[float] = None) -> Optional[str]:
        """Return the cached display name, or None on a miss or expired entry."""
        key = self._key(user_id)
        if key is None:
            return None
        now = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        name, stored_at = entry
        if self._expired(stored_at, now):
            return None
        return name

    def put(
        self,
        user_id: Union[str, int],
        name: str,
        now: Optional[float] = None,
    ) -> None:
        """Store a display name, pruning the cache and persisting it."""
        key = self._key(user_id)
        if key is None or not name:
            return
        now = time.time() if now is None else now
        with self._lock:
            self._entries[key] = (str(name), now)
            self._prune_locked(now)
        self.save()

    async def get_or_fetch(
        self,
        user_id: Union[str, int],
        fetch: Callable[[Union[str, int]], object],
    ) -> Optional[str]:
        """Return the cached name, refreshing it via ``fetch`` on a miss.

        ``fetch`` is called with ``user_id`` and may be a regular or async
        callable returning the display name (or a falsey value when there is
        none). A fetched name is cached; a falsey result is not.
        """
        cached = self.get(user_id)
        if cached is not None:
            return cached
        result = fetch(user_id)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            return None
        name = str(result)
        self.put(user_id, name)
        return name

    def size(self) -> int:
        """Return the number of cached entries."""
        with self._lock:
            return len(self._entries)

    @staticmethod
    def _key(user_id: Union[str, int]) -> Optional[str]:
        if user_id is None:
            return None
        key = str(user_id)
        return key or None

    def _expired(self, stored_at: float, now: float) -> bool:
        # ttl_seconds <= 0 disables expiry
        return self.ttl_seconds > 0 and (now - stored_at) >= self.ttl_seconds

    def _prune_locked(self, now: float) -> None:
        """Drop expired entries, then enforce max_size (oldest first)."""
        if self.ttl_seconds > 0:
            expired = [k for k, (_, at) in self._entries.items() if self._expired(at, now)]
            for key in expired:
                del self._entries[key]
        if self.max_size > 0:
            while len(self._entries) > self.max_size:
                oldest = min(self._entries, key=lambda k: self._entries[k][1])
                del self._entries[oldest]
