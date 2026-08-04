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


@pytest.fixture
def tmp_data_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def mock_register():
    call_count = 0

    def fn():
        nonlocal call_count
        call_count += 1
        return {"queue_id": f"q{call_count}", "last_event_id": call_count * 10}

    fn.call_count = lambda: call_count
    return fn


class TestZulipQueueManager:
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


class TestDebouncedSave:
    """Tests for debounced queue event ID saves."""

    @pytest.mark.asyncio
    async def test_update_schedules_debounced_save(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        await mgr.ensure_queue()

        # Update should set dirty flag and schedule save
        mgr.update_last_event_id(42)
        assert mgr._dirty is True
        assert mgr._save_timer is not None

    @pytest.mark.asyncio
    async def test_flush_writes_to_disk(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        await mgr.ensure_queue()

        mgr.update_last_event_id(99)
        await mgr.flush()

        # Reload and verify
        mgr2 = ZulipQueueManager("test", tmp_data_dir, mock_register)
        q = await mgr2.ensure_queue()
        assert q.last_event_id == 99

    @pytest.mark.asyncio
    async def test_multiple_updates_debounced(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        await mgr.ensure_queue()

        # Multiple updates should only schedule one save
        # Use values greater than initial last_event_id (10)
        mgr.update_last_event_id(100)
        timer1 = mgr._save_timer
        mgr.update_last_event_id(200)
        timer2 = mgr._save_timer
        mgr.update_last_event_id(300)
        timer3 = mgr._save_timer

        # Each update should cancel the previous timer and create a new one
        assert timer1 is not None
        assert timer2 is not None
        assert timer3 is not None
        assert timer1 is not timer2  # Different timer handles

        await mgr.flush()
        assert mgr._current_queue.last_event_id == 300

    @pytest.mark.asyncio
    async def test_flush_without_dirty_does_nothing(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        await mgr.ensure_queue()

        # Flush without any updates should not crash
        await mgr.flush()
        assert mgr._current_queue.last_event_id == 10  # initial value from registration

    @pytest.mark.asyncio
    async def test_flush_cancels_pending_timer(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        await mgr.ensure_queue()

        mgr.update_last_event_id(50)
        assert mgr._save_timer is not None

        await mgr.flush()
        assert mgr._save_timer is None  # Timer should be cancelled
        assert mgr._dirty is False  # Should be clean after flush

    @pytest.mark.asyncio
    async def test_lower_event_id_ignored(self, tmp_data_dir, mock_register):
        mgr = ZulipQueueManager("test", tmp_data_dir, mock_register)
        await mgr.ensure_queue()

        mgr.update_last_event_id(100)
        mgr.update_last_event_id(50)  # Lower, should be ignored

        await mgr.flush()
        assert mgr._current_queue.last_event_id == 100
