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

#: How many turns may wait behind one running session before the queue refuses
#: to hold more. A message over the cap is dispatched immediately by the
#: adapter instead of being dropped (issue #151).
DEFAULT_QUEUE_CAP = 20


@dataclass
class SessionQueueConfig:
    """Whether queueing is on, and how deep one session's queue may get."""

    enabled: bool = False
    cap: int = DEFAULT_QUEUE_CAP


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
        self._active: set[str] = set()
        self._pending: dict[str, deque[PendingTurn]] = {}

    def is_active(self, session_key: str) -> bool:
        """Whether a run is in flight (or just dispatched) for this session."""
        return session_key in self._active

    def mark_active(self, session_key: str) -> None:
        """Record that this session's run is in flight."""
        self._active.add(session_key)

    def release(self, session_key: str) -> None:
        """Record that this session has no run in flight."""
        self._active.discard(session_key)

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
