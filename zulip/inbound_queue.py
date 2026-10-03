"""Per-session inbound queue orchestration (issue #151).

A Zulip topic is one session, so a message that arrives while that session has a
run in flight would otherwise be *steered into* the running turn — letting one
person in a shared topic redirect another person's work. ``zulip/session_queue``
holds the ordering rules (which sessions have a run in flight, and the FIFO of
turns waiting behind each one); this module owns the adapter-side orchestration
around them: the session key a turn is queued under, the audit trail of each
queue transition, enqueueing a fully-gated turn, and draining the next waiting
turn when a run ends. The ⏳ reaction and the dispatch itself belong to the
adapter and are reached through it.

Layering: L3 — depends on L0-L2 (``session_queue``, ``outbound``, ``logger``).

The adapter is passed in as an opaque collaborator and its state
(``_session_queue``, ``_audit_logger``, ``_session_key_for_event``,
``handle_message``, ``_stop_typing``, ``_mark_read``) is read at call time, so an
instance patch of any of those still takes effect. ``adapter`` is deliberately
never imported here — that would be a cycle, and the adapter's own methods are
thin delegates into this module.
"""

from __future__ import annotations

import logging
from typing import Any

from . import outbound
from .logger import mask_pii
from .session_queue import PendingTurn

logger = logging.getLogger(__name__)


def event_route(event: Any) -> tuple[str, Any]:
    """``(chat_id, metadata)`` for a gateway event, or ``("", None)``.

    The pair identifies the chat+topic route a work item's delivery events
    belong to, so the activity trace and the delivery audit key work items
    identically.
    """
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    return chat_id, getattr(event, "metadata", None)


