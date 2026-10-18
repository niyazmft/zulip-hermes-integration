"""#224: jittered registration, poll-loop liveness, and server backpressure.

Three gaps on the client path, all about a bot that looks healthy while it is
not: retries that line up across a restart storm, a poll loop alive but deaf,
and a rate-limit hint treated as a generic error.
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from zulip import connection, queue_manager
from zulip.queue_manager import ZulipQueueManager


class TestJitteredRegistrationDelay:
    """Retries must not line up across bots restarting together."""

    def test_the_delay_stays_inside_the_band(self):
        rng = random.Random(1234)
        for attempt in range(1, 6):
            raw = 1.0 * (2 ** (attempt - 1))
            for _ in range(20):
                delay = queue_manager.jittered_delay(attempt, 1.0, rng=rng.uniform)
                assert raw * queue_manager.REGISTRATION_JITTER_LOW <= delay <= raw

    def test_attempts_produce_different_delays(self):
        rng = random.Random(7)
        delays = {round(queue_manager.jittered_delay(3, 1.0, rng=rng.uniform), 4) for _ in range(20)}
        assert len(delays) > 1, "a fixed delay is the lockstep the jitter exists to break"

    def test_the_band_is_half_to_full(self):
        assert queue_manager.jittered_delay(1, 2.0, rng=lambda a, b: a) == 1.0
        assert queue_manager.jittered_delay(1, 2.0, rng=lambda a, b: b) == 2.0

    @pytest.mark.asyncio
    async def test_registration_retries_are_jittered_and_still_raise(self, monkeypatch, tmp_path):
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        manager = ZulipQueueManager(
            account_id="acct",
            data_dir=str(tmp_path),
            register_fn=lambda: (_ for _ in ()).throw(RuntimeError("down")),
        )

        with pytest.raises(RuntimeError, match="after all retries"):
            await manager._perform_registration()

        raw = [1.0, 2.0, 4.0, 8.0]
        assert len(sleeps) == len(raw), "four waits between five attempts"
        for delay, expected_raw in zip(sleeps, raw):
            assert expected_raw * 0.5 <= delay <= expected_raw
        assert sum(sleeps) < sum(raw) + 1e-9, "the total window must stay bounded"


class TestPollLiveness:
    """A loop alive but no longer reading is indistinguishable from an idle one."""

    def _adapter(self, elapsed):
        import time

        queue_mgr = SimpleNamespace(
            mark_queue_expired=MagicMock(),
            _current_queue=SimpleNamespace(queue_id="q-42"),
        )
        return SimpleNamespace(
            _events_timeout=60.0,
            _last_successful_read=time.monotonic() - elapsed,
            _queue_mgr=queue_mgr,
        )

    def test_a_synthetic_stall_is_detected_and_forces_re_registration(self, caplog):
        adapter = self._adapter(elapsed=connection.poll_stall_seconds(60.0) + 5)

        with caplog.at_level(logging.WARNING, logger="zulip.connection"):
            stalled = connection.check_poll_liveness(adapter)

        assert stalled is True
        adapter._queue_mgr.mark_queue_expired.assert_called_once()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "queue=" in text, "the log must name the queue it expired"
        assert "stalled" in text.lower()
        assert "q-42" not in text, "the queue id is masked like every other id"

    def test_an_idle_realm_does_not_trip_the_threshold(self):
        adapter = self._adapter(elapsed=5.0)

        assert connection.check_poll_liveness(adapter) is False
        adapter._queue_mgr.mark_queue_expired.assert_not_called()

    def test_the_threshold_is_far_above_long_poll_latency(self):
        assert connection.poll_stall_seconds(60.0) >= 180.0
        assert connection.poll_stall_seconds(90.0) > 90.0

    def test_no_reading_yet_is_not_a_stall(self):
        adapter = SimpleNamespace(_events_timeout=60.0, _last_successful_read=None,
                                  _queue_mgr=SimpleNamespace(mark_queue_expired=MagicMock()))

        assert connection.check_poll_liveness(adapter) is False


class TestBackpressure:
    """A retry hint is information; a generic error is not."""

    def test_a_retry_hint_is_used_and_clamped(self):
        assert connection.backpressure_delay({"code": "RATE_LIMIT_HIT", "retry_after": 7}) == 7.0
        assert (
            connection.backpressure_delay({"code": "RATE_LIMIT_HIT", "retry_after": 10_000})
            == connection.BACKPRESSURE_MAX_WAIT_SECONDS
        )

    def test_a_rate_limit_without_a_hint_still_waits_a_short_interval(self):
        wait = connection.backpressure_delay({"code": "RATE_LIMIT_HIT"})
        assert wait is not None and 0 < wait <= 5

    def test_an_ordinary_error_has_no_backpressure_delay(self):
        assert connection.backpressure_delay({"code": "BAD_REQUEST", "msg": "nope"}) is None
        assert connection.backpressure_delay({}) is None
        assert connection.backpressure_delay(None) is None
