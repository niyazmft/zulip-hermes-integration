"""Tests for zulip.display_names — persisted, bounded display-name cache."""

import json
import stat
import tempfile
from pathlib import Path

import pytest

from zulip.display_names import DisplayNameCache


class TestDisplayNameCache:
    @pytest.fixture
    def tmp_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def cache(self, tmp_data_dir):
        return DisplayNameCache(tmp_data_dir, ttl_seconds=3600, max_size=5)

    def test_get_miss_returns_none(self, cache):
        assert cache.get("42") is None

    def test_put_then_get_hit(self, cache):
        cache.put("42", "Alice")
        assert cache.get("42") == "Alice"

    def test_path_under_cache_dir(self, cache, tmp_data_dir):
        assert cache.path == Path(tmp_data_dir) / "cache" / "zulip_display_names.json"

    def test_persisted_and_reloaded(self, tmp_data_dir):
        first = DisplayNameCache(tmp_data_dir, ttl_seconds=3600, max_size=5)
        first.put("42", "Alice")
        second = DisplayNameCache(tmp_data_dir, ttl_seconds=3600, max_size=5)
        assert second.get("42") == "Alice"

    def test_integer_user_id_coerced(self, cache):
        cache.put(42, "Alice")
        assert cache.get(42) == "Alice"
        assert cache.get("42") == "Alice"

    def test_ttl_expiry(self, cache):
        cache.put("42", "Alice", now=1000.0)
        assert cache.get("42", now=1000.0) == "Alice"
        assert cache.get("42", now=4601.0) is None  # ttl=3600 elapsed

    def test_ttl_disabled_when_zero(self, tmp_data_dir):
        cache = DisplayNameCache(tmp_data_dir, ttl_seconds=0, max_size=5)
        cache.put("42", "Alice", now=1000.0)
        assert cache.get("42", now=10_000_000.0) == "Alice"

    def test_max_size_eviction(self, tmp_data_dir):
        cache = DisplayNameCache(tmp_data_dir, ttl_seconds=0, max_size=2)
        cache.put("1", "One", now=1.0)
        cache.put("2", "Two", now=2.0)
        cache.put("3", "Three", now=3.0)
        assert cache.size() == 2
        assert cache.get("1") is None  # oldest evicted
        assert cache.get("2") == "Two"
        assert cache.get("3") == "Three"

    def test_expired_entries_pruned_on_load(self, tmp_data_dir):
        first = DisplayNameCache(tmp_data_dir, ttl_seconds=1, max_size=5)
        first.put("42", "Alice", now=1000.0)
        second = DisplayNameCache(tmp_data_dir, ttl_seconds=1, max_size=5)
        # stored_at is far in the past relative to real clock → pruned
        assert second.get("42") is None
        assert second.size() == 0

    def test_file_written_0600(self, cache):
        cache.put("42", "Alice")
        mode = stat.S_IMODE(cache.path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_corrupt_file_non_fatal(self, tmp_data_dir):
        path = Path(tmp_data_dir) / "cache" / "zulip_display_names.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        # Must not raise on construction.
        cache = DisplayNameCache(tmp_data_dir, ttl_seconds=3600, max_size=5)
        assert cache.get("42") is None
        # A subsequent put heals the file.
        cache.put("42", "Alice")
        assert cache.get("42") == "Alice"

    def test_non_dict_payload_non_fatal(self, tmp_data_dir):
        path = Path(tmp_data_dir) / "cache" / "zulip_display_names.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        cache = DisplayNameCache(tmp_data_dir, ttl_seconds=3600, max_size=5)
        assert cache.get("42") is None

    def test_unreadable_path_non_fatal(self, tmp_data_dir):
        # A directory where the file should be triggers OSError on read.
        path = Path(tmp_data_dir) / "cache" / "zulip_display_names.json"
        path.mkdir(parents=True)
        cache = DisplayNameCache(tmp_data_dir, ttl_seconds=3600, max_size=5)
        assert cache.get("42") is None

    @pytest.mark.asyncio
    async def test_get_or_fetch_miss_then_cache(self, cache):
        calls = []

        async def fetch(user_id):
            calls.append(user_id)
            return "Alice"

        assert await cache.get_or_fetch("42", fetch) == "Alice"
        assert calls == ["42"]
        # Second call is a hit; fetch is not invoked again.
        assert await cache.get_or_fetch("42", fetch) == "Alice"
        assert calls == ["42"]

    @pytest.mark.asyncio
    async def test_get_or_fetch_empty_result_not_cached(self, cache):
        async def fetch(user_id):
            return None

        assert await cache.get_or_fetch("42", fetch) is None
        assert cache.get("42") is None

    @pytest.mark.asyncio
    async def test_get_or_fetch_supports_sync_fetch(self, cache):
        def fetch(user_id):
            return "Bob"

        assert await cache.get_or_fetch("7", fetch) == "Bob"
        assert cache.get("7") == "Bob"

    @pytest.mark.asyncio
    async def test_get_or_fetch_refreshes_expired_entry(self, tmp_data_dir):
        cache = DisplayNameCache(tmp_data_dir, ttl_seconds=1, max_size=5)
        cache.put("42", "Old", now=1000.0)
        assert cache.get("42", now=4600.0) is None

        async def fetch(user_id):
            return "New"

        assert await cache.get_or_fetch("42", fetch) == "New"
