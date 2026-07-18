"""Tests for zulip.dedupe_store — message deduplication with TTL + disk."""

import json
import tempfile
import time
from pathlib import Path

import pytest

from zulip.dedupe_store import ZulipDedupeStore


class TestZulipDedupeStore:
    @pytest.fixture
    def tmp_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def store(self, tmp_data_dir):
        s = ZulipDedupeStore("test", tmp_data_dir, ttl_ms=60_000, max_size=5)
        return s

    def test_first_check_returns_false(self, store):
        assert store.check("msg_1", now=1_000) is False

    def test_second_check_returns_true(self, store):
        store.check("msg_1", now=1_000)
        assert store.check("msg_1", now=2_000) is True

    def test_ttl_expiration(self, store):
        store.check("msg_1", now=1_000)
        assert store.check("msg_1", now=62_000) is False  # expired (ttl=60s)

    def test_different_keys_independent(self, store):
        store.check("msg_1", now=1_000)
        assert store.check("msg_2", now=1_000) is False
        assert store.check("msg_1", now=2_000) is True

    def test_max_size_eviction(self, store):
        # max_size=5, add 6 entries → msg_0 should be evicted
        for i in range(6):
            store.check(f"msg_{i}", now=1_000)
        # Verify present entries first (touches keep them fresh)
        assert store.check("msg_5", now=2_000) is True
        assert store.check("msg_1", now=2_000) is True
        # msg_0 was evicted; checking it re-adds and evicts msg_2
        assert store.check("msg_0", now=2_000) is False
        assert store.check("msg_2", now=2_000) is False

    def test_touch_refreshes_lru(self, store):
        store.check("msg_1", now=1_000)
        store.check("msg_2", now=2_000)
        store.check("msg_3", now=3_000)
        # Touch msg_1 again, making it most recent
        store.check("msg_1", now=4_000)
        # Fill to max_size=5
        store.check("msg_4", now=5_000)
        store.check("msg_5", now=6_000)
        # msg_2 is oldest; add one more to trigger eviction
        store.check("msg_6", now=6_000)
        # msg_2 should be evicted
        assert store.check("msg_1", now=7_000) is True  # still fresh
        assert store.check("msg_2", now=7_000) is False  # evicted

    def test_load_from_disk(self, tmp_data_dir):
        # Pre-seed a dedupe file
        path = Path(tmp_data_dir) / "zulip_dedupe_test.json"
        with open(path, "w") as f:
            json.dump([["msg_1", 1_000], ["msg_2", 2_000]], f)

        store = ZulipDedupeStore("test", tmp_data_dir, ttl_ms=60_000, max_size=5)
        store.load()
        assert store.check("msg_1", now=3_000) is True
        assert store.check("msg_2", now=3_000) is True
        assert store.check("msg_3", now=3_000) is False

    def test_invalid_json_ignored(self, tmp_data_dir):
        path = Path(tmp_data_dir) / "zulip_dedupe_test.json"
        with open(path, "w") as f:
            f.write("not json")

        store = ZulipDedupeStore("test", tmp_data_dir, ttl_ms=60_000, max_size=5)
        store.load()
        assert store.check("msg_1", now=1_000) is False

    def test_save_and_reload(self, store):
        store.check("msg_1", now=1_000)
        store.save()

        store2 = ZulipDedupeStore("test", store._data_dir, ttl_ms=60_000, max_size=5)
        store2.load()
        assert store2.check("msg_1", now=2_000) is True

    def test_empty_key_returns_false(self, store):
        assert store.check("", now=1_000) is False

    def test_flush_immediate(self, store):
        store.check("msg_1", now=1_000)
        store.flush()
        path = store._persistence_path()
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert any(k == "msg_1" for k, _ in data)

    def test_account_id_sanitization(self, tmp_data_dir):
        store = ZulipDedupeStore("ac:me@test.com", tmp_data_dir, ttl_ms=60_000, max_size=5)
        path = store._persistence_path()
        assert "ac_me_test_com" in path.name
        assert ":" not in path.name

    def test_size_method(self, store):
        assert store.size() == 0
        store.check("msg_1", now=1_000)
        assert store.size() == 1
        store.check("msg_2", now=2_000)
        assert store.size() == 2

    def test_negative_ttl_no_expiration(self, tmp_data_dir):
        store = ZulipDedupeStore("test", tmp_data_dir, ttl_ms=-1, max_size=5)
        store.check("msg_1", now=1_000)
        # Very far in the future, should still be present
        assert store.check("msg_1", now=999_999_999_000) is True

    def test_debounced_save(self, store):
        # Multiple rapid checks should coalesce into one save
        store.check("msg_1", now=1_000)
        store.check("msg_2", now=2_000)
        store.check("msg_3", now=3_000)
        # Before flush, only temp file might exist if any
        store.flush()
        path = store._persistence_path()
        assert path.exists()
