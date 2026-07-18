"""Tests for zulip.queue_manager — persistent event queue manager."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zulip.queue_manager import QueueMetadata, ZulipQueueManager


class TestQueueMetadata:
    def test_roundtrip_dict(self):
        m = QueueMetadata(queue_id="q123", last_event_id=42, registered_at=1_000_000)
        d = m.to_dict()
        assert d["queue_id"] == "q123"
        assert d["last_event_id"] == 42
        assert d["registered_at"] == 1_000_000
        restored = QueueMetadata.from_dict(d)
        assert restored.queue_id == "q123"
        assert restored.last_event_id == 42
        assert restored.registered_at == 1_000_000

    def test_default_registered_at(self):
        m = QueueMetadata(queue_id="q1", last_event_id=0)
        assert m.registered_at > 0


class TestZulipQueueManager:
    @pytest.fixture
    def tmp_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def mock_register(self):
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return {"queue_id": f"q{call_count}", "last_event_id": call_count * 10}

        fn.call_count = lambda: call_count
        return fn

    def test_load_from_disk(self, tmp_data_dir, mock_register):
        # Pre-seed a queue file
        path = Path(tmp_data_dir) / "zulip_queue_test.json"
        with open(path, "w") as f:
            json.dump({"queue_id": "q_old", "last_event_id": 99, "registered_at": 1}, f)

        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        q = asyncio.run(mgr.ensure_queue())
        assert q.queue_id == "q_old"
        assert q.last_event_id == 99
        assert mock_register.call_count() == 0  # no registration needed

    def test_register_new_queue(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        q = asyncio.run(mgr.ensure_queue())
        assert q.queue_id == "q1"
        assert q.last_event_id == 10
        assert mock_register.call_count() == 1

        # File should exist
        assert (Path(tmp_data_dir) / "zulip_queue_test.json").exists()

    def test_reuse_cached_queue(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        q1 = asyncio.run(mgr.ensure_queue())
        q2 = asyncio.run(mgr.ensure_queue())
        assert q1.queue_id == q2.queue_id
        assert mock_register.call_count() == 1  # only registered once

    def test_mark_expired(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        asyncio.run(mgr.ensure_queue())
        path = Path(tmp_data_dir) / "zulip_queue_test.json"
        assert path.exists()

        mgr.mark_queue_expired()
        assert mgr.get_queue() is None
        assert not path.exists()

    def test_update_last_event_id(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        asyncio.run(mgr.ensure_queue())
        mgr.update_last_event_id(5)
        mgr.update_last_event_id(10)
        mgr.update_last_event_id(3)  # should be ignored (lower)

        # Reload from disk
        mgr2 = ZulipQueueManager("test", tmp_data_dir, mock_register)
        q = asyncio.run(mgr2.ensure_queue())
        assert q.last_event_id == 10

    def test_concurrent_ensure_queue(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)

        async def race():
            results = await asyncio.gather(
                mgr.ensure_queue(),
                mgr.ensure_queue(),
                mgr.ensure_queue(),
            )
            return results

        qs = asyncio.run(race())
        assert all(q.queue_id == qs[0].queue_id for q in qs)
        assert mock_register.call_count() == 1  # single registration despite 3 callers

    def test_retry_on_registration_failure(self, tmp_data_dir):
        attempt_count = 0

        def flaky_register():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ConnectionError("zulip down")
            return {"queue_id": "q_ok", "last_event_id": 1}

        mgr = ZulipQueueManager("test", tmp_data_dir, flaky_register)
        q = asyncio.run(mgr.ensure_queue())
        assert q.queue_id == "q_ok"
        assert attempt_count == 3

    def test_retry_exhaustion_raises(self, tmp_data_dir):
        def always_fail():
            raise ConnectionError("permanent failure")

        mgr = ZulipQueueManager("test", tmp_data_dir, always_fail)
        with pytest.raises(RuntimeError, match="Queue registration failed after all retries"):
            asyncio.run(mgr.ensure_queue())

    def test_invalid_json_file(self, tmp_data_dir, mock_register):
        path = Path(tmp_data_dir) / "zulip_queue_test.json"
        with open(path, "w") as f:
            f.write("not json")

        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        q = asyncio.run(mgr.ensure_queue())
        assert q.queue_id == "q1"  # falls back to registration

    def test_save_atomic(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        asyncio.run(mgr.ensure_queue())
        # Verify only one json file exists (no temp files left)
        files = list(Path(tmp_data_dir).glob("*.json"))
        assert len(files) == 1

    def test_account_id_sanitization(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("ac:me@test.com", tmp_data_dir, mock_register)
        path = mgr._persistence_path()
        assert "ac_me_test_com" in path.name
        assert ":" not in path.name

    def test_get_queue_without_registration(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        assert mgr.get_queue() is None
        # get_queue should not trigger registration
        assert mock_register.call_count() == 0
