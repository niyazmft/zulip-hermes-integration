"""Stable conversation identity: session keys, topic maintenance, migration.

A Zulip topic is a session, and a topic can be renamed or moved — so a session
must key on a persistent *conversation id*, never on the topic name. This module
owns that identity for one adapter: the conversation registry, the pending
dispatch stash, the per-stream reply cache, and the gateway session-store seam
the migration and backfill features read, plus the rules that keep them true
across renames (R2/R3/R5/R8), topic deletions (R10) and upgrades.

``RoutingState`` is the single owner of that state. ``ZulipAdapter`` constructs
one and keeps thin delegates (``_handle_topic_update``, ``_routed_topic``, …)
so ``adapter._<name>`` call sites keep working; the state itself is reached
through ``adapter._routing``.

The poll-loop → handler handoff (F1) is the delicate part and is preserved
verbatim: ``connection.listen_for_events`` calls ``pre_resolve_conversation``
inline, in event-id order, stashing ``msg_id -> conversation_id``; the deferred
handler in ``zulip.inbound`` POPS that stash (``dict.pop``, before any early
return) instead of re-resolving. A rename later in the same batch — or processed
while the handler awaits — must therefore apply *after* the message it precedes;
that ordering is what stops a rename from freeing a name under a queued message
and forking the topic's session onto a fresh conversation. Keep the stash key,
the pop-not-get semantics, the insertion (dict) order, and the call order in
``listen_for_events`` unchanged.

Layering: L3 — depends on L0–L2 (``conversations``, ``settings``, ``outbound``,
``logger``). ``zulip.adapter`` is deliberately never imported here — that would
be a cycle; the adapter is passed in as an opaque collaborator whose state
(``_session_store``, ``client``, ``_sdk_call``) is read at call time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace as _dc_replace
from typing import Any

from . import outbound
from .conversations import TopicConversationRegistry
from .logger import format_zulip_log, mask_pii
from .settings import topic_sessions_enabled as _topic_sessions_enabled

logger = logging.getLogger(__name__)


# Legacy (pre-stable-sessions) zulip stream session key:
#   agent:<profile>:zulip:stream:<channel_id>:<topic_name>
# The topic is everything after the channel prefix — topic names may
# contain colons, so the tail is captured greedily.
_LEGACY_ZULIP_STREAM_KEY = re.compile(
    r"^(?P<prefix>agent:[^:]+:zulip:stream:(?P<channel>\d+):)(?P<topic>.+)$"
)

# Conversation ids minted by the registry ("c" + 12 hex chars) — used to
# recognize already-migrated (conv-keyed) session keys and in-flight replies.
_CONVERSATION_ID_RE = re.compile(r"^c[0-9a-f]{12}$")


def split_session_key_tail(tail: str) -> tuple[str, str | None]:
    """Split a legacy session-key tail into ``(head, user_suffix)``.

    The gateway appends the participant id AFTER the thread segment when
    per-user isolation applies (``group_sessions_per_user`` /
    ``thread_sessions_per_user``), and Zulip participant ids are emails —
    they contain ``@`` and never contain ``:``. So when the last
    colon-separated segment looks like an email, it is the user suffix
    and everything before it is the topic name or conversation id (topic
    names may themselves contain colons, e.g. ``Deploy: XY:user@x.com``).
    Participant ids without ``@`` (non-email shapes) are not detected —
    the Zulip adapter always uses the sender email.
    """
    head, sep, user = tail.rpartition(":")
    if sep and user and "@" in user:
        return head, user
    return tail, None


def session_key_for_event(adapter: Any, event: Any) -> str | None:
    """The host's own session key for an event, when it exposes one.

    Reused rather than reimplemented: a second key derivation would drift
    from the host's, which is the #143 lesson. Returns None when the host
    offers no such method, so callers fall back to the route alias.
    """
    getter = getattr(adapter, "_event_session_key", None)
    if not callable(getter):
        return None
    try:
        key = getter(event)
    except Exception:
        return None
    return str(key) if key else None


def session_aliases(adapter: Any, event: Any) -> list[str]:
    """Aliases that identify this event's run, as seen from the adapter."""
    aliases: list[str] = []
    session_key = session_key_for_event(adapter, event)
    if session_key:
        aliases.append(f"key:{session_key}")
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if chat_id:
        thread = outbound.metadata_topic(getattr(event, "metadata", None)) or ""
        aliases.append(f"route:{chat_id}\x00{thread}")
    return aliases