class SessionQueueController:
    """Owns the per-session queue orchestration for one adapter."""

    def __init__(self, adapter: Any):
        self._adapter = adapter

    def queue_session_key(self, event: Any) -> str:
        """Session identity for the queue.

        The host's own session key when it exposes one, so the queue agrees with
        the host about what a session is; otherwise the chat+topic route, the
        same pair the activity trace keys on (and the same reasoning: a second
        derivation would drift). Never one global key — two topics in a stream,
        and two DMs, must not serialize against each other.
        """
        key = self._adapter._session_key_for_event(event)
        if key:
            return key
        chat_id, metadata = event_route(event)
        return f"route:{chat_id}\x00{outbound.metadata_topic(metadata) or ''}"

    async def audit_queue_transition(
        self, event_type: str, event: Any, message_id: Any, depth: int
    ) -> None:
        """Audit one queue transition: identifiers and depth, never a body.

        The ⏳ reaction is transient by design, so this file is what proves the
        queue engaged and how deep it got.
        """
        chat_id, _ = event_route(event)
        await self._adapter._audit_logger.log_event(
            event_type,
            {
                "chat_id": chat_id,
                "message_id": None if message_id is None else str(message_id),
                "depth": depth,
            },
        )

    async def _queue_turn(
        self,
        event: Any,
        reactions: Any,
        typing_params: Any,
        message_id: Any,
        *,
        is_slash: bool = False,
    ) -> bool:
        """Queue a fully cleared turn when its session is mid-run.

        True when the turn was queued and must not be dispatched now. Called
        after every gate above, so a queued turn re-enters only the dispatch
        path — waiting can never change what the bot is allowed to answer.

        ``is_slash`` marks a turn the gateway answers itself (a native slash
        command). Those never start an agent run, so the host never fires
        ``on_processing_complete`` for them: queueing one left the session
        marked in-flight forever and wedged every later message in that topic
        behind a run that had already ended. They are never serialized.
        """
        if is_slash:
            return False
        key = self.queue_session_key(event)
        session_queue = self._adapter._session_queue
        if not key:
            return False
        # Safety net: free a session whose run never reported completion, so a
        # single such turn cannot wedge this topic permanently.
        for freed in session_queue.release_stale():
            logger.warning(
                "zulip session queue: no completion signal "
                "[key=%s pending=%d]; recovering",
                mask_pii(freed),
                session_queue.depth(freed),
            )
        if not session_queue.is_active(key):
            return False
        accepted, depth = session_queue.enqueue(
            key,
            PendingTurn(
                event=event,
                reactions=reactions,
                typing_params=typing_params,
                message_id=message_id,
            ),
        )
        if not accepted:
            # Full: dispatch now rather than drop. The cap bounds the wait, not
            # the delivery, and this turn has already cleared every gate.
            await self.audit_queue_transition(
                "session_queue_overflow", event, message_id, depth
            )
            logger.warning(
                "zulip session queue full [key=%s msg=%s depth=%d]; "
                "dispatching immediately",
                mask_pii(key),
                mask_pii(str(message_id)),
                depth,
            )
            return False
        await reactions.queued()
        await self.audit_queue_transition(
            "session_queue_enqueue", event, message_id, depth
        )
        logger.info(
            "zulip session queued [key=%s msg=%s depth=%d]",
            mask_pii(key),
            mask_pii(str(message_id)),
            depth,
        )
        return True

    async def _drain_session_queue(self, event: Any) -> None:
        """Dispatch the oldest turn waiting behind the session that just ended.

        One at a time, in arrival order. The session stays marked active while
        the dispatched turn runs, so a message arriving before the host's start
        hook for it still queues instead of steering it. Never raises: this runs
        inside ``on_processing_complete``, which must not reach the gateway loop.
        """
        session_queue = self._adapter._session_queue
        if session_queue is None:
            return
        key = self.queue_session_key(event)
        if not key:
            return
        await self._drain_key(key)

    async def sweep_stale_sessions(self) -> None:
        """Recover sessions whose run never reported completion, then drain them.

        Called from the adapter's periodic scanner: the ``_queue_turn`` guard
        only fires when a *new* message arrives, so without this a wedged topic
        would stay wedged while nobody posted to it.
        """
        session_queue = self._adapter._session_queue
        if session_queue is None:
            return
        for key in session_queue.release_stale():
            logger.warning(
                "zulip session queue: no completion signal for a session with "
                "waiting turns [key=%s pending=%d]; recovering",
                mask_pii(key),
                session_queue.depth(key),
            )
            try:
                await self._drain_key(key)
            except Exception as e:  # never reach the gateway loop
                logger.warning(
                    "zulip session queue recovery drain failed [key=%s]: %s",
                    mask_pii(key),
                    mask_pii(str(e)),
                )

    async def _drain_key(self, key: str) -> None:
        """Dispatch the oldest turn waiting behind ``key``, one at a time."""
        session_queue = self._adapter._session_queue
        while True:
            turn = session_queue.dequeue(key)
            if turn is None:
                session_queue.release(key)
                return
            session_queue.mark_active(key)
            await self.audit_queue_transition(
                "session_queue_dequeue",
                turn.event,
                turn.message_id,
                session_queue.depth(key),
            )
            await turn.reactions.unqueued()
            try:
                await self._dispatch_turn(
                    turn.event, turn.reactions, turn.typing_params, turn.message_id
                )
            except Exception as e:
                # The turn never reached a run, so its session is free again:
                # give the next waiting turn its chance instead of wedging the
                # queue behind a dispatch that cannot start.
                logger.warning(
                    "zulip session queue dispatch failed [key=%s msg=%s]: %s",
                    mask_pii(key),
                    mask_pii(str(turn.message_id)),
                    mask_pii(str(e)),
                )
                session_queue.release(key)
                continue
            return

    async def _dispatch_turn(
        self, event: Any, reactions: Any, typing_params: Any, message_id: Any
    ) -> None:
        """Hand one cleared turn to the gateway and finish its reaction state.

        Shared by the immediate path and the queue's drain path, so a queued
        turn gets identical error / read / success treatment to one dispatched
        on arrival (issue #151).
        """
        adapter = self._adapter
        try:
            await adapter.handle_message(event)
        except Exception:
            await reactions.error()
            await adapter._stop_typing(typing_params)
            raise
        finally:
            await adapter._mark_read(message_id)

        # Only reached on success. The core stops typing itself via the
        # stop_typing() hook when the agent run finishes; mark the success
        # reaction here.
        await reactions.success()
