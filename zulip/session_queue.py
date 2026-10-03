"""Per-session inbound queue (issue #151).

A Zulip topic is one session, so a message that arrives while that session has a
run in flight would otherwise be *steered into* the running turn — letting one
person in a shared topic redirect another person's work. This module holds the
ordering rules: which sessions have a run in flight, and the FIFO of turns
waiting behind each one.

State only, deliberately. The adapter owns the ⏳ reaction, the audit trail and
the dispatch, so the ordering can be exercised without a gateway.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional
import time

#: How many turns may wait behind one running session before the queue refuses
#: to hold more. A message over the cap is dispatched immediately by the
#: adapter instead of being dropped (issue #151).
DEFAULT_QUEUE_CAP = 20

#: Safety net: how long a session may stay marked "run in flight" before the
#: queue assumes the run will never report completion and frees it. This exists
#: because the drain is driven by the host's ``on_processing_complete``, which
#: does NOT fire for a turn the gateway answers itself (e.g. a slash command it
#: handles natively). Such a turn left the session marked active forever, so
#: every later message queued behind a run that never completed. The bound is
#: deliberately far above the slowest observed real turn (~7.5 min) so a legit
#: long run is never cut short.
DEFAULT_MAX_ACTIVE_SECONDS = 1800.0


@dataclass
class SessionQueueConfig:
    """Whether queueing is on, and how deep one session's queue may get."""

    enabled: bool = False
    cap: int = DEFAULT_QUEUE_CAP
    #: Free a session marked active for longer than this (seconds).
    max_active_seconds: float = DEFAULT_MAX_ACTIVE_SECONDS


@dataclass
class PendingTurn:
    """One turn waiting its turn: everything needed to finish it later.

    ``reactions``/``typing_params`` are carried because dispatch happens long
    after ``_handle_message`` built them; ``message_id`` is what the read marker
    and the audit trail key on.
    """

    event: Any
    reactions: Any
    typing_params: Any
    message_id: Any


class SessionQueue:
    """Which sessions have a run in flight, and what is queued behind each one."""

    def __init__(self, config: SessionQueueConfig):
        self.config = config
        #: session_key -> monotonic time the run was marked in flight. A mapping
        #: rather than a set so a wedge can be detected and recovered.
        self._active: dict[str, float] = {}
        self._pending: dict[str, deque[PendingTurn]] = {}

    def is_active(self, session_key: str) -> bool:
        """Whether a run is in flight (or just dispatched) for this session."""
        return session_key in self._active

    def mark_active(self, session_key: str) -> None:
        """Record that this session's run is in flight, and when it started."""
        self._active[session_key] = time.monotonic()

    def release(self, session_key: str) -> None:
        """Record that this session has no run in flight."""
        self._active.pop(session_key, None)

    def release_stale(self, now: Optional[float] = None) -> list[str]:
        """Free sessions whose in-flight run never reported completion.

        Returns the freed keys so the caller can log them. Only sessions with
        turns actually waiting are freed: a stale mark with an empty queue is
        harmless (the next message dispatches immediately and re-marks it), so
        releasing it would only cost a spurious log line.
        """
        current = time.monotonic() if now is None else now
        limit = self.config.max_active_seconds
        if limit <= 0:
            return []
        stale = [
            key
            for key, started in self._active.items()
            if current - started > limit and self._pending.get(key)
        ]
        for key in stale:
            self._active.pop(key, None)
        return stale

    def depth(self, session_key: str) -> int:
        """How many turns are waiting for this session."""
        return len(self._pending.get(session_key, ()))

    def enqueue(self, session_key: str, turn: PendingTurn) -> tuple[bool, int]:
        """Append ``turn``, or refuse it when the queue is already full.

        Returns ``(accepted, depth)``. A refused turn is the caller's to
        dispatch immediately: the cap bounds the *wait*, never the delivery.
        """
        queue = self._pending.setdefault(session_key, deque())
        if len(queue) >= self.config.cap:
            return False, len(queue)
        queue.append(turn)
        return True, len(queue)

    def dequeue(self, session_key: str) -> Optional[PendingTurn]:
        """Pop the oldest waiting turn, or None when there is none."""
        queue = self._pending.get(session_key)
        if not queue:
            return None
        turn = queue.popleft()
        if not queue:
            del self._pending[session_key]
        return turn