class RoutingState:
    """Owns the stable conversation-identity state for one adapter."""

    def __init__(self, adapter: Any):
        self._adapter = adapter

        # Stable topic sessions: with topic sessions enabled, sessions key on
        # a persistent conversation id — never the topic name — so renames
        # continue the session instead of stranding it. There is no opt-out:
        # name-keyed topic sessions are not a feature (explicit /new is the
        # only way to start a fresh session in a topic).
        self._conversations: TopicConversationRegistry | None = None
        if _topic_sessions_enabled():
            self._conversations = TopicConversationRegistry(
                account_id=adapter.email or "default",
                data_dir=adapter._data_dir,
            )

        # Fallback topic per stream, used only when a send carries no routing
        # metadata (e.g. TOPIC_SESSIONS off, or metadata-less callers). With
        # topic sessions on, the gateway routes replies by metadata thread_id —
        # the session's own topic — never by this cache.
        self._topic_cache: dict[str, str] = {}

        # F1: conversation ids resolved at event dispatch, keyed by message
        # id (str). Written inline by the poll loop in event-id order;
        # popped by _handle_message so a rename processed between dispatch
        # and the handler's first await cannot fork the conversation.
        self._pending_conversations: dict[str, str] = {}

    # -- gateway session-store seam ----------------------------------------

    def _session_db(self):
        """Gateway seam: ``SessionStore._db`` (the state-db handle, v0.21.5).

        Used read-only for PAST gateway-session generations, which exist in
        the sessions table but not in the routing index. Guarded: any shape
        change degrades the listing/switch features gracefully (no counts,
        live-session-only /continue) instead of raising.
        """
        store = getattr(self._adapter, "_session_store", None)
        return getattr(store, "_db", None)

    # -- dispatch pre-resolution (poll-loop side of the F1 handoff) --------

    def pre_resolve_conversation(self, msg: dict, msg_id: str) -> None:
        """Resolve (mint) a stream topic's conversation at event dispatch,
        in event-id order — the F1 fix.

        `_handle_message` runs as a deferred task, so within one poll batch
        every inline `update_message` rename applies BEFORE any message
        task starts, and a rename can also land while a handler sits in a
        pre-resolve await. Without eager resolution the handler's own
        `resolve()` would then find the old name freed and mint a fresh
        conversation under it — forking the topic's session and routing
        the reply to the resurrected old topic.

        Resolving here, inline in event-id order, makes registry
        operations strictly follow event order for messages and renames
        alike; `_handle_message` pops the stashed id instead of
        re-resolving. Messages later dropped by gating still mint their
        topic's conversation — harmless: the topic exists, so its
        conversation exists, and no gateway session is created until a
        message actually flows. Stash entries are popped at the top of
        `_handle_message` (before any early return), so nothing lingers.
        """
        if self._conversations is None:
            return
        if msg.get("type") != "stream":
            return
        stream_id = msg.get("stream_id")
        topic = msg.get("subject", "")
        if not topic or not isinstance(stream_id, int):
            return
        try:
            conversation_id = self._conversations.resolve(
                stream_id,
                topic,
                anchor_message_id=int(msg_id) if msg_id else None,
            )
        except Exception:
            logger.exception(
                "zulip pre-dispatch conversation resolve failed [msg=%s]",
                mask_pii(msg_id),
            )
            return
        self._pending_conversations[msg_id] = conversation_id

    # -- topic rename/move maintenance -------------------------------------

    def handle_topic_update(self, event: dict) -> None:
        """Registry maintenance for topic renames/moves (stable topic sessions).

        Implements R2/R5 (full rename re-points; the old name keeps only a
        NULL-membership audit row), R3 (partial moves, on any channel, are
        splits: no registry change — the source topic keeps its session)
        and R8 (FULL cross-channel moves free the mapping; the freed set is
        orphaned). Never raises.
        """
        if self._conversations is None:
            return
        stream_id = event.get("stream_id")
        orig_subject = event.get("orig_subject")
        subject = event.get("subject")
        propagate_mode = event.get("propagate_mode", "")
        try:
            if not isinstance(stream_id, int) or not orig_subject:
                return  # content-only edit or malformed event
            if event.get("new_stream_id") is not None:
                # Cross-channel move. Only a FULL move (change_all) empties
                # the source topic: R8 — free the mapping here; the new
                # location becomes a fresh conversation, and the freed
                # session set has no beneficiary (orphaned). A PARTIAL move
                # (change_one/change_later) leaves the source topic alive
                # with its remaining messages — it keeps its session
                # (R3 semantics).
                if propagate_mode == "change_all":
                    freed = self._conversations.free(stream_id, orig_subject)
                    if freed:
                        logger.debug(
                            "zulip conversation freed on cross-channel move"
                            " [channel=%s conv=%s]",
                            stream_id, freed,
                        )
                return
            if not subject or subject == orig_subject:
                return  # same-topic touch (e.g. content edit)
            if propagate_mode == "change_all":
                # R2: full rename — the conversation moves to the new name;
                # the old name keeps only a NULL-membership audit row.
                moved = self._conversations.repoint(stream_id, orig_subject, subject)
                if moved:
                    logger.debug(
                        "zulip conversation repointed [channel=%s conv=%s"
                        " old=%r new=%r]",
                        stream_id, moved, mask_pii(orig_subject), mask_pii(subject),
                    )
            # R3: change_one/change_later are splits — the new name resolves
            # to a new conversation via R1 on its next message.
        except Exception as e:
            logger.warning(
                format_zulip_log(
                    "zulip topic registry update failed",
                    error=mask_pii(str(e)),
                )
            )

    # -- routed-topic lookup (reply routing) -------------------------------

    def routed_topic(self, stream_id: int, metadata: Any) -> str | None:
        """Routing topic from send metadata, resolving conversation ids.

        With stable topic sessions, ``metadata["thread_id"]`` carries a
        conversation id; map it to the conversation's CURRENT topic name (R6 —
        an in-flight reply after a rename lands in the new name). For a
        conversation id that is no longer live (orphaned by a topic deletion
        or a cross-channel move while the reply was in flight), the reply
        lands on the conversation's LAST known topic name instead of
        materializing a ghost topic named like the id. Unknown ids with no
        record, and registry-less runs, return the raw value (legacy
        name-keyed sessions keep working verbatim).
        """
        raw = outbound.metadata_topic(metadata)
        if raw and self._conversations is not None:
            try:
                current = self._conversations.current_name(int(stream_id), raw)
            except (TypeError, ValueError):
                current = None
            if current:
                return current
            if _CONVERSATION_ID_RE.fullmatch(raw):
                # Conv-shaped but not live: route by the LAST known topic
                # name from the conversation's own former_holders row (never a
                # ghost topic named like the id). No mapping is created —
                # the orphaned session stays unreachable (R4/R10 intact).
                try:
                    last = self._conversations.last_topic_of(int(stream_id), raw)
                except (TypeError, ValueError):
                    last = None
                if last:
                    return last
        return raw

    def routed_topic_for_chat(self, chat_id: str, metadata: Any) -> str | None:
        """``routed_topic`` for chat-id strings (typing hooks)."""
        if not chat_id.isdigit():
            return outbound.metadata_topic(metadata)
        return self.routed_topic(int(chat_id), metadata)

    # -- topic deletion (R10) ----------------------------------------------

    async def apply_topic_deletion(self, stream_id: int, topic: str) -> None:
        """Verify a deletion trigger against the channel's topic list and,
        only if the topic is really gone, orphan its session set (R10)."""
        try:
            result = await self._adapter._sdk_call(
                self._adapter.client.get_stream_topics, stream_id, timeout=10.0
            )
        except Exception:
            logger.exception(
                "zulip topic-deletion verification failed; mapping kept"
                " [channel=%s topic=%r]",
                stream_id,
                mask_pii(topic),
            )
            return
        if not isinstance(result, dict) or result.get("result") != "success":
            logger.warning(
                "zulip topic-deletion verification unavailable; mapping kept"
                " [channel=%s topic=%r]",
                stream_id,
                mask_pii(topic),
            )
            return
        names = {str(t.get("name", "")) for t in (result.get("topics") or [])}
        if topic in names:
            logger.info(
                "zulip delete event verified: topic still exists — ignored"
                " [channel=%s topic=%r]",
                stream_id,
                mask_pii(topic),
            )
            return
        orphaned = self._conversations.orphan_topic_sessions(stream_id, topic)
        logger.info(
            "zulip topic deleted — session set orphaned (no beneficiary)"
            " [channel=%s topic=%r former=%d]",
            stream_id,
            mask_pii(topic),
            orphaned,
        )

    # -- upgrade migration -------------------------------------------------

    def migrate_legacy_topic_sessions(self) -> int:
        """One-time continuity migration (upgrade or first enable).

        Re-keys existing name-keyed zulip stream sessions to the stable
        conversation-id keys, so users keep their current sessions when
        the feature turns on instead of starting fresh per topic:

            agent:<ns>:zulip:stream:<ch>:<topic>  ->  ...:<ch>:<conv_id>

        The registry is seeded with the (topic -> conversation) mapping in
        the same pass; transcripts are untouched (the session id does not
        change, only its routing key). The greedy key tail is parsed
        shape-aware (see ``split_session_key_tail``):

        - an email-shaped trailing segment is a per-user suffix
          (``group_sessions_per_user`` / ``thread_sessions_per_user``
          deployments) and is preserved on the re-keyed key;
        - a head matching the conversation-id format is recognized as
          already conv-keyed only when the REGISTRY knows that
          conversation (live row or orphaned audit row) — an id-shaped
          head the registry does not know is a legacy session for a topic
          literally named like an id, and migrates normally;
        - an email-shaped head (feature-off legacy key: the tail is the
          sender, not a topic) is left in place — minting a topic for it
          would create a junk conversation and rekey the session to a key
          nothing routes to.

        A name-keyed route whose conv-keyed successor already exists
        (previous enable cycle) is dropped as stale. Runs before the
        first message flows (the gateway wires ``set_session_store``
        during adapter setup).

        Uses the store's routing internals (``_entries``/``_save`` under
        ``_lock``) the same way the store's own ``rekey_profile_routing``
        does — no per-key public rekey API exists on the store yet;
        revisit on gateway upgrades.
        """
        if self._conversations is None:
            return 0
        store = getattr(self._adapter, "_session_store", None)
        entries = getattr(store, "_entries", None)
        lock = getattr(store, "_lock", None)
        save = getattr(store, "_save", None)
        if entries is None or lock is None or save is None:
            logger.debug(
                "zulip legacy-session migration skipped: no compatible session store"
            )
            return 0
        with lock:
            moves = []
            for key, entry in list(entries.items()):
                m = _LEGACY_ZULIP_STREAM_KEY.match(key)
                if m is None:
                    continue
                head, user_suffix = split_session_key_tail(m["topic"])
                if _CONVERSATION_ID_RE.fullmatch(head):
                    if self._conversations.has_conversation(
                        int(m["channel"]), head
                    ):
                        # Registry knows this conversation: the key is
                        # already conv-keyed (with or without a per-user
                        # suffix). An unknown id-shaped head falls through —
                        # it is a legacy session for a topic literally named
                        # like a conversation id, and migrates normally.
                        continue
                elif "@" in head:
                    # Feature-off legacy key: the tail is the sender (a
                    # per-stream-per-user session), not a topic name. Leave
                    # it in place — there is no per-topic successor key for
                    # it, and minting a topic would strand the session.
                    continue
                conversation_id = self._conversations.resolve(
                    int(m["channel"]), head
                )
                new_key = f"{m['prefix']}{conversation_id}"
                if user_suffix:
                    # Preserve the per-user suffix so the re-keyed key
                    # matches what the gateway will build post-enable.
                    new_key = f"{new_key}:{user_suffix}"
                if new_key in entries:
                    # The conv-keyed session is the successor; the name-keyed
                    # route is stale residue from a previous enable cycle.
                    entries.pop(key, None)
                    logger.info(
                        "zulip legacy migration dropped stale route [key=%s]",
                        mask_pii(key),
                    )
                    continue
                moves.append((key, new_key, entry))
            for old_key, new_key, entry in moves:
                entries.pop(old_key, None)
                entries[new_key] = _dc_replace(entry, session_key=new_key)
            if moves:
                save()
        if moves:
            logger.info(
                "zulip legacy-session migration: rekeyed %d session(s)", len(moves)
            )
        return len(moves)

    def backfill_session_starts(self) -> int:
        """Record start labels for every known session (idempotent).

        Runs at store wiring after the legacy migration: for each live
        conversation, enumerate its gateway sessions (live routing entry
        plus past generations) and record each missing label via the
        registry's derivation. ``INSERT OR IGNORE`` means existing
        records always win and restarts only ever fill gaps.
        """
        if self._conversations is None:
            return 0
        entries = self._adapter._route_entries()
        db = self._session_db()
        recorded = 0
        for channel_id, conversation_id in self._conversations.iter_conversations():
            key, entry = self._adapter._entry_for_conversation(entries, conversation_id)
            ids = []
            if entry is not None and entry.session_id:
                ids.append(entry.session_id)
            if db is not None and key is not None:
                try:
                    rows = db.list_sessions_rich(
                        session_key=key, limit=50,
                        order_by_last_active=True, include_hidden=True,
                    )
                except Exception as exc:
                    logger.debug(
                        "zulip session enumeration unavailable [conv=%s]: %s",
                        conversation_id, exc,
                    )
                    rows = []
                for row in rows:
                    sid = (row or {}).get("id") or (row or {}).get(
                        "session_id")
                    if sid and sid not in ids:
                        ids.append(sid)
            for sid in ids:
                if self._conversations.session_start(channel_id, sid) is None:
                    self._conversations.record_session_start(
                        channel_id, conversation_id, sid
                    )
                    recorded += 1
        if recorded:
            logger.debug(
                "zulip session-start backfill recorded %d labels", recorded
            )
        return recorded
