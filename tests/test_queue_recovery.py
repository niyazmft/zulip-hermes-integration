"""Regression tests for the per-session queue wedge and its watchdog (issue #151).

Found live on a local test deployment: a single ``/status`` message wedged a whole
topic. The mechanism is that the queue's drain is driven **only** by the host's
``on_processing_complete`` hook. A turn the gateway answers itself — a native
slash command — never starts an agent run, so that hook never fires. The session
stayed marked in-flight forever and every later message queued behind a run that
had already ended (observed depths 1 -> 2 -> 3 with no further dequeue).

Two independent defences are pinned here:

* ``is_slash`` turns are never serialized at all (prevention), and
* ``release_stale`` frees a session whose run never reported completion, but only
  when turns are actually waiting behind it (recovery).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from zulip.inbound_queue import SessionQueueController
from zulip.session_queue import PendingTurn, SessionQueue, SessionQueueConfig


def _turn(message_id: str = "1") -> PendingTurn:
    """A turn carrying no live collaborators — only its identity matters here."""
    return PendingTurn(
        event=None, reactions=None, typing_params=None, message_id=message_id
    )


class _AuditLogger:
    """Stands in for the audit logger the queue records transitions through."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def log_event(self, event_type: str, details: Any = None, **kwargs) -> None:
        self.events.append(event_type)


class _Recorder:
    """Stands in for a ReactionLifecycle; records the calls the queue makes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def queued(self) -> None:
        self.calls.append("queued")

    async def unqueued(self) -> None:
        self.calls.append("unqueued")


class TestSlashTurnsAreNotSerialized:
    def test_slash_turn_is_dispatched_immediately_even_when_session_is_active(self):
        """The exact live failure: an active session must not hold a command."""
        queue = SessionQueue(SessionQueueConfig(enabled=True))
        queue.mark_active("route:573422\x00general chat")
        controller = SessionQueueController(SimpleNamespace(_session_queue=queue))

        queued = asyncio.run(
            controller._queue_turn(
                SimpleNamespace(), _Recorder(), None, "1", is_slash=True
            )
        )

        assert queued is False, "a slash command must never be queued"
        assert queue.depth("route:573422\x00general chat") == 0

    def test_non_slash_turn_still_queues_behind_an_active_session(self):
        """The guard must not disable queueing for ordinary messages."""
        queue = SessionQueue(SessionQueueConfig(enabled=True))
        queue.mark_active("route:573422\x00general chat")
        adapter = SimpleNamespace(
            _session_queue=queue,
            _session_key_for_event=lambda event: "route:573422\x00general chat",
            _audit_logger=_AuditLogger(),
        )
        controller = SessionQueueController(adapter)
        reactions = _Recorder()

        queued = asyncio.run(
            controller._queue_turn(SimpleNamespace(), reactions, None, "1")
        )

        assert queued is True
        assert queue.depth("route:573422\x00general chat") == 1
        assert reactions.calls == ["queued"]


class TestStaleSessionRecovery:
    def test_release_stale_frees_a_wedged_session_that_has_turns_waiting(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True, max_active_seconds=100))
        queue.mark_active("route:1\x00t")
        queue.enqueue("route:1\x00t", _turn())

        released = queue.release_stale(now=time.monotonic() + 101)

        assert released == ["route:1\x00t"]
        assert queue.is_active("route:1\x00t") is False
        # The waiting turn is still there for the caller to drain.
        assert queue.depth("route:1\x00t") == 1

    def test_release_stale_leaves_a_healthy_in_flight_run_alone(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True, max_active_seconds=1800))
        queue.mark_active("route:1\x00t")
        queue.enqueue("route:1\x00t", _turn())

        assert queue.release_stale(now=time.monotonic() + 60) == []
        assert queue.is_active("route:1\x00t") is True

    def test_release_stale_ignores_a_session_with_nothing_waiting(self):
        """A stale mark with an empty queue is harmless; don't log about it."""
        queue = SessionQueue(SessionQueueConfig(enabled=True, max_active_seconds=100))
        queue.mark_active("route:1\x00t")

        assert queue.release_stale(now=time.monotonic() + 10_000) == []

    def test_release_stale_is_disabled_at_zero(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True, max_active_seconds=0))
        queue.mark_active("route:1\x00t")
        queue.enqueue("route:1\x00t", _turn())

        assert queue.release_stale(now=time.monotonic() + 10_000) == []
        assert queue.is_active("route:1\x00t") is True

    def test_mark_active_records_a_fresh_timestamp_on_redispatched_turns(self):
        """Draining must reset the clock, not inherit the wedged run's age."""
        queue = SessionQueue(SessionQueueConfig(enabled=True, max_active_seconds=100))
        queue.mark_active("route:1\x00t")
        assert queue.release_stale(now=time.monotonic() + 101) == []  # nothing waiting
        queue.enqueue("route:1\x00t", _turn())
        queue.release("route:1\x00t")
        queue.mark_active("route:1\x00t")

        assert queue.release_stale(now=time.monotonic() + 50) == []
